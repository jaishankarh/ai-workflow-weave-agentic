#!/bin/sh
/opt/oh/bin/python /opt/fake-anthropic/fake_anthropic_api.py 8765 &
exec /opt/oh/bin/python -m openhands.agent_server "$@"
