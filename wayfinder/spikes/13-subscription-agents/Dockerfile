# Sandbox image for the spike: the stock OpenHands agent-server (which already
# ships claude-agent-acp) plus the Cursor CLI, which is a host binary rather
# than an npm package and so is not in the stock image.
#
# Build:  docker build -t weave-spike-agent-server .
ARG BASE=ghcr.io/openhands/agent-server:latest-python
FROM ${BASE}

USER root
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates procps \
 && rm -rf /var/lib/apt/lists/*

# Install Cursor CLI into a world-readable prefix so it works whatever user the
# agent-server runs as, and put `agent` on PATH.
RUN mkdir -p /opt/cursor \
 && HOME=/opt/cursor bash -c 'curl -fsSL https://cursor.com/install | bash' \
 && chmod -R a+rX /opt/cursor \
 && ln -sf /opt/cursor/.local/bin/agent /usr/local/bin/agent \
 && agent --version

# Make sure claude-agent-acp is on PATH too (the stock image normally has it;
# this is a no-op guard if it does).
RUN command -v claude-agent-acp || npm install -g @agentclientprotocol/claude-agent-acp@0.63.0

# Claude Code refuses its permission-bypass mode as root unless it is told it is
# inside a sandbox. Harmless if the server runs as a normal user.
ENV IS_SANDBOX=1

# Hand back to whatever user the base image runs as (run.sh passes it in).
ARG RUNTIME_USER=root
USER ${RUNTIME_USER}
