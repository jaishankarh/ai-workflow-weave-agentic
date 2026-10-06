"""The sandbox's only git remote: a push gateway on the Sandbox host (ADR 0009).

An agent can read any credential its sandbox holds (#13), so the rule "push
only the run's Integration branches" cannot rest on the agent. Instead each
Repo in the sandbox has one remote, `origin`, pointing at this gateway, and the
only git credential in the sandbox is a random per-run token in that URL.

For each run the gateway keeps a bare mirror of each Repo (cloned from its
`source`). The mirror's `pre-receive` hook accepts an update only to
`refs/heads/<that Repo's Integration branch>` (no deletes, no tags, no other
branch), and before accepting it pushes the same commit onward to the Repo's
`source` with the Sandbox host's own git credentials. So a push is either
refused, or lands on the real remote's Integration branch. The run token stops
working when the run ends.

The gateway speaks git's smart HTTP protocol through `git http-backend`. It
listens on an address sandboxes can reach (the Docker bridge's gateway by
default); sandboxes reach it as `GATEWAY_HOST` (`--add-host ...:host-gateway`).
"""

from __future__ import annotations

import os
import secrets
import shlex
import shutil
import subprocess
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

GATEWAY_HOST = "weave-git"

_HOOK = """#!/bin/sh
# Accept only the run's Integration branch, and only once it is on the Repo's real remote.
set -u
status=0
while read -r old new ref; do
  if [ "$ref" != {ref} ]; then
    echo "weave: refused $ref: this run may push only {ref}" >&2
    status=1; continue
  fi
  if [ "$new" = 0000000000000000000000000000000000000000 ]; then
    echo "weave: refused deleting $ref" >&2
    status=1; continue
  fi
  # Leave git's quarantine so the onward push sees the objects just received.
  if ! env -u GIT_QUARANTINE_PATH -u GIT_DIR git --git-dir="$PWD" push --quiet {upstream} "$new:$ref" >&2; then
    echo "weave: the Repo's remote refused $ref" >&2
    status=1
  fi
done
exit $status
"""


class PushGatewayError(RuntimeError):
    pass


def default_bind_address() -> str:
    """The Docker bridge network's gateway: reachable from sandboxes, not from elsewhere."""
    r = subprocess.run(
        ["docker", "network", "inspect", "bridge", "-f", "{{(index .IPAM.Config 0).Gateway}}"],
        capture_output=True, text=True,
    )
    addr = r.stdout.strip()
    if r.returncode != 0 or not addr:
        raise PushGatewayError(f"cannot find the Docker bridge gateway: {r.stderr.strip()}")
    return addr


@dataclass(frozen=True)
class RunRemotes:
    token: str
    root: Path
    base_url: str

    def url(self, repo: str) -> str:
        """The `origin` URL a Repo's working copy in the sandbox gets."""
        return f"{self.base_url}/{self.token}/{repo}.git"


