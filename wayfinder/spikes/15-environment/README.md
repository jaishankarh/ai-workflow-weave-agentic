# Spike: an Environment with nested Docker and an Android emulator under OpenHands

Ticket: [Prove an Environment runs nested Docker and an Android emulator under OpenHands](https://github.com/jaishankarh/ai-workflow-weave-agentic/issues/15)

`spike.py` starts an OpenHands `DockerWorkspace` on the **sysbox** runtime with `/dev/kvm` passed in, and inside it (no host Docker socket, no `--privileged`) brings up the sample Run recipe's two-service stack, builds a React Native release APK, boots an emulator, shows the stack's data on screen, screenshots it, and tears it all down. It records, in `results-<runtime>.json`:

| Check | Passes when |
|---|---|
| sandbox | The workspace starts; `runtime_in_use` is `sysbox-runc`, `privileged` is false, `host_docker_socket_mounted` is false |
| dockerd | An inner Docker daemon starts inside the sandbox |
| compose | `recipe/` builds and comes up, is seeded with a per-run nonce, and `GET /hello` returns it; `api_log_tail` shows the server log lines an API Proof would quote |
| kvm | `/dev/kvm` exists in the sandbox, opens read-write, and `emulator -accel-check` says KVM is usable |
| apk | A release APK builds (x86_64 only); records React Native version and size |
| emulator | A headless emulator (API 34, x86_64) cold-boots to `sys.boot_completed` |
| proof | The APK installs and the nonce appears on screen (checked from the UI dump, not by eye); `proof-<runtime>.png` is the screenshot |
| teardown | After the workspace exits, no new containers, volumes, networks, images (bar the sandbox image) or emulator processes remain on the host |

Every check has `seconds`, so boot times come out of the same file.

## Default: GitHub Actions (no machine needed)

[`.github/workflows/spike-15-environment.yml`](../../../.github/workflows/spike-15-environment.yml) runs everything below on a GitHub-hosted Ubuntu runner: a full throwaway VM with sudo and KVM, so it can install sysbox and accelerate the emulator. It runs on every push to this branch that touches the spike, and on manual dispatch (optionally with `rn_app_repo` to also time a real app). It posts the results summary as a comment on the ticket and uploads the screenshot as the `spike-15-results` artifact.

Codespaces is not a substitute: a Codespace is a dev container on a VM we don't control, so sysbox can't be installed in it.

## Host you need (to run it yourself instead)

A **Linux x86_64 machine with KVM**: bare metal, or a cloud VM with nested virtualization. Docker Desktop on macOS or Windows cannot do this (no sysbox, no `/dev/kvm`), and neither can this cloud workspace (no `/dev/kvm`, no virtualization flags, no image pulls).

Roughly 8 vCPU, 16 GB RAM, 80 GB free disk (the sandbox image is ~10 GB; Gradle and the emulator are the heavy parts).

If you don't have such a machine, a GCP VM works: an Intel N2 instance created with `--enable-nested-virtualization`, Ubuntu 24.04, 80 GB disk.

## Run it (~1 hour, mostly builds)

1. **Install Docker Engine and sysbox** (sysbox's installer restarts Docker, so stop other containers first):
   ```bash
   # Docker Engine, if missing: https://docs.docker.com/engine/install/
   # sysbox CE: download the .deb for your distro from https://github.com/nestybox/sysbox/releases
   sudo apt-get install -y jq ./sysbox-ce_*.deb
   docker info --format '{{json .Runtimes}}' | grep -q sysbox-runc && echo sysbox ok
   ```
2. **Make `/dev/kvm` world read-write.** Sysbox maps devices to `nobody:nogroup` inside the container, so the emulator can only open `/dev/kvm` if "others" can:
   ```bash
   ls -l /dev/kvm   # expect crw-rw---- root kvm on most distros
   echo 'KERNEL=="kvm", MODE="0666"' | sudo tee /etc/udev/rules.d/99-kvm-spike.rules
   sudo udevadm control --reload && sudo udevadm trigger --name-match=kvm
   ls -l /dev/kvm   # now crw-rw-rw-
   ```
3. **Build the sandbox image** (agent-server + Docker Engine + JDK 17 + Android SDK/emulator):
   ```bash
   cd wayfinder/spikes/15-environment
   docker pull ghcr.io/openhands/agent-server:latest-python
   RUNTIME_USER=$(docker inspect --format '{{.Config.User}}' ghcr.io/openhands/agent-server:latest-python)
   docker build --build-arg RUNTIME_USER=${RUNTIME_USER:-root} -t weave-spike-environment .
   ```
   If the command-line tools download 404s, take the current Linux zip name from <https://developer.android.com/studio#command-line-tools-only> and update the Dockerfile.
4. **Install the SDK and run:**
   ```bash
   python3 -m venv .venv && . .venv/bin/activate
   pip install openhands-sdk==1.51.0 openhands-workspace==1.51.0
   python spike.py
   ```
5. **Optional, worth it:** time one of your real apps too (build, install, launch, screenshot; no backend check):
   ```bash
   python spike.py --rn-app ~/path/to/one-of-your-react-native-apps
   ```
   That run overwrites `results-sysbox-runc.json`, so rename the first one before.
6. **Post `results-sysbox-runc.json` and `proof-sysbox-runc.png`** as a comment on the ticket (or commit them on this branch).

If a step fails, still post the results file: `output_tail` on the failing check is the finding. If `sandbox` fails before anything runs, also try `python spike.py --runtime default` after setting `"default-runtime": "sysbox-runc"` in `/etc/docker/daemon.json` (see below).

## Already established without a run (2026-10-05)

- **Image: selectable.** `DockerWorkspace(server_image=...)` takes any pre-built agent-server image, so the Environment toolchain (Docker Engine, JDK, Android SDK) can be baked into our own image.
- **Runtime and devices: not selectable in SDK 1.51.0.** `DockerWorkspace` exposes `volumes`, `network`, ports and `enable_gpu`, but has no `runtime` or `device` option. `spike.py` adds both through a small subclass that injects `--runtime` / `--device` into the SDK's `docker run`; a local test confirmed the flags land in the `docker run` call. For the build there are three ways: keep that subclass (fragile across SDK upgrades), contribute the two options upstream, or set sysbox as the Docker daemon's `default-runtime` on a host dedicated to sandboxes (no code at all, but every container on that daemon gets sysbox). `/dev/kvm` still needs `--device` in every case.
- **Kubernetes alternative exists.** `AgentSandboxWorkspace` runs the agent-server in a pod claimed from a warm pool, and its isolation comes from a `runtimeClass` on the `SandboxTemplate`. Not needed now; noted in case Environments move to a cluster.
- **Sysbox limits that matter here** (from its [limitations doc](https://github.com/nestybox/sysbox/blob/master/docs/user-guide/limitations.md)): `--privileged`, `--net=host`, `--pid=host` and `--userns=host` are refused; devices passed in appear as `nobody:nogroup` (hence step 2); sysbox cannot run inside sysbox, so the host itself must be a VM or bare metal, not a container.
- **Emulator networking.** The emulator runs inside the sandbox, so the stack's published ports are on the sandbox's loopback, which the emulator reaches as `10.0.2.2`. No extra networking needed.
- **Release APKs block plain HTTP.** React Native release builds set `usesCleartextTraffic` false, and the Environment's backend is plain HTTP; the sample app flips it. Every mobile Repo's Run recipe will need the same (or a debuggable build variant pointed at the Environment).

## What this spike decides

- **Passes:** Environments run as designed in [ADR 0004](https://github.com/jaishankarh/ai-workflow-weave-agentic/blob/main/docs/adr/0004-proofs-run-per-story-in-one-throwaway-environment.md); mobile UI leaves the "Not provable here" list; the boot times feed the per-run time budgets.
- **Sysbox fails under OpenHands but works on the host:** use the daemon default-runtime route, or the upstream options.
- **Sysbox can't be used at all:** each Environment needs its own throwaway VM (a microVM such as Kata or Firecracker, or a cloud VM with nested virtualization) in which plain Docker and the emulator run; the results' `host` block says what this machine had.
- **Emulator fails but nested Docker works:** the "Mobile device farm" question on the map becomes mandatory.
