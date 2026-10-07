"""One throwaway Docker sandbox running the OpenHands agent-server.

We start the container ourselves rather than through `DockerWorkspace` so it
carries labels (which run, which Product) the Sandbox host can be queried by,
and so the open-files limit is a setting, then talk to it through the SDK's
`RemoteWorkspace` as `DockerWorkspace` does.
"""

from __future__ import annotations

import secrets
import socket
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.request import urlopen

from openhands.sdk.workspace import RemoteWorkspace

WORKDIR = "/workspace"
LABEL_RUN_ID = "weave.run-id"
LABEL_PRODUCT = "weave.product"


class SandboxError(RuntimeError):
    pass


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _docker(*args: str, timeout: float = 120) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)


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
    ) -> None:
        """`mounts` are (host path, sandbox path) pairs, mounted read-only."""
        self.run_id = run_id
        self._api_key = secrets.token_urlsafe(24)
        port = _free_port()
        cmd = [
            # No --rm: a sandbox that dies keeps its logs for the run's reason; destroy() removes it.
            "run", "-d",
            "--name", f"weave-run-{run_id}",
            "--label", f"{LABEL_RUN_ID}={run_id}",
            "--label", f"{LABEL_PRODUCT}={product}",
            "-p", f"127.0.0.1:{port}:8000",
            "-e", f"OH_SESSION_API_KEYS_0={self._api_key}",
        ]
        if nofile_limit:
            cmd += ["--ulimit", f"nofile={nofile_limit}:{nofile_limit}"]
        for host_path, sandbox_path in mounts:
            cmd += ["--mount", f"type=bind,source={host_path},target={sandbox_path},readonly"]
        for h in extra_hosts:
            cmd += ["--add-host", h]
        for k, v in env.items():
            cmd += ["-e", f"{k}={v}"]
        cmd += [image, "--host", "0.0.0.0", "--port", "8000"]
        proc = _docker(*cmd)
        if proc.returncode != 0:
            raise SandboxError(f"sandbox failed to start: {proc.stderr.strip()}")
        self.container_id = proc.stdout.strip()
        self.host = f"http://127.0.0.1:{port}"
        try:
            self._wait_healthy(start_timeout)
        except Exception:
            self.destroy()
            raise
        self.workspace = RemoteWorkspace(host=self.host, api_key=self._api_key, working_dir=WORKDIR)

    def _wait_healthy(self, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                with urlopen(f"{self.host}/health", timeout=1.0):
                    return
            except Exception:
                pass
            if how := self.stopped():
                raise SandboxError(f"{how} while starting: {self.last_logs()}")
            time.sleep(0.5)
        raise SandboxError(f"sandbox did not become healthy within {timeout:.0f}s")

    def sh(self, command: str, timeout: float = 120, cwd: str = WORKDIR) -> str:
        r = self.workspace.execute_command(command, cwd=cwd, timeout=timeout)
        if r.exit_code != 0:
            raise SandboxError(f"`{command}` failed ({r.exit_code}): {(r.stderr or r.stdout or '').strip()[-1000:]}")
        return r.stdout or ""

    def put_repo(self, source: str, name: str, base_branch: str, integration_branch: str, remote_url: str) -> None:
        """Clone a Repo from the Sandbox host into the sandbox, check out its Integration
        branch, and make `remote_url` (the push gateway) its only remote."""
        with tempfile.TemporaryDirectory() as tmp:
            bundle = Path(tmp) / f"{name}.bundle"
            r = subprocess.run(["git", "-C", source, "bundle", "create", str(bundle), "--all"], capture_output=True, text=True)
            if r.returncode != 0:
                raise SandboxError(f"cannot read Repo {name} at {source}: {r.stderr.strip()}")
            remote_bundle = f"/tmp/weave-repos/{name}.bundle"
            self.workspace.file_upload(bundle, remote_bundle)
        dest = f"{WORKDIR}/{name}"
        self.sh(f"git clone -q --branch {base_branch} {remote_bundle} {dest}")
        self.sh(f"git checkout -q -B {integration_branch}", cwd=dest)
        self.sh(f"git remote set-url origin {remote_url} && rm -f {remote_bundle}", cwd=dest)

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
        _docker("rm", "-f", self.container_id, timeout=60)


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
