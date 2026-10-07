# TEST ONLY: the Claude Code Agent profile's image with Claude Code pointed at a
# scripted fake Messages API inside the sandbox (fake_anthropic_api.py), so the real
# Claude Code CLI and claude-agent-acp can be run at Seam A without a token.
# Built by the test suite (tests/conftest.py).
ARG CLAUDE_CODE_IMAGE=weave/claude-code:dev
FROM ${CLAUDE_CODE_IMAGE}
COPY fake_anthropic_api.py /opt/fake-anthropic/fake_anthropic_api.py
COPY managed-settings.json /etc/claude-code/managed-settings.json
COPY entrypoint.sh /opt/fake-anthropic/entrypoint.sh
ENTRYPOINT ["/bin/sh", "/opt/fake-anthropic/entrypoint.sh"]
