"""Spike for "Prove Cursor and Claude Code subscription agents run unattended under OpenHands".

Runs each subscription agent through OpenHands' ACPAgent inside a DockerWorkspace
and checks, with no human at the keyboard:

  1. auth        the agent answers a prompt using only a token passed as a
                 conversation secret (no interactive login)
  2. permissions the agent runs a shell command and writes a file with nobody
                 approving it
  3. skills      which staged skill folders / always-on files the agent actually
                 loads (one canary per candidate folder)
  4. cancel      interrupting a run kills the agent's in-flight command, and
                 leaving the workspace removes the container
  5. errors      every ConversationErrorEvent seen, verbatim (redacted by the SDK),
                 so the wrapper can learn what auth / quota failures look like

Usage (see README.md):
    CLAUDE_CODE_OAUTH_TOKEN=... CURSOR_API_KEY=... python spike.py --agent all

Writes results-<agent>.json next to this file. Never prints secrets.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

os.environ.setdefault("OPENHANDS_SUPPRESS_BANNER", "1")

from openhands.sdk import Conversation  # noqa: E402
from openhands.sdk.agent import ACPAgent  # noqa: E402
from openhands.sdk.conversation.response_utils import get_agent_final_response  # noqa: E402
from openhands.workspace import DockerWorkspace  # noqa: E402

HERE = Path(__file__).resolve().parent
IMAGE = os.environ.get("SPIKE_IMAGE", "weave-spike-agent-server")
WORKDIR = "/workspace"


@dataclass
class AgentSpec:
    key: str
    acp_command: list[str]
    secret_env: str  # env var holding the subscription credential
    session_mode: str | None
    version_cmd: str


AGENTS: dict[str, AgentSpec] = {
    "claude-code": AgentSpec(
        key="claude-code",
        acp_command=["claude-agent-acp"],
        secret_env="CLAUDE_CODE_OAUTH_TOKEN",  # from `claude setup-token`
        session_mode=None,  # SDK auto-selects bypassPermissions
        version_cmd="claude-agent-acp --version 2>&1 | head -1; claude --version 2>&1 | head -1",
    ),
    "cursor": AgentSpec(
        key="cursor",
        acp_command=["agent", "acp"],
        secret_env="CURSOR_API_KEY",  # from the Cursor dashboard
        session_mode="agent",
        version_cmd="agent --version 2>&1 | head -1",
    ),
}

# One canary per place a skill or always-on file might be read from. Each tells
# the agent to append its own token to canary.txt, so the file shows exactly
# which locations the agent loaded.
SKILL_DIRS = {
    ".claude/skills": "CANARY-CLAUDE-DIR",
    ".agents/skills": "CANARY-AGENTS-DIR",
    ".cursor/skills": "CANARY-CURSOR-DIR",
}
ALWAYS_ON_FILES = {
    "CLAUDE.md": "ALWAYS-ON-CLAUDE-MD",
    "AGENTS.md": "ALWAYS-ON-AGENTS-MD",
}


@dataclass
class Result:
    agent: str
    started_at: str = field(default_factory=lambda: time.strftime("%Y-%m-%dT%H:%M:%S%z"))
    versions: str = ""
    checks: dict[str, Any] = field(default_factory=dict)
    error_events: list[dict[str, str]] = field(default_factory=list)
    container_removed: bool | None = None


# --------------------------------------------------------------------------- helpers


def sh(ws: DockerWorkspace, cmd: str, timeout: float = 60) -> str:
    r = ws.execute_command(cmd, cwd=WORKDIR, timeout=timeout)
    return (r.stdout or "") + (r.stderr or "")


def stage(ws: DockerWorkspace) -> None:
    """Lay out a tiny git repo with every canary skill and always-on file."""
    sh(ws, "git init -q . 2>/dev/null || true; git config user.email spike@example.com; git config user.name spike")
    for folder, token in SKILL_DIRS.items():
        name = "canary-" + folder.strip(".").split("/")[0]
        body = (
            f"---\nname: {name}\n"
            f"description: Spike canary skill. Use when asked to run the canary skills.\n---\n\n"
            f"Append exactly one line `{token}` to the file canary.txt in the working directory "
            f"(create it if missing). Do nothing else.\n"
        )
        ws.file_upload(body.encode(), f"{WORKDIR}/{folder}/{name}/SKILL.md")
    for fname, token in ALWAYS_ON_FILES.items():
        body = (
            "# Spike instructions\n\n"
            f"Whenever you are asked to run the canary skills, also append exactly one line "
            f"`{token}` to canary.txt in the working directory.\n"
        )
        ws.file_upload(body.encode(), f"{WORKDIR}/{fname}")
    sh(ws, "git add -A && git commit -qm spike-fixtures || true")


def new_conversation(ws: DockerWorkspace, spec: AgentSpec, token: str):
    agent = ACPAgent(
        acp_command=spec.acp_command,
        acp_session_mode=spec.session_mode,
        acp_startup_timeout=180.0,
        acp_prompt_timeout=900.0,
    )
    # Passed as a conversation secret: becomes an env var of the ACP subprocess
    # only, never baked into the image, masked in any output.
    return Conversation(agent=agent, workspace=ws, secrets={spec.secret_env: token})


def collect_errors(conv, res: Result) -> None:
    for ev in conv.state.events:
        if type(ev).__name__ == "ConversationErrorEvent":
            item = {"code": getattr(ev, "code", ""), "detail": getattr(ev, "detail", "")}
            if item not in res.error_events:
                res.error_events.append(item)


def ask(ws, spec, token, res, prompt: str, timeout: float = 900) -> tuple[str, str | None]:
    conv = new_conversation(ws, spec, token)
    try:
        conv.send_message(prompt)
        t0 = time.time()
        conv.run(timeout=timeout)
        reply = get_agent_final_response(conv.state.events) or ""
        return reply, f"{time.time() - t0:.1f}s"
    except Exception as e:  # recorded, not raised: we want every check's outcome
        return "", f"ERROR {type(e).__name__}: {e}"[:600]
    finally:
        collect_errors(conv, res)
        try:
            conv.close()
        except Exception:
            pass


# --------------------------------------------------------------------------- checks


def check_auth(ws, spec, token, res):
    reply, info = ask(ws, spec, token, res, "Reply with exactly: SPIKE-OK")
    res.checks["auth"] = {"pass": "SPIKE-OK" in reply, "reply": reply[:200], "info": info}


def check_permissions(ws, spec, token, res):
    reply, info = ask(
        ws, spec, token, res,
        "Use your shell/terminal tool to run exactly: echo perm-ok > perm.txt && date >> perm.txt "
        "Then reply DONE.",
    )
    on_disk = sh(ws, "cat perm.txt 2>&1")
    res.checks["permissions"] = {
        "pass": "perm-ok" in on_disk,
        "file": on_disk.strip()[:200],
        "reply": reply[:200],
        "info": info,
    }


def check_skills(ws, spec, token, res):
    sh(ws, "rm -f canary.txt")
    reply, info = ask(
        ws, spec, token, res,
        "Run the canary skills: find every skill available to you whose name starts with "
        "'canary-' and follow each one's instructions exactly. Then reply with the list of "
        "skill names you found.",
    )
    canary = sh(ws, "cat canary.txt 2>&1")
    loaded = {k: (v in canary) for k, v in {**SKILL_DIRS, **ALWAYS_ON_FILES}.items()}
    res.checks["skills"] = {
        "pass": any(loaded[d] for d in SKILL_DIRS),
        "loaded": loaded,
        "reply": reply[:400],
        "info": info,
    }


def check_cancel(ws, spec, token, res):
    conv = new_conversation(ws, spec, token)
    out: dict[str, Any] = {}
    try:
        conv.send_message(
            "Use your shell tool to run exactly this command and wait for it to finish: "
            "sleep 901 && echo late > late.txt"
        )
        conv.run(blocking=False)
        seen = False
        for _ in range(60):  # up to ~3 minutes for the agent to start the command
            if "sleep 901" in sh(ws, "ps -eo pid,args | grep -v grep | grep 'sleep 901' || true"):
                seen = True
                break
            time.sleep(3)
        out["sleep_started"] = seen
        out["agent_procs_before"] = sh(ws, "ps -eo pid,args | grep -Ei 'claude|agent acp|cursor' | grep -v grep || true")[:800]
        t0 = time.time()
        conv.interrupt()
        status = None
        for _ in range(30):
            status = str(conv.state.execution_status)
            if "RUNNING" not in status.upper():
                break
            time.sleep(2)
        out["status_after_interrupt"] = status
        out["seconds_to_stop"] = round(time.time() - t0, 1)
        time.sleep(5)
        out["sleep_still_running"] = "sleep 901" in sh(ws, "ps -eo pid,args | grep -v grep | grep 'sleep 901' || true")
        out["agent_procs_after"] = sh(ws, "ps -eo pid,args | grep -Ei 'claude|agent acp|cursor' | grep -v grep || true")[:800]
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"[:600]
    finally:
        collect_errors(conv, res)
        try:
            conv.close()
        except Exception:
            pass
    out["agent_procs_after_close"] = sh(ws, "ps -eo pid,args | grep -Ei 'claude|agent acp|cursor' | grep -v grep || true")[:800]
    out["pass"] = bool(out.get("sleep_started")) and out.get("sleep_still_running") is False
    res.checks["cancel"] = out


def check_bad_token(ws, spec, res):
    """What an auth failure looks like, so the wrapper can tell it from quota."""
    reply, info = ask(ws, spec, "invalid-token-for-spike", res, "Reply with exactly: SPIKE-OK", timeout=240)
    res.checks["bad_token"] = {"reply": reply[:200], "info": info}


# --------------------------------------------------------------------------- main


def run_agent(spec: AgentSpec) -> Result:
    token = os.environ.get(spec.secret_env)
    if not token:
        sys.exit(f"{spec.secret_env} is not set; see README.md")
    res = Result(agent=spec.key)
    container_id = None
    try:
        with DockerWorkspace(server_image=IMAGE, working_dir=WORKDIR) as ws:
            container_id = ws._container_id
            res.versions = sh(ws, spec.version_cmd).strip()
            stage(ws)
            for check in (check_auth, check_permissions, check_skills, check_cancel):
                print(f"[{spec.key}] {check.__name__} ...", flush=True)
                try:
                    check(ws, spec, token, res)
                except Exception:
                    res.checks[check.__name__.removeprefix("check_")] = {"error": traceback.format_exc()[-800:]}
                print(f"[{spec.key}]   -> {res.checks.get(check.__name__.removeprefix('check_'), {}).get('pass')}", flush=True)
            print(f"[{spec.key}] check_bad_token ...", flush=True)
            check_bad_token(ws, spec, res)
    finally:
        if container_id:
            left = subprocess.run(
                ["docker", "ps", "-aq", "--filter", f"id={container_id}"],
                capture_output=True, text=True,
            ).stdout.strip()
            res.container_removed = left == ""
    return res


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--agent", choices=[*AGENTS, "all"], default="all")
    args = p.parse_args()
    keys = list(AGENTS) if args.agent == "all" else [args.agent]
    for key in keys:
        res = run_agent(AGENTS[key])
        out = HERE / f"results-{key}.json"
        out.write_text(json.dumps(res.__dict__, indent=2, default=str))
        print(f"wrote {out}")


if __name__ == "__main__":
    main()
