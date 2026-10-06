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
    ) -> None:
        self.run_id = run_id
        self._api_key = secrets.token_urlsafe(24)
        port = _free_port()
        cmd = [
            "run", "-d", "--rm",
            "--name", f"weave-run-{run_id}",
            "--label", f"{LABEL_RUN_ID}={run_id}",
            "--label", f"{LABEL_PRODUCT}={product}",
            "-p", f"127.0.0.1:{port}:8000",
            "-e", f"OH_SESSION_API_KEYS_0={self._api_key}",
        ]
        if nofile_limit:
            cmd += ["--ulimit", f"nofile={nofile_limit}:{nofile_limit}"]
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
            state = _docker("inspect", "-f", "{{.State.Running}}", self.container_id).stdout.strip()
            if state != "true":
                logs = _docker("logs", "--tail", "50", self.container_id)
                raise SandboxError(f"sandbox stopped while starting: {logs.stdout[-2000:]}{logs.stderr[-2000:]}")
            time.sleep(0.5)
        raise SandboxError(f"sandbox did not become healthy within {timeout:.0f}s")

    def sh(self, command: str, timeout: float = 120, cwd: str = WORKDIR) -> str:
        r = self.workspace.execute_command(command, cwd=cwd, timeout=timeout)
        if r.exit_code != 0:
            raise SandboxError(f"`{command}` failed ({r.exit_code}): {(r.stderr or r.stdout or '').strip()[-1000:]}")
        return r.stdout or ""

    def put_repo(self, source: str, name: str, base_branch: str, integration_branch: str) -> None:
        """Clone a Repo from the Sandbox host into the sandbox and check out its Integration branch."""
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

    def destroy(self) -> None:
        _docker("rm", "-f", self.container_id, timeout=60)