class PushGateway:
    def __init__(self, bind_address: str | None = None, port: int = 0) -> None:
        self._bind = bind_address
        self._port = port
        self._runs: dict[str, Path] = {}
        self._lock = threading.Lock()
        self._server: ThreadingHTTPServer | None = None

    # ------------------------------------------------------------------ lifecycle

    def _ensure_started(self) -> ThreadingHTTPServer:
        with self._lock:
            if self._server is None:
                bind = self._bind or default_bind_address()
                server = ThreadingHTTPServer((bind, self._port), _handler(self))
                server.daemon_threads = True
                threading.Thread(target=server.serve_forever, name="push-gateway", daemon=True).start()
                self._server = server
            return self._server

    @property
    def port(self) -> int:
        return self._ensure_started().server_address[1]

    def shutdown(self) -> None:
        with self._lock:
            server, self._server = self._server, None
        if server is not None:
            server.shutdown()
            server.server_close()

    # ------------------------------------------------------------------ runs

    def open_run(self, root: Path, repos: dict[str, tuple[str, str]]) -> RunRemotes:
        """Mirror each Repo (name -> (source, Integration branch)) under `root` and
        return the remotes a run's sandbox gets."""
        root.mkdir(parents=True, exist_ok=True)
        for name, (source, branch) in repos.items():
            mirror = root / f"{name}.git"
            r = subprocess.run(["git", "clone", "-q", "--bare", source, str(mirror)], capture_output=True, text=True)
            if r.returncode != 0:
                raise PushGatewayError(f"cannot mirror Repo {name} from {source}: {r.stderr.strip()}")
            git = lambda *a: subprocess.run(["git", "--git-dir", str(mirror), *a], check=True)  # noqa: E731
            git("config", "http.receivepack", "true")
            git("config", "receive.denyDeletes", "true")
            hook = mirror / "hooks" / "pre-receive"
            hook.write_text(_HOOK.format(ref=shlex.quote(f"refs/heads/{branch}"), upstream=shlex.quote(source)))
            hook.chmod(0o755)
        token = secrets.token_urlsafe(24)
        with self._lock:
            self._runs[token] = root
        return RunRemotes(token=token, root=root, base_url=f"http://{GATEWAY_HOST}:{self.port}")

    def close_run(self, remotes: RunRemotes) -> None:
        """Revoke the run's token and drop its mirrors."""
        with self._lock:
            self._runs.pop(remotes.token, None)
        shutil.rmtree(remotes.root, ignore_errors=True)

    def _root_for(self, token: str) -> Path | None:
        with self._lock:
            return self._runs.get(token)


def _handler(gateway: PushGateway) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args: object) -> None:  # quiet
            pass

        def do_GET(self) -> None:
            self._serve()

        def do_POST(self) -> None:
            self._serve()

        def _deny(self, code: int, text: str) -> None:
            body = (text + "\n").encode()
            self.send_response(code)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _body(self) -> bytes:
            if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
                out = bytearray()
                while True:
                    size = int(self.rfile.readline().split(b";")[0].strip(), 16)
                    if size == 0:
                        self.rfile.readline()
                        return bytes(out)
                    out += self.rfile.read(size)
                    self.rfile.readline()
            return self.rfile.read(int(self.headers.get("Content-Length") or 0))

        def _serve(self) -> None:
            path, _, query = self.path.partition("?")
            token, _, rest = path.lstrip("/").partition("/")
            root = gateway._root_for(token)
            repo = rest.split("/", 1)[0]
            if root is None or not repo.endswith(".git") or not (root / repo).is_dir():
                return self._deny(404, "not found")
            body = self._body() if self.command == "POST" else b""
            env = {
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "HOME": os.environ.get("HOME", "/"),
                "GIT_PROJECT_ROOT": str(root),
                "GIT_HTTP_EXPORT_ALL": "1",
                "REMOTE_USER": "weave-run",
                "REQUEST_METHOD": self.command,
                "PATH_INFO": "/" + rest,
                "QUERY_STRING": query,
                "CONTENT_TYPE": self.headers.get("Content-Type", ""),
                "CONTENT_LENGTH": str(len(body)),
                "GIT_PROTOCOL": self.headers.get("Git-Protocol", ""),
            }
            if enc := self.headers.get("Content-Encoding"):
                env["HTTP_CONTENT_ENCODING"] = enc
            r = subprocess.run(["git", "http-backend"], input=body, env=env, capture_output=True)
            head, _, payload = r.stdout.partition(b"\r\n\r\n")
            if not _:
                head, _, payload = r.stdout.partition(b"\n\n")
            status, headers = 200, []
            for line in head.decode("latin-1").splitlines():
                k, _, v = line.partition(":")
                if k.lower() == "status":
                    status = int(v.strip().split()[0])
                elif k:
                    headers.append((k, v.strip()))
            self.send_response(status)
            for k, v in headers:
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    return Handler
