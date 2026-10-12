#!/bin/sh
# Sandbox entrypoint. Starts the sandbox's own Docker engine first when WEAVE_START_DOCKERD=1
# (set only for sandboxes on the sysbox runtime), then becomes the OpenHands agent-server.
# Without the variable it does nothing but exec the agent-server, as before.
set -eu

if [ "${WEAVE_START_DOCKERD:-}" = "1" ]; then
    dockerd >/var/log/dockerd.log 2>&1 &
    i=0
    until docker info >/dev/null 2>&1; do
        i=$((i + 1))
        if [ "$i" -gt 60 ]; then
            echo "weave: the sandbox's Docker engine did not come up; last log lines:" >&2
            tail -n 20 /var/log/dockerd.log >&2
            exit 1
        fi
        sleep 1
    done
fi

exec /opt/oh/bin/python -m openhands.agent_server "$@"
