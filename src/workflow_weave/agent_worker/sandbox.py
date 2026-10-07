"""One throwaway Docker sandbox running the OpenHands agent-server.

We start the container ourselves rather than through `DockerWorkspace` so it
carries labels (which run, which Product) the Sandbox host can be queried by,
and so the open-files limit is a setting, then talk to it through the SDK's
`RemoteWorkspace` as `DockerWorkspace` does.
"""

from __future__ import annotations

import json
import secrets
import shlex
import socket
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Callable
from urllib.request import urlopen

from openhands.sdk.workspace import RemoteWorkspace

WORKDIR = "/workspace"
LABEL_RUN_ID = "weave.run-id"
LABEL_PRODUCT = "weave.product"


# The runtime that lets a sandbox run its own Docker engine without the host's socket and without
# --privileged (ADR 0004). The sandbox's entrypoint starts the engine when this env var is set.
SYSBOX_RUNTIME = "sysbox-runc"
START_DOCKERD_ENV = "WEAVE_START_DOCKERD"
# How long teardown waits for the Sandbox host to finish removing a sandbox.
REMOVAL_TIMEOUT = 60.0
REMOVAL_POLL_INTERVAL = 0.5


class SandboxError(RuntimeError):
    pass


class MissingRuntimeError(SandboxError):
    """The Sandbox host cannot start sandboxes on the runtime they need."""


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _docker(*args: str, timeout: float = 120) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)


def require_runtime(runtime: str, docker: Callable[..., subprocess.CompletedProcess] = _docker) -> None:
    """Raise MissingRuntimeError, naming `runtime`, unless the Sandbox host's Docker lists it.

    Asked before a sandbox is started, so a host without sysbox fails the run plainly (an
    infra-failure) rather than as a puzzling `docker run` error.
    """
    fix = f"install the {runtime} runtime on the Sandbox host and register it with Docker"
    try:
        r = docker("info", "--format", "{{json .Runtimes}}", timeout=30)
    except Exception as e:
        raise MissingRuntimeError(f"cannot check the Sandbox host for the {runtime} runtime: {type(e).__name__}: {e}") from e
    if r.returncode != 0:
        raise MissingRuntimeError(
            f"cannot check the Sandbox host for the {runtime} runtime: {(r.stderr or r.stdout).strip()[-500:]}"
        )
    try:
        runtimes = json.loads(r.stdout or "{}")
    except ValueError as e:
        raise MissingRuntimeError(f"cannot read the Sandbox host's Docker runtimes for {runtime}: {r.stdout[:200]!r}") from e
    if runtime not in runtimes:
        raise MissingRuntimeError(
            f"the Sandbox host has no {runtime} runtime (Docker lists: {sorted(runtimes) or 'none'}); {fix}"
        )


def sandbox_run_command(
    *,
    image: str,
    run_id: str,
    product: str,
    api_key: str,
    port: int,
    env: dict[str, str],
    nofile_limit: int | None,
    mounts: list[tuple[str, str]],
    extra_hosts: list[str],
    runtime: str | None,
) -> list[str]:
    """The `docker run` arguments (after `docker`) that start a sandbox.

    With a `runtime` (sysbox) the sandbox also starts its own Docker engine. Never `--privileged`
    and never the host's Docker socket (ADR 0004): the runtime is what makes that safe.
    """
    cmd = [
        # No --rm: a sandbox that dies keeps its logs for the run's reason; destroy() removes it.
        "run", "-d",
        "--name", f"weave-run-{run_id}",
        "--label", f"{LABEL_RUN_ID}={run_id}",
        "--label", f"{LABEL_PRODUCT}={product}",
        "-p", f"127.0.0.1:{port}:8000",
        "-e", f"OH_SESSION_API_KEYS_0={api_key}",
    ]
    if runtime:
        cmd += ["--runtime", runtime, "-e", f"{START_DOCKERD_ENV}=1"]
    if nofile_limit:
        cmd += ["--ulimit", f"nofile={nofile_limit}:{nofile_limit}"]
    for host_path, sandbox_path in mounts:
        cmd += ["--mount", f"type=bind,source={host_path},target={sandbox_path},readonly"]
    for h in extra_hosts:
        cmd += ["--add-host", h]
    for k, v in env.items():
        cmd += ["-e", f"{k}={v}"]
    cmd += [image, "--host", "0.0.0.0", "--port", "8000"]
    return cmd


