"""Spike for "Prove an Environment runs nested Docker and an Android emulator under OpenHands".

Starts an OpenHands DockerWorkspace (the agent sandbox) under a chosen container
runtime (sysbox by default) with /dev/kvm passed in, then, inside it and without
the host Docker socket:

  1. sandbox     the workspace starts on the chosen runtime and image; the host
                 socket is not mounted
  2. dockerd     an inner Docker daemon starts
  3. compose     the sample Run recipe's two-service stack builds, comes up, is
                 seeded with a per-run nonce, and serves it
  4. kvm         /dev/kvm reaches the sandbox and the emulator can use it
  5. apk         a React Native release APK builds (x86_64)
  6. emulator    a headless emulator cold-boots
  7. proof       the APK installs, shows the nonce fetched from the stack's
                 backend, and a screenshot is taken and copied out
  8. teardown    after the workspace exits, nothing new is left on the host:
                 containers, volumes, networks, images (bar the sandbox image),
                 emulator processes

Usage (see README.md):
    python spike.py                       # sysbox + KVM, generated sample app
    python spike.py --rn-app ~/code/app   # also time a real app's build/launch
    python spike.py --runtime runc        # control run without sysbox (expected to fail at dockerd)

Writes results-<runtime>.json and proof-<runtime>.png next to this file.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import secrets
import subprocess
import time
import traceback
from pathlib import Path
from typing import Any

os.environ.setdefault("OPENHANDS_SUPPRESS_BANNER", "1")

import openhands.workspace.docker.workspace as oh_docker  # noqa: E402
from openhands.workspace import DockerWorkspace  # noqa: E402
from pydantic import Field  # noqa: E402

HERE = Path(__file__).resolve().parent
RECIPE_DIR = HERE / "recipe"
REMOTE_RECIPE = "/tmp/recipe"


class EnvironmentWorkspace(DockerWorkspace):
    """DockerWorkspace plus `--runtime` and `--device`.

    SDK 1.51.0's DockerWorkspace exposes volumes, network, ports and GPU but no
    runtime or device option, so this injects them into its `docker run` call.
    Whether that is acceptable for the build (vs an upstream option or a daemon
    default-runtime) is one of the things this spike records.
    """

    runtime: str | None = Field(default=None)
    devices: list[str] = Field(default_factory=list)

    def _start_container(self, image: str, context: Any) -> None:
        extra: list[str] = []
        if self.runtime:
            extra += ["--runtime", self.runtime]
        for dev in self.devices:
            extra += ["--device", dev]
        original = oh_docker.execute_command

        def patched(cmd, *args, **kwargs):  # type: ignore[no-untyped-def]
            if isinstance(cmd, list) and cmd[:3] == ["docker", "run", "-d"]:
                cmd = cmd[:3] + extra + cmd[3:]
            return original(cmd, *args, **kwargs)

        oh_docker.execute_command = patched
        try:
            super()._start_container(image, context)
        finally:
            oh_docker.execute_command = original

    @property
    def container_id(self) -> str | None:
        return self._container_id


# ---------------------------------------------------------------- host helpers
def sh(cmd: str) -> str:
    p = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    return (p.stdout + p.stderr).strip()


def host_facts() -> dict[str, Any]:
    return {
        "os": platform.platform(),
        "kernel": sh("uname -r"),
        "cpus": os.cpu_count(),
        "mem_gb": sh("awk '/MemTotal/ {printf \"%.1f\", $2/1048576}' /proc/meminfo"),
        "cpu_virt_flags": sh("grep -m1 -oE 'vmx|svm' /proc/cpuinfo || echo none"),
        "dev_kvm": sh("ls -l /dev/kvm 2>&1"),
        "docker_server": sh("docker version --format '{{.Server.Version}}'"),
        "docker_runtimes": sh("docker info --format '{{json .Runtimes}}'"),
        "docker_default_runtime": sh("docker info --format '{{.DefaultRuntime}}'"),
        "sysbox": sh("sysbox-runc --version 2>&1 | head -3 || echo absent"),
    }


def host_inventory() -> dict[str, set[str]]:
    def ids(cmd: str) -> set[str]:
        return {line for line in sh(cmd).splitlines() if line}

    return {
        "containers": ids("docker ps -aq --no-trunc"),
        "volumes": ids("docker volume ls -q"),
        "networks": ids("docker network ls -q --no-trunc"),
        "images": ids("docker images -q --no-trunc"),
        "emulator_procs": ids("pgrep -f '[q]emu-system|[e]mulator.*-avd' || true"),
    }


# ----------------------------------------------------------- sandbox helpers
def last_json(text: str) -> dict[str, Any] | None:
    for line in reversed(text.strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                return None
    return None


def step(results: dict, name: str, ws: DockerWorkspace, command: str, timeout: float) -> dict:
    t0 = time.monotonic()
    r = ws.execute_command(command, timeout=timeout)
    rec: dict[str, Any] = {
        "seconds": round(time.monotonic() - t0, 1),
        "exit_code": r.exit_code,
        "timed_out": r.timeout_occurred,
    }
    parsed = last_json(r.stdout)
    if parsed is not None:
        rec.update(parsed)
    rec.setdefault("ok", r.exit_code == 0 and not r.timeout_occurred)
    if not rec["ok"]:
        rec["output_tail"] = (r.stdout + "\n" + r.stderr)[-3000:]
    results["checks"][name] = rec
    print(f"[{name}] ok={rec['ok']} {rec['seconds']}s", flush=True)
    return rec


def upload_recipe(ws: DockerWorkspace) -> None:
    for path in RECIPE_DIR.rglob("*"):
        if path.is_file():
            dest = f"{REMOTE_RECIPE}/{path.relative_to(RECIPE_DIR).as_posix()}"
            ws.execute_command(f"mkdir -p {os.path.dirname(dest)}")
            res = ws.file_upload(str(path), dest)
            if not res.success:
                raise RuntimeError(f"upload failed: {path}")


# ------------------------------------------------------------------- the run
def run(args: argparse.Namespace) -> dict[str, Any]:
    nonce = f"hello from the Environment {secrets.token_hex(4)}"
    results: dict[str, Any] = {
        "ticket": "https://github.com/jaishankarh/ai-workflow-weave-agentic/issues/15",
        "started": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "runtime": args.runtime,
        "kvm_passed": not args.no_kvm,
        "image": args.image,
        "rn_app": args.rn_app or "generated sample",
        "host": host_facts(),
        "checks": {},
    }
    before = host_inventory()
    volumes = [f"{Path(args.rn_app).expanduser().resolve()}:/src-app:ro"] if args.rn_app else []

    t0 = time.monotonic()
    ws = EnvironmentWorkspace(
        server_image=args.image,
        runtime=None if args.runtime == "default" else args.runtime,
        devices=[] if args.no_kvm else ["/dev/kvm"],
        volumes=volumes,
        health_check_timeout=300.0,
    )
    container_id = None
    try:
        with ws:
            container_id = ws.container_id
            inspect = json.loads(sh(f"docker inspect {container_id}"))[0]
            mounts = [m.get("Source", "") for m in inspect.get("Mounts", [])]
            results["checks"]["sandbox"] = {
                "ok": True,
                "seconds": round(time.monotonic() - t0, 1),
                "runtime_in_use": inspect["HostConfig"].get("Runtime"),
                "privileged": inspect["HostConfig"].get("Privileged"),
                "devices": inspect["HostConfig"].get("Devices"),
                "host_docker_socket_mounted": any("docker.sock" in m for m in mounts),
                "image_selected": inspect["Config"]["Image"],
                "user": ws.execute_command("id").stdout.strip(),
            }
            print(f"[sandbox] runtime={results['checks']['sandbox']['runtime_in_use']}", flush=True)

            dockerd = step(results, "dockerd", ws, "/opt/spike/start_dockerd.sh", 180)
            if dockerd["ok"]:
                upload_recipe(ws)
                compose = step(
                    results, "compose", ws,
                    f"cd {REMOTE_RECIPE} && docker compose up -d --build --wait --wait-timeout 300 >/tmp/compose.log 2>&1 "
                    f"|| {{ tail -n 40 /tmp/compose.log; exit 1; }}; "
                    f"docker compose exec -T redis redis-cli SET greeting '{nonce}' >/dev/null && "
                    f"for i in $(seq 1 90); do curl -fs localhost:8080/health >/dev/null && break; sleep 2; done; "
                    f"body=$(curl -fs localhost:8080/hello); "
                    f"n=$(docker compose ps --services --status running | wc -l); "
                    f"python3 -c \"import json,sys; b=json.loads(sys.argv[1]); print(json.dumps({{'ok': b.get('greeting')=={nonce!r}, 'services_running': int(sys.argv[2]), 'hello': b}}))\" \"$body\" \"$n\"",
                    900,
                )
                if compose["ok"]:
                    logs = ws.execute_command(f"cd {REMOTE_RECIPE} && docker compose logs --no-color api | tail -n 5")
                    compose["api_log_tail"] = logs.stdout.strip()

            kvm = step(results, "kvm", ws, "/opt/spike/kvm_check.sh", 120)

            apk_src = "/src-app" if args.rn_app else "sample"
            apk = step(results, "apk", ws, f"/opt/spike/build_apk.sh {apk_src}", 3600)

            if kvm["ok"]:
                emu = step(results, "emulator", ws, "/opt/spike/boot_emulator.sh", 900)
                if emu["ok"] and apk["ok"]:
                    expect = "" if args.rn_app else nonce
                    proof = step(
                        results, "proof", ws,
                        f"/opt/spike/prove_app.sh '{apk['apk']}' '{apk['package']}' '{expect}'", 300,
                    )
                    shot = HERE / f"proof-{args.runtime}.png"
                    if ws.file_download("/tmp/proof/screen.png", str(shot)).success:
                        proof["screenshot_file"] = shot.name
            else:
                results["checks"]["emulator"] = {"ok": False, "skipped": "kvm check failed"}

            # In-sandbox teardown first (what the Dispatcher would do), then the
            # workspace's own cleanup on exit.
            ws.execute_command(
                f"adb emu kill >/dev/null 2>&1; cd {REMOTE_RECIPE} 2>/dev/null && docker compose down -v >/dev/null 2>&1; true",
                timeout=120,
            )
    except Exception as exc:  # noqa: BLE001
        results["fatal"] = f"{type(exc).__name__}: {exc}"
        results["traceback"] = traceback.format_exc()[-4000:]

    # ------------------------------------------------------------ teardown check
    # The workspace's cleanup runs `docker stop` on a `--rm` container, and the
    # removal that follows is asynchronous (a sysbox container with inner Docker
    # data can take a while). Poll for removal instead of assuming it, and record
    # how long it took: the Dispatcher's wrapper needs the same wait.
    t_exit = time.monotonic()
    state_while_waiting = ""
    while container_id and time.monotonic() - t_exit < 180:
        if container_id not in host_inventory()["containers"]:
            break
        state_while_waiting = sh(f"docker inspect -f '{{{{.State.Status}}}}' {container_id}")
        time.sleep(2)
    removal_seconds = round(time.monotonic() - t_exit, 1)
    after = host_inventory()
    leftovers = {k: sorted(after[k] - before[k]) for k in before}
    sandbox_image_id = sh(f"docker image inspect -f '{{{{.Id}}}}' {args.image}")
    leftovers["images"] = [i for i in leftovers["images"] if i != sandbox_image_id]
    gone = container_id is None or container_id not in after["containers"]
    results["checks"]["teardown"] = {
        "ok": not any(leftovers.values()),
        "sandbox_container_gone": gone,
        "seconds_until_sandbox_removed": removal_seconds if gone else None,
        "sandbox_state_while_waiting": state_while_waiting,
        "new_on_host": leftovers,
    }
    if not gone:  # record what it was stuck in, then clean the runner up
        results["checks"]["teardown"]["stuck_inspect"] = sh(
            f"docker inspect -f '{{{{json .State}}}}' {container_id}")[:2000]
        sh(f"docker rm -f {container_id}")
    results["finished"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    return results


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runtime", default="sysbox-runc",
                    help="docker runtime for the sandbox: sysbox-runc (default), runc, or 'default' (daemon default)")
    ap.add_argument("--no-kvm", action="store_true", help="don't pass /dev/kvm")
    ap.add_argument("--rn-app", help="path to a real React Native app to build instead of the generated sample")
    ap.add_argument("--image", default=os.environ.get("SPIKE_IMAGE", "weave-spike-environment"))
    args = ap.parse_args()

    results = run(args)
    out = HERE / f"results-{args.runtime}.json"
    out.write_text(json.dumps(results, indent=2))
    summary = {k: v.get("ok") for k, v in results["checks"].items()}
    print(json.dumps(summary, indent=2))
    print(f"wrote {out.name}")


if __name__ == "__main__":
    main()
