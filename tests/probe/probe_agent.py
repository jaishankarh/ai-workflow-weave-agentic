"""Probe Agent profile: a minimal ACP agent for Seam A tests.

It speaks ACP over stdio like a real agent CLI (Claude Code's
claude-agent-acp, Cursor's `agent acp`). On each prompt it:

1. reads its script from a ``probe: {...}`` line in the prompt (the test
   puts it in the run's spec text);
2. emits a report of what it sees inside the sandbox, as a completed tool
   call titled ``probe-report`` whose raw output is the JSON report (tool
   calls are streamed live, so the report reaches the saved event log even
   if the run is later cancelled);
3. ends the way the script says:

   - ``{"end": "succeed"}``  reports completion (``RUN-OUTCOME: done``)
   - ``{"end": "give-up", "reason": "..."}``  ends without completing
   - ``{"end": "reply", "text": "..."}``  ends with exactly that final reply
   - ``{"end": "hang", "command": "sleep 600"}``  runs a command that never
     finishes and waits on it. Like Claude Code, an ACP ``session/cancel``
     (an interrupt) does NOT stop the command; only closing the agent does.
   - ``{"edit": {"<path>": "<text>"}}`` (with any end) first changes a file in the
     first working copy, uncommitted
   - ``{"end": "error", "errorKind": "rate_limit", "message": "..."}``  fails
     the prompt the way claude-agent-acp fails a turn: a JSON-RPC internal
     error (-32603, which the SDK retries unless ``ACP_PROMPT_MAX_RETRIES``
     caps it) with the message as its text and ``{"errorKind": "..."}`` as its
     data (``"errorKind": null`` sends no data; ``"code"`` changes the JSON-RPC
     code, e.g. -32000, ACP's "authentication required"). Every attempt is counted in
     ``/tmp/probe-prompt-attempts``.

Add a new report item by adding a function to ``REPORTERS``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any, Callable

from acp import (
    InitializeResponse,
    RequestError,
    NewSessionResponse,
    PromptResponse,
    run_agent,
    start_tool_call,
    update_agent_message_text,
)
from acp.schema import Implementation

PROBE_VERSION = "1"
WORKSPACE = Path(os.environ.get("PROBE_WORKSPACE", "/workspace"))
# Left behind by every probe run; a fresh sandbox never contains one.
MARKERS = [Path.home() / ".probe-was-here", Path("/tmp/.probe-was-here"), WORKSPACE / ".probe-was-here"]

# --------------------------------------------------------------------------- report


def report_earlier_run(ctx: dict[str, Any]) -> Any:
    """Markers left by an earlier probe run that this sandbox can see."""
    return [str(m) for m in MARKERS if m.exists()]


def report_env_names(ctx: dict[str, Any]) -> Any:
    """Names (never values) of the agent's environment variables."""
    return sorted(os.environ)


def report_repos(ctx: dict[str, Any]) -> Any:
    """Each git working copy under the workspace: branch and cleanliness."""
    repos = []
    if WORKSPACE.is_dir():
        for d in sorted(WORKSPACE.iterdir()):
            if not (d / ".git").exists():
                continue
            branch = _git(d, "rev-parse", "--abbrev-ref", "HEAD")
            status = _git(d, "status", "--porcelain")
            repos.append({"name": d.name, "branch": branch, "clean": status == ""})
    return repos


def report_cwd(ctx: dict[str, Any]) -> Any:
    return ctx.get("cwd")


def report_env_fingerprints(ctx: dict[str, Any]) -> Any:
    """SHA-256 of the env vars the script names in ``fingerprint_env`` (so values never reach the log)."""
    names = ctx["script"].get("fingerprint_env") or []
    return {n: hashlib.sha256(os.environ[n].encode()).hexdigest() for n in names if n in os.environ}


def report_prompt_has_secrets_block(ctx: dict[str, Any]) -> Any:
    """Whether OpenHands sent conversation secrets (its <CUSTOM_SECRETS> block) with the prompt."""
    return "<CUSTOM_SECRETS>" in ctx.get("prompt", "")


USER_SKILLS = Path.home() / ".claude" / "skills"
PROJECT_SKILLS = Path(".claude") / "skills"


def _skill_description(skill_md: Path) -> str | None:
    m = re.search(r"^description:\s*(.*)$", skill_md.read_text(), re.MULTILINE)
    return m.group(1).strip().strip('"') if m else None