def remove_sandbox(
    container_id: str,
    *,
    timeout: float = REMOVAL_TIMEOUT,
    interval: float = REMOVAL_POLL_INTERVAL,
    docker: Callable[..., subprocess.CompletedProcess] = _docker,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """Remove a sandbox and return only once the Sandbox host no longer has it.

    `docker rm -f` can return before a sysbox container (and the engine inside it) is fully gone,
    so teardown polls until `docker inspect` says there is no such container. A Docker that cannot
    answer is not taken as "gone".
    """
    r = docker("rm", "-f", container_id, timeout=60)
    if r.returncode != 0:
        raise SandboxError(f"cannot remove sandbox {container_id}: {r.stderr.strip()}")
    deadline = clock() + timeout
    last = "still present"
    while True:
        r = docker("inspect", container_id, timeout=30)
        if r.returncode != 0 and "no such" in (r.stderr or "").lower():
            return
        last = "still present" if r.returncode == 0 else f"Docker could not answer: {(r.stderr or '').strip()[-200:]}"
        if clock() >= deadline:
            raise SandboxError(
                f"sandbox {container_id} is still on the Sandbox host {timeout:.0f}s after its removal was asked for ({last})"
            )
        sleep(interval)


class Sandbox:
    def __init__(
        self,
        *,
        image: str,
        run_id: str,
        product: str,
        env: dict[str, str],
        nofile_limit: int | None,
        start_timeout: float,
        mounts: list[tuple[str, str]] = (),
        extra_hosts: list[str] = (),
        runtime: str | None = None,
    ) -> None:
        """`mounts` are (host path, sandbox path) pairs, mounted read-only. `runtime` (sysbox)
        gives the sandbox its own Docker engine; None leaves the default runtime, unchanged."""
        self.run_id = run_id
        self._api_key = secrets.token_urlsafe(24)
        port = _free_port()
        cmd = sandbox_run_command(
            image=image, run_id=run_id, product=product, api_key=self._api_key, port=port, env=env,
            nofile_limit=nofile_limit, mounts=list(mounts), extra_hosts=list(extra_hosts), runtime=runtime,
        )
        proc = _docker(*cmd)
        if proc.returncode != 0:
            raise SandboxError(f"sandbox failed to start: {proc.stderr.strip()}")
        self.container_id = proc.stdout.strip()
        self.host = f"http://127.0.0.1:{port}"
        try:
            self._wait_healthy(start_timeout)
        except Exception as e:
            try:
                self.destroy()
            except Exception as gone:
                raise SandboxError(f"{e}; and the sandbox could not be removed: {gone}") from e
            raise
        self.workspace = RemoteWorkspace(host=self.host, api_key=self._api_key, working_dir=WORKDIR)

    def _wait_healthy(self, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        last_error = "no answer yet"
        while time.monotonic() < deadline:
            try:
                with urlopen(f"{self.host}/health", timeout=1.0):
                    return
            except Exception as e:  # not up yet: keep polling, and keep the error for the reason
                last_error = f"{type(e).__name__}: {e}"
            if how := self.stopped():
                raise SandboxError(f"{how} while starting: {self.last_logs()}")
            time.sleep(0.5)
        raise SandboxError(
            f"sandbox did not become healthy within {timeout:.0f}s (last health check: {last_error})"
        )

    def sh(self, command: str, timeout: float = 120, cwd: str = WORKDIR) -> str:
        r = self.workspace.execute_command(command, cwd=cwd, timeout=timeout)
        if r.exit_code != 0:
            raise SandboxError(f"`{command}` failed ({r.exit_code}): {(r.stderr or r.stdout or '').strip()[-1000:]}")
        return r.stdout or ""

    def run(self, command: str, timeout: float = 120, cwd: str = WORKDIR) -> tuple[int, str]:
        """Run a command in the sandbox; its exit code and output (stdout, then stderr), however it exits."""
        r = self.workspace.execute_command(command, cwd=cwd, timeout=timeout)
        return r.exit_code, (r.stdout or "") + (r.stderr or "")

    def put_text(self, path: str, text: str) -> None:
        """Write a text file inside the sandbox (creating its folder)."""
        directory = path.rsplit("/", 1)[0] or "/"
        self.sh(f"mkdir -p {shlex.quote(directory)}", cwd="/")
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp) / "upload"
            local.write_text(text)
            self.workspace.file_upload(local, path)

    def put_repo(
        self, source: str, name: str, base_branch: str, integration_branch: str, remote_url: str,
        base_ref: str | None = None,
    ) -> None:
        """Clone a Repo from the Sandbox host into the sandbox, start its Integration branch
        from the Base branch as read at `base_ref` in `source` (default: the local branch),
        and make `remote_url` (the push gateway) its only remote."""
        with tempfile.TemporaryDirectory() as tmp:
            bundle = Path(tmp) / f"{name}.bundle"
            r = subprocess.run(["git", "-C", source, "bundle", "create", str(bundle), "--all"], capture_output=True, text=True)
            if r.returncode != 0:
                raise SandboxError(f"cannot read Repo {name} at {source}: {r.stderr.strip()}")
            remote_bundle = f"/tmp/weave-repos/{name}.bundle"
            self.workspace.file_upload(bundle, remote_bundle)
        dest = f"{WORKDIR}/{name}"
        self.sh(f"git clone -q {remote_bundle} {dest}")
        ref = shlex.quote(base_ref or f"refs/heads/{base_branch}")
        self.sh(f"git fetch -q {remote_bundle} {ref} && git checkout -q -B {base_branch} FETCH_HEAD", cwd=dest)
        self.sh(f"git checkout -q -B {integration_branch}", cwd=dest)
        self.sh(f"git remote set-url origin {remote_url} && rm -f {remote_bundle}", cwd=dest)

    def put_checkout(self, source: str, name: str, ref: str, dest: str) -> None:
        """Check out a Repo the run does not work on, read at `ref` in `source`, at `dest`: a Repo
        pulled in by another recipe's `depends_on` (its Base branch). No remote is kept and nothing
        can be pushed from it (ADR 0009)."""
        with tempfile.TemporaryDirectory() as tmp:
            bundle = Path(tmp) / f"{name}.bundle"
            r = subprocess.run(["git", "-C", source, "bundle", "create", str(bundle), "--all"], capture_output=True, text=True)
            if r.returncode != 0:
                raise SandboxError(f"cannot read Repo {name} at {source}: {r.stderr.strip()}")
            remote_bundle = f"/tmp/weave-repos/{name}.dependency.bundle"
            self.workspace.file_upload(bundle, remote_bundle)
        self.sh(f"mkdir -p {shlex.quote(dest)} && git init -q {shlex.quote(dest)}", cwd="/")
        self.sh(f"git fetch -q {remote_bundle} {shlex.quote(ref)} && git checkout -q --detach FETCH_HEAD && rm -f {remote_bundle}",
                cwd=dest)

    def put_task_branch_checkout(self, name: str, integration_branch: str, base_branch: str, dest: str) -> bool:
        """Check out, at `dest`, the Task branch (the Integration branch) of a Repo the Story
        touches but the run is not working on, apart from the agent's working copy so the agent's
        edits are not what that Repo runs from. A Story's first Task there has no branch yet: then it
        is the Base branch, which is what the branch would be. Returns whether the branch was found."""
        src = f"{WORKDIR}/{name}"
        branch = shlex.quote(f"refs/remotes/origin/{integration_branch}")
        found = self.run(f"git rev-parse -q --verify {branch}", cwd=src)[0] == 0
        ref = branch if found else shlex.quote(f"refs/heads/{base_branch}")
        self.sh(f"mkdir -p {shlex.quote(dest)} && git init -q {shlex.quote(dest)}", cwd="/")
        self.sh(f"git fetch -q {shlex.quote(src)} {ref} && git checkout -q --detach FETCH_HEAD", cwd=dest)
        return found

    def put_base_checkout(self, name: str, base_branch: str, dest: str) -> None:
        """Check out, at `dest`, the Base branch of a Repo the Story touches, from its clone in the
        sandbox: what the Environment runs from when it is retried with every Repo at Base (#51).
        Re-running over an earlier checkout at `dest` replaces its files."""
        src = f"{WORKDIR}/{name}"
        self.sh(f"mkdir -p {shlex.quote(dest)} && git init -q {shlex.quote(dest)}", cwd="/")
        self.sh(f"git fetch -q {shlex.quote(src)} {shlex.quote(f'refs/heads/{base_branch}')} && "
                f"git checkout -q -f --detach FETCH_HEAD", cwd=dest)

    def processes(self) -> list[str]:
        """Command lines of live processes, apart from the agent-server itself."""
        r = _docker("exec", self.container_id, "ps", "-eo", "pid=,stat=,args=")
        out = []
        for line in r.stdout.splitlines():
            parts = line.split(None, 2)
            if len(parts) < 3:
                continue
            pid, stat, args = parts
            if pid == "1" or stat.startswith("Z") or args.startswith("ps -eo"):
                continue
            out.append(args)
        return out

    def stopped(self) -> str | None:
        """None while the sandbox is running; otherwise how it stopped."""
        r = _docker("inspect", "-f", "{{.State.Running}} {{.State.ExitCode}} {{.State.OOMKilled}}", self.container_id)
        if r.returncode != 0:
            return "sandbox is gone from the Sandbox host"
        running, code, oom = (r.stdout.split() + ["", "", ""])[:3]
        if running == "true":
            return None
        return f"sandbox stopped (exit code {code}{', out of memory' if oom == 'true' else ''})"

    def last_logs(self, chars: int = 1500) -> str:
        """The end of the agent-server's output, for a reason."""
        logs = _docker("logs", "--tail", "40", self.container_id)
        return (logs.stdout + logs.stderr).strip()[-chars:]

    def destroy(self) -> None:
        """Remove the sandbox; returns only once it is gone from the Sandbox host."""
        remove_sandbox(self.container_id)


# For Claude Code (and the probe), the agent's user-level skills folder.
USER_SKILLS_DIR = "$HOME/.claude/skills"


def stage_skills(sandbox: Sandbox, plan) -> None:
    """Unpack the planned skills into the user-level skills folder; never into a working copy."""
    with tempfile.TemporaryDirectory() as tmp:
        archive = Path(tmp) / "skills.tar"
        archive.write_bytes(plan.tarball())
        remote = "/tmp/weave-staging/skills.tar"
        sandbox.workspace.file_upload(archive, remote)
    sandbox.sh(f'mkdir -p "{USER_SKILLS_DIR}" && tar -xf {remote} -C "{USER_SKILLS_DIR}" && rm -f {remote}', cwd="/")
    if plan.hidden_repo_skills:
        turn_off_skills(sandbox, plan.hidden_repo_skills)


# For Claude Code (and the probe), the agent's user-level folder: the always-on file
# (`CLAUDE.md`) and the staged Coding standards go here.
USER_LEVEL_DIR = "$HOME/.claude"


def stage_user_files(sandbox: Sandbox, make_tarball) -> str:
    """Unpack `make_tarball(<user-level folder's absolute path>)` into the user-level folder.

    Returns that folder's absolute path. Never touches a working copy.
    """
    user_dir = sandbox.sh(f'mkdir -p "{USER_LEVEL_DIR}" && cd "{USER_LEVEL_DIR}" && pwd', cwd="/").strip()
    with tempfile.TemporaryDirectory() as tmp:
        archive = Path(tmp) / "user-files.tar"
        archive.write_bytes(make_tarball(user_dir))
        remote = "/tmp/weave-staging/user-files.tar"
        sandbox.workspace.file_upload(archive, remote)
    sandbox.sh(f'tar -xf {remote} -C "{user_dir}" && rm -f {remote}', cwd="/")
    return user_dir


# Edits the agent's user-level settings (Claude Code's `~/.claude/settings.json`).
_TURN_OFF = """import json, os, sys
path = os.path.expanduser("~/.claude/settings.json")
settings = json.load(open(path)) if os.path.exists(path) else {}
settings.setdefault("skillOverrides", {}).update({name: "off" for name in sys.argv[1:]})
os.makedirs(os.path.dirname(path), exist_ok=True)
json.dump(settings, open(path, "w"), indent=2)
"""


def turn_off_skills(sandbox: Sandbox, names: list[str]) -> None:
    """Turn skills off in the agent's user-level settings (Claude Code's `skillOverrides`):
    not listed to the agent, and refused if it asks for one by name."""
    with tempfile.TemporaryDirectory() as tmp:
        script = Path(tmp) / "turn_off_skills.py"
        script.write_text(_TURN_OFF)
        remote = "/tmp/weave-staging/turn_off_skills.py"
        sandbox.workspace.file_upload(script, remote)
    args = " ".join(shlex.quote(n) for n in names)
    sandbox.sh(f"python3 {remote} {args} && rm -f {remote}", cwd="/")
