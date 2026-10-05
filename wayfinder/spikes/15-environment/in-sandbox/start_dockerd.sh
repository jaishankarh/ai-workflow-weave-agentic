#!/usr/bin/env bash
# Start the inner Docker daemon inside the sandbox and wait until it answers.
# Last line of output is JSON. spike.py times the call.
set -uo pipefail
SUDO=""; [ "$(id -u)" = 0 ] || SUDO="sudo -n"

# A socket here before we start our own daemon means something mounted the
# host's socket in, which ADR 0004 forbids.
pre_socket=false; [ -S /var/run/docker.sock ] && pre_socket=true

$SUDO bash -c 'nohup dockerd >/tmp/dockerd.log 2>&1 &'
for _ in $(seq 1 120); do
  $SUDO docker info >/dev/null 2>&1 && break
  sleep 1
done

if $SUDO docker info >/dev/null 2>&1; then
  $SUDO chmod 666 /var/run/docker.sock 2>/dev/null || true
  printf '{"ok":true,"storage_driver":"%s","server_version":"%s","host_socket_present_before_start":%s}\n' \
    "$(docker info --format '{{.Driver}}')" "$(docker version --format '{{.Server.Version}}')" "$pre_socket"
else
  tail -n 30 /tmp/dockerd.log
  printf '{"ok":false,"host_socket_present_before_start":%s}\n' "$pre_socket"
  exit 1
fi