def _skills_in(folder: Path) -> dict[str, str | None]:
    if not folder.is_dir():
        return {}
    return {d.name: _skill_description(d / "SKILL.md")
            for d in sorted(folder.iterdir()) if (d / "SKILL.md").is_file()}


def report_user_skills(ctx: dict[str, Any]) -> Any:
    """Skills staged at the agent's user level: name -> description."""
    return _skills_in(USER_SKILLS)


def report_skills_loaded(ctx: dict[str, Any]) -> Any:
    """The skill the agent loads for each name, by Claude Code's rule: a user-level
    skill wins over a project (Repo) skill of the same name."""
    loaded: dict[str, dict] = {}
    if WORKSPACE.is_dir():
        for repo in sorted(WORKSPACE.iterdir()):
            for name, desc in _skills_in(repo / PROJECT_SKILLS).items():
                loaded.setdefault(name, {"level": "project", "repo": repo.name, "description": desc})
    for name, desc in _skills_in(USER_SKILLS).items():
        loaded[name] = {"level": "user", "description": desc}
    return loaded


def report_skills_turned_off(ctx: dict[str, Any]) -> Any:
    """Skills turned off in the agent's user-level settings (Claude Code's `skillOverrides`)."""
    settings = Path.home() / ".claude" / "settings.json"
    if not settings.is_file():
        return []
    overrides = json.loads(settings.read_text()).get("skillOverrides") or {}
    return sorted(name for name, state in overrides.items() if state == "off")


def report_working_copies(ctx: dict[str, Any]) -> Any:
    """Each working copy's full `git status` (ignored and untracked files too) and a
    digest of every file outside `.git`, to compare with the Repo as committed."""
    out = {}
    if WORKSPACE.is_dir():
        for d in sorted(WORKSPACE.iterdir()):
            if (d / ".git").exists():
                out[d.name] = {
                    "status": _git(d, "status", "--porcelain", "--ignored", "--untracked-files=all"),
                    "digest": tree_digest(d),
                }
    return out


def tree_digest(root: Path) -> str:
    """sha256 over every file's relative path and bytes, `.git` excluded."""
    h = hashlib.sha256()
    for f in sorted(p for p in root.rglob("*") if p.is_file() and ".git" not in p.relative_to(root).parts):
        h.update(str(f.relative_to(root)).encode() + b"\0" + f.read_bytes() + b"\0")
    return h.hexdigest()


PROMPT_ATTEMPTS = Path("/tmp/probe-prompt-attempts")


def _count_attempt() -> None:
    with open(PROMPT_ATTEMPTS, "a") as f:
        f.write("prompt\n")


def report_prompt_attempts(ctx: dict[str, Any]) -> Any:
    """How many times this sandbox's agent has been prompted (retries included)."""
    return len(PROMPT_ATTEMPTS.read_text().splitlines()) if PROMPT_ATTEMPTS.exists() else 0


_TRACKER_DOC_IN_PROMPT = re.compile(r"issue tracker is described in `([^`]+)`")


def report_local_tickets(ctx: dict[str, Any]) -> Any:
    """The tracker as an upstream skill finds it: the tracker description the prompt names,
    the tickets it points at, and which read-only originals the probe managed to change."""
    m = _TRACKER_DOC_IN_PROMPT.search(ctx.get("prompt", ""))
    if not m:
        return {"tracker": None}
    doc = Path(m.group(1)).read_text()
    heading = doc.splitlines()[0]
    tracker = heading.split(":", 1)[1].strip().lower().replace(" ", "-")
    tickets_dir = Path(re.search(r"Tickets live in `([^`]+)`", doc).group(1))
    originals_dir = Path(re.search(r"originals are in `([^`]+)`", doc).group(1))
    tickets = [{"kind": "spec", "path": str(tickets_dir / "spec.md"), "body": (tickets_dir / "spec.md").read_text()}]
    for f in sorted((tickets_dir / "issues").glob("*.md")):
        tickets.append({"kind": "task", "path": str(f), "body": f.read_text()})
    originals = sorted(str(p) for p in originals_dir.rglob("*") if p.is_file())
    return {"tracker": tracker, "tickets": tickets, "originals": originals,
            "originals_changed": try_to_change(originals_dir)}


def try_to_change(folder: Path) -> list[str]:
    """Try to write, chmod, delete and add files in a folder; what succeeded."""
    changed = []
    for p in sorted(folder.rglob("*")):
        if not p.is_file():
            continue
        attempts = [
            ("write", lambda: p.open("a").write("tampered\n")),
            ("chmod", lambda: os.chmod(p, 0o666)),
            ("rename", lambda: os.rename(p, str(p) + ".moved")),
            ("delete", lambda: p.unlink()),
        ]
        for what, attempt in attempts:
            try:
                attempt()
                changed.append(f"{what} {p}")
            except OSError:
                pass
    try:
        (folder / "new.md").write_text("x")
        changed.append(f"create {folder / 'new.md'}")
    except OSError:
        pass
    return changed


def _tracker_dir(prompt_text: str) -> Path:
    doc = Path(_TRACKER_DOC_IN_PROMPT.search(prompt_text).group(1)).read_text()
    return Path(re.search(r"Tickets live in `([^`]+)`", doc).group(1))


def mark_done(prompt_text: str, ticket: str) -> dict[str, Any]:
    """Close a ticket the way the tracker description says: set its Status line to done."""
    folder = _tracker_dir(prompt_text)
    [path] = [folder / "spec.md"] if ticket == "spec" else sorted((folder / "issues").glob(f"{ticket}-*.md"))
    text = re.sub(r"^Status:.*$", "Status: done", path.read_text(), count=1, flags=re.MULTILINE)
    path.write_text(text)
    return {"action": "mark_done", "ticket": ticket, "ok": True}


def edit(rel: str, text: str) -> dict[str, Any]:
    """Change a file in the first working copy (left uncommitted)."""
    repo = next(d for d in sorted(WORKSPACE.iterdir()) if (d / ".git").exists())
    (repo / rel).write_text(text)
    return {"action": "edit", "path": rel, "ok": True}


def push(branch: str) -> dict[str, Any]:
    """Commit in the first working copy and push HEAD to `branch` on its remote."""
    repo = next(d for d in sorted(WORKSPACE.iterdir()) if (d / ".git").exists())
    _git(repo, "commit", "-q", "--allow-empty", "-m", f"probe commit for {branch}")
    commit = _git(repo, "rev-parse", "HEAD")
    r = subprocess.run(["git", "-C", str(repo), "push", "origin", f"HEAD:refs/heads/{branch}"],
                       capture_output=True, text=True, timeout=60)
    return {"action": "push", "branch": branch, "ok": r.returncode == 0, "commit": commit,
            "output": (r.stderr or r.stdout)[-1500:]}


CODE_HOST_API = "https://api.github.com"
# What each Tracker / Code host write would POST (REST, as `gh` does it).
CODE_HOST_WRITES = {
    "issue": "/repos/{repo}/issues",
    "comment": "/repos/{repo}/issues/1/comments",
    "label": "/repos/{repo}/issues/1/labels",
    "pr": "/repos/{repo}/pulls",
}
TOKEN_VARS = ["GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN", "GITLAB_TOKEN"]


def find_code_host_credential() -> str | None:
    """Where an agent could get a Code host credential: a token variable, or git's credential store."""
    for name in TOKEN_VARS:
        if os.environ.get(name):
            return f"env {name}"
    r = subprocess.run(["git", "credential", "fill"], input="protocol=https\nhost=github.com\n\n",
                       capture_output=True, text=True, timeout=15,
                       env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "/bin/false"})
    if r.returncode == 0 and "password=" in r.stdout:
        return "git credential helper"
    return None


def code_host_write(write: str) -> dict[str, Any]:
    """Try a Tracker / Code host write the way an agent would: find a credential, then POST."""
    from urllib.request import Request, urlopen

    result: dict[str, Any] = {"action": "code_host_write", "write": write, "ok": False}
    result["credential_found"] = cred = find_code_host_credential()
    if cred is None:
        result["error"] = "no credentials for the Tracker or Code host"
        return result
    token = os.environ.get(cred.removeprefix("env "), "")
    req = Request(CODE_HOST_API + CODE_HOST_WRITES[write].format(repo="weave-fixture/app"),
                  data=b"{}", method="POST", headers={"Authorization": f"token {token}"})
    try:
        with urlopen(req, timeout=10) as resp:
            result["ok"] = 200 <= resp.status < 300
            result["status"] = resp.status
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"
    return result


def run_actions(script: dict[str, Any], prompt_text: str) -> list[dict[str, Any]]:
    """Do what the script asks before reporting; each action's result goes in the report."""
    results = []
    for rel, text in (script.get("edit") or {}).items():
        results.append(_attempt(lambda: edit(rel, text), {"action": "edit", "path": rel}))
    for ticket in script.get("mark_done", []):
        results.append(_attempt(lambda: mark_done(prompt_text, ticket), {"action": "mark_done", "ticket": ticket}))
    for branch in script.get("push", []):
        results.append(_attempt(lambda: push(branch), {"action": "push", "branch": branch}))
    for write in script.get("code_host_writes", []):
        results.append(_attempt(lambda: code_host_write(write), {"action": "code_host_write", "write": write}))
    return results


def _attempt(fn: Callable[[], dict[str, Any]], what: dict[str, Any]) -> dict[str, Any]:
    try:
        return fn()
    except Exception as e:
        return {**what, "ok": False, "error": f"{type(e).__name__}: {e}"}


def report_git_credentials(ctx: dict[str, Any]) -> Any:
    """Git credential helpers configured, and credential files present, for the agent's user."""
    r = subprocess.run(["git", "config", "--get-all", "credential.helper"], capture_output=True, text=True)
    candidates = [Path.home() / ".git-credentials", Path.home() / ".config" / "git" / "credentials",
                  Path.home() / ".config" / "gh" / "hosts.yml", Path.home() / ".config" / "glab-cli" / "config.yml",
                  Path.home() / ".netrc"]
    return {"helpers": r.stdout.split(), "files": [str(p) for p in candidates if p.exists()]}


USER_ALWAYS_ON = Path.home() / ".claude" / "CLAUDE.md"
_POINTED_PATH = re.compile(r"`(/[^`]+)`")


def report_always_on(ctx: dict[str, Any]) -> Any:
    """The always-on file at the agent's user level, and every absolute path it names in
    backticks: path -> the file's content (None if there is no such file)."""
    if not USER_ALWAYS_ON.is_file():
        return None
    content = USER_ALWAYS_ON.read_text()
    pointed = {}
    for path in dict.fromkeys(_POINTED_PATH.findall(content)):
        p = Path(path)
        pointed[path] = p.read_text() if p.is_file() else None
    return {"path": str(USER_ALWAYS_ON), "content": content, "pointed": pointed}


def report_actions(ctx: dict[str, Any]) -> Any:
    """Results of the actions the script asked for (done before the report)."""
    return ctx.get("actions", [])


def _docker(*args: str, timeout: float = 240) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)


def report_container(ctx: dict[str, Any]) -> Any:
    """Start a container inside the sandbox's own Docker engine, stop it, and say what happened.

    Script key ``run_container``: ``{"image": ..., "leave_running": bool}``. One container prints a
    line; another is started with ``sleep`` and stopped (unless ``leave_running``).
    """
    spec = ctx["script"].get("run_container")
    if not spec:
        return None
    image = spec["image"]
    result: dict[str, Any] = {"image": image, "started": False, "stopped": False}
    try:
        ran = _docker("run", "--rm", image, "echo", "hello from inside the sandbox")
        result["output"] = ran.stdout
        if ran.returncode != 0:
            result["error"] = ran.stderr.strip()[-500:]
            return result
        sleeper = _docker("run", "-d", image, "sleep", "300")
        if sleeper.returncode != 0:
            result["error"] = sleeper.stderr.strip()[-500:]
            return result
        cid = sleeper.stdout.strip()
        result["started"] = _docker("inspect", "-f", "{{.State.Running}}", cid).stdout.strip() == "true"
        if spec.get("leave_running"):
            return result
        _docker("stop", "-t", "1", cid)
        result["stopped"] = _docker("inspect", "-f", "{{.State.Running}}", cid).stdout.strip() == "false"
        _docker("rm", "-f", cid)
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"
    return result


def report_docker_socket_mounts(ctx: dict[str, Any]) -> Any:
    """Mounts of a docker.sock into this sandbox from the Sandbox host. The nested engine's own
    socket is a file it creates, not a mount, so a healthy sandbox reports none."""
    return [line for line in Path("/proc/self/mountinfo").read_text().splitlines() if "docker.sock" in line]


def _service_container(repo: str, service: str) -> str | None:
    """The id of the Environment container for `service` of `repo` (a Compose project named for the Repo)."""
    out = _docker(
        "ps", "-q",
        "--filter", f"label=com.docker.compose.project={repo}",
        "--filter", f"label=com.docker.compose.service={service}",
    ).stdout.split()
    return out[0] if out else None


def report_environment(ctx: dict[str, Any]) -> Any:
    """What the Environment looks like from inside the sandbox (script keys, all optional):

    ``reach``: ``[{"repo", "service", "port", "path"}]``  HTTP GET the service through its address
        on the Environment network (resolved to the container's IP on that network, as the sandbox's
        own resolver does not know ``<service>.<repo>``), with no retry.
    ``exec_in_service``: ``[{"repo", "service", "command"}]``  run a shell command in a service's container.
    ``resolve``: ``["<service>.<repo>", ...]``  what the sandbox's own resolver answers for each name.
    ``list_containers``: true  every container in the sandbox's own engine, running or not.
    """
    from urllib.request import urlopen

    script = ctx["script"]
    out: dict[str, Any] = {}
    if reaches := script.get("reach"):
        results = []
        for spec in reaches:
            item: dict[str, Any] = dict(spec)
            try:
                cid = _service_container(spec["repo"], spec["service"])
                if cid is None:
                    item["error"] = "no such service container"
                else:
                    net = '(index .NetworkSettings.Networks "weave-env")'
                    ip = _docker("inspect", "-f", "{{" + net + ".IPAddress}}", cid).stdout.strip()
                    aliases = _docker("inspect", "-f", "{{" + net + ".Aliases}}", cid).stdout
                    item["alias_ok"] = f"{spec['service']}.{spec['repo']}" in aliases
                    # Compose's own bare-name alias would clash across Repos (#50).
                    item["bare_alias"] = spec["service"] in aliases.strip("[]").split()
                    with urlopen(f"http://{ip}:{spec['port']}{spec.get('path', '/')}", timeout=5) as r:
                        item["status"], item["body"] = r.status, r.read().decode()[:500]
            except Exception as e:
                item["error"] = f"{type(e).__name__}: {e}"
            results.append(item)
        out["reach"] = results
    if execs := script.get("exec_in_service"):
        results = []
        for spec in execs:
            item = dict(spec)
            try:
                cid = _service_container(spec["repo"], spec["service"])
                r = _docker("exec", cid, "sh", "-c", spec["command"])
                item["exit"], item["output"] = r.returncode, (r.stdout + r.stderr)[-1000:]
            except Exception as e:
                item["error"] = f"{type(e).__name__}: {e}"
            results.append(item)
        out["exec"] = results
    if names := script.get("resolve"):
        # The sandbox's own resolver (the agent's shell), for `<service>.<repo>` names.
        import socket

        out["resolve"] = {}
        for name in names:
            try:
                out["resolve"][name] = socket.gethostbyname(name)
            except OSError as e:
                out["resolve"][name] = f"error: {e}"
    if script.get("list_containers"):
        r = _docker("ps", "-a", "--format", "{{.Names}}")
        out["containers"] = r.stdout.split() if r.returncode == 0 else {"error": r.stderr.strip()[-300:]}
    return out or None


def report_weave_env(ctx: dict[str, Any]) -> Any:
    """Run the agent's `weave-env` command as the script asks (``weave_env``: a list run in order,
    each an argument list, or ``{"exec": {"repo", "service", "command"}}`` to change a service's
    data in between) and say what each did: ``[{"args", "exit", "output"}]``. All of this happens
    before the ``environment`` report looks at the Environment."""
    results = []
    for args in ctx["script"].get("weave_env") or []:
        item: dict[str, Any] = {"args": args}
        try:
            if isinstance(args, dict):
                spec = args["exec"]
                r = _docker("exec", _service_container(spec["repo"], spec["service"]), "sh", "-c", spec["command"])
            else:
                r = subprocess.run(["weave-env", *args], capture_output=True, text=True, timeout=1800)
            item["exit"], item["output"] = r.returncode, (r.stdout + r.stderr)[-3000:]
        except Exception as e:
            item["error"] = f"{type(e).__name__}: {e}"
        results.append(item)
    return results or None

def _user_mcp_servers() -> dict[str, Any]:
    """The MCP servers Claude Code would load: the managed configuration's (`mcpServers` in
    /etc/claude-code/managed-mcp.json), which since #56 holds the only ones it loads. The location
    can be overridden with WEAVE_MANAGED_MCP, for testing the probe outside a sandbox."""
    path = Path(os.environ.get("WEAVE_MANAGED_MCP") or "/etc/claude-code/managed-mcp.json")
    if not path.is_file():
        return {}
    return json.loads(path.read_text()).get("mcpServers") or {}


async def _mcp_session(entry: dict[str, Any], work: Callable[[Any], Any]) -> Any:
    """Start one configured stdio server as Claude Code does (its command, arguments and environment)
    and run `work(session)` against it."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(command=entry["command"], args=entry.get("args", []), env=entry.get("env"))
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            return await work(session)


def _in_own_loop(coro: Any) -> Any:
    """Run a coroutine to the end from inside the probe's running event loop (in a thread of its own)."""
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


def report_mcp(ctx: dict[str, Any]) -> Any:
    """The agent's MCP servers, as its user-level configuration has them (script key ``mcp``, optional):

    Always ``configured``: ``{name: {"command", "args", "env_names"}}`` (never an environment value).
    ``{"tools": true}``  adds ``tools``: ``{name: [tool names]}``, asking each server.
    ``{"calls": [{"server", "tool", "arguments"}]}``  adds ``calls``: the same dicts with ``ok`` and
        ``text`` (the tool's text output) or ``error``. A failure is reported, never raised.
    """
    servers = _user_mcp_servers()
    out: dict[str, Any] = {
        "configured": {
            name: {"command": e.get("command"), "args": e.get("args", []), "env_names": sorted(e.get("env") or {})}
            for name, e in servers.items()
        }
    }
    spec = ctx["script"].get("mcp") or {}
    if spec.get("tools"):
        out["tools"] = {}
        for name, entry in servers.items():
            try:
                listed = _in_own_loop(_mcp_session(entry, lambda s: s.list_tools()))
                out["tools"][name] = [t.name for t in listed.tools]
            except BaseException as e:  # noqa: BLE001 - reported, not raised
                out["tools"][name] = {"error": f"{type(e).__name__}: {e}"}
    if calls := spec.get("calls"):
        out["calls"] = []
        for call in calls:
            item: dict[str, Any] = dict(call)
            try:
                if call["server"] not in servers:
                    raise LookupError(f"server {call['server']!r} is not configured")
                result = _in_own_loop(_mcp_session(
                    servers[call["server"]], lambda s, c=call: s.call_tool(c["tool"], c.get("arguments") or {})
                ))
                item["text"] = "".join(getattr(part, "text", "") for part in result.content)
                item["ok"] = not result.isError
                if result.isError:
                    item["error"] = item["text"]
            except BaseException as e:  # noqa: BLE001
                item["ok"], item["error"] = False, f"{type(e).__name__}: {e}"
                if isinstance(e, LookupError):
                    item["error"] = str(e)
            out["calls"].append(item)
    return out


REPORTERS: dict[str, Callable[[dict[str, Any]], Any]] = {
    "earlier_run_markers": report_earlier_run,
    "env_names": report_env_names,
    "repos": report_repos,
    "cwd": report_cwd,
    "env_fingerprints": report_env_fingerprints,
    "prompt_has_secrets_block": report_prompt_has_secrets_block,
    "user_skills": report_user_skills,
    "skills_loaded": report_skills_loaded,
    "skills_turned_off": report_skills_turned_off,
    "working_copies": report_working_copies,
    "prompt_attempts": report_prompt_attempts,
    "local_tickets": report_local_tickets,
    "git_credentials": report_git_credentials,
    "actions": report_actions,
    "always_on": report_always_on,
    "container": report_container,
    "docker_socket_mounts": report_docker_socket_mounts,
    "weave_env": report_weave_env,  # before "environment": what that sees is after these commands
    "environment": report_environment,
    "mcp": report_mcp,
}


def _git(repo: Path, *args: str) -> str:
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    return (r.stdout or r.stderr).strip()


def build_report(ctx: dict[str, Any]) -> dict[str, Any]:
    report: dict[str, Any] = {"probe_version": PROBE_VERSION}
    for name, fn in REPORTERS.items():
        try:
            report[name] = fn(ctx)
        except Exception as e:  # a broken reporter must not hide the others
            report[name] = {"error": f"{type(e).__name__}: {e}"}
    return report


def leave_markers() -> None:
    for m in MARKERS:
        try:
            m.write_text("probe\n")
        except OSError:
            pass


# --------------------------------------------------------------------------- agent


def parse_script(prompt_text: str) -> dict[str, Any]:
    m = re.search(r"^\s*probe:\s*(\{.*\})\s*$", prompt_text, re.MULTILINE)
    return json.loads(m.group(1)) if m else {"end": "succeed"}


class ProbeAgent:
    def __init__(self) -> None:
        self._conn: Any = None
        self._cwd: str | None = None
        self._commands: list[subprocess.Popen] = []

    def on_connect(self, conn: Any) -> None:
        self._conn = conn

    async def initialize(self, protocol_version: int, **kwargs: Any) -> InitializeResponse:
        return InitializeResponse(
            protocol_version=protocol_version,
            agent_info=Implementation(name="weave-probe", version=PROBE_VERSION),
        )

    async def new_session(self, cwd: str, **kwargs: Any) -> NewSessionResponse:
        self._cwd = cwd
        return NewSessionResponse(session_id=str(uuid.uuid4()))

    async def authenticate(self, method_id: str, **kwargs: Any) -> None:
        return None

    async def set_session_mode(self, session_id: str, mode_id: str, **kwargs: Any) -> None:
        return None

    async def cancel(self, session_id: str, **kwargs: Any) -> None:
        # Deliberately like Claude Code: an interrupt leaves commands running.
        return None

    async def _send(self, session_id: str, update: Any) -> None:
        await self._conn.session_update(session_id=session_id, update=update)

    async def prompt(self, session_id: str, prompt: list[Any], **kwargs: Any) -> PromptResponse:
        text = "\n".join(getattr(b, "text", "") or "" for b in prompt)
        _count_attempt()
        script = parse_script(text)
        actions = run_actions(script, text)
        report = build_report({"cwd": self._cwd, "script": script, "prompt": text, "actions": actions})
        leave_markers()
        await self._send(
            session_id,
            start_tool_call(
                f"probe-report-{uuid.uuid4().hex[:8]}",
                "probe-report",
                kind="other",
                status="completed",
                raw_output=json.dumps(report),
            ),
        )

        end = script.get("end", "succeed")
        if end == "succeed":
            await self._send(session_id, update_agent_message_text("Probe done.\nRUN-OUTCOME: done\n"))
            return PromptResponse(stop_reason="end_turn")
        if end == "give-up":
            reason = script.get("reason", "the probe was told to give up")
            await self._send(session_id, update_agent_message_text(f"RUN-OUTCOME: gave-up: {reason}\n"))
            return PromptResponse(stop_reason="end_turn")
        if end == "reply":
            await self._send(session_id, update_agent_message_text(script.get("text", "")))
            return PromptResponse(stop_reason="end_turn")
        if end == "hang":
            command = script.get("command", "sleep 600")
            proc = subprocess.Popen(["bash", "-c", command], cwd=self._cwd, start_new_session=True)
            self._commands.append(proc)
            await self._send(
                session_id,
                start_tool_call(
                    f"hang-{proc.pid}", f"hanging command: {command}", kind="execute",
                    status="in_progress", raw_input={"command": command, "pid": proc.pid},
                ),
            )
            while proc.poll() is None:
                await asyncio.sleep(0.5)
            return PromptResponse(stop_reason="end_turn")
        if end == "error":
            kind = script.get("errorKind", "authentication_failed")
            message = script.get("message", f"the probe was told to fail with {kind}")
            # Exactly how claude-agent-acp fails a turn: RequestError.internalError({errorKind}, resultText).
            code = int(script.get("code", -32603))
            raise RequestError(code, message, {"errorKind": kind} if kind else None)
        raise ValueError(f"unknown probe end: {end!r}")

    def kill_commands(self) -> None:
        for proc in self._commands:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


async def main() -> None:
    agent = ProbeAgent()
    loop = asyncio.get_running_loop()

    def stop(*_: Any) -> None:
        # Closing the agent (what a cancel does) stops everything it started.
        agent.kill_commands()
        os._exit(0)

    loop.add_signal_handler(signal.SIGTERM, stop)
    loop.add_signal_handler(signal.SIGINT, stop)
    try:
        await run_agent(agent)
    finally:
        agent.kill_commands()


if __name__ == "__main__":
    asyncio.run(main())
    sys.exit(0)
