# Base sandbox image for agent runs: the OpenHands agent-server on a plain
# Ubuntu base, with git and Python. Agent profiles build on top of it
# (the probe Agent profile in tests/probe/, Claude Code later).
#
# Build (normal machine):
#   docker build -t weave/sandbox:dev sandbox/
# Build (host without registry access; see README):
#   docker build --network host --build-arg BASE_IMAGE=weave/ubuntu-base:noble -t weave/sandbox:dev sandbox/
ARG BASE_IMAGE=ubuntu:24.04
FROM ${BASE_IMAGE}

ARG OPENHANDS_VERSION=1.53.0

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates git python3 python3-venv python3-pip curl procps tar \
 && rm -rf /var/lib/apt/lists/*

# Docker engine and Compose, for sandboxes started on the sysbox runtime (ADR 0004): the
# entrypoint starts the engine only when WEAVE_START_DOCKERD=1, so other sandboxes never run it.
# From the Ubuntu archive, so the version is the base image's, not a moving repository.
RUN apt-get update \
 && apt-get install -y --no-install-recommends docker.io docker-compose-v2 iptables \
 && rm -rf /var/lib/apt/lists/*

RUN python3 -m venv /opt/oh \
 && /opt/oh/bin/pip install --no-cache-dir -q \
      openhands-agent-server==${OPENHANDS_VERSION} \
      openhands-tools==${OPENHANDS_VERSION}

# Environment MCP servers (#55): the catalog's pinned database servers, each in its own virtualenv
# under /opt/weave-mcp/<kind>, installed now so a run needs no network to start one. These pins are
# the ones in src/workflow_weave/agent_worker/mcp.py (tests/test_environment_mcp_on_sysbox.py compares
# them). Their other dependencies are resolved at build time, not pinned, except the MCP SDK under
# postgres-mcp: it asks for `mcp>=1.5`, which now resolves to mcp 2.x, whose rename of FastMCP breaks
# postgres-mcp 0.3.0 at import (found by starting it; see README "Images").
ARG POSTGRES_MCP_VERSION=0.3.0
ARG POSTGRES_MCP_SDK_VERSION=1.30.0
ARG NEO4J_MCP_VERSION=0.6.0
ARG MYSQL_MCP_VERSION=0.4.4
RUN python3 -m venv /opt/weave-mcp/postgres \
 && /opt/weave-mcp/postgres/bin/pip install --no-cache-dir -q \
      postgres-mcp==${POSTGRES_MCP_VERSION} "mcp[cli]==${POSTGRES_MCP_SDK_VERSION}" \
 && python3 -m venv /opt/weave-mcp/neo4j \
 && /opt/weave-mcp/neo4j/bin/pip install --no-cache-dir -q mcp-neo4j-cypher==${NEO4J_MCP_VERSION} \
 && python3 -m venv /opt/weave-mcp/mysql \
 && /opt/weave-mcp/mysql/bin/pip install --no-cache-dir -q mysql-mcp-server==${MYSQL_MCP_VERSION}

# IS_SANDBOX tells agent CLIs (Claude Code) they may skip permission prompts.
# LITELLM_LOCAL_MODEL_COST_MAP: the agent-server otherwise fetches LiteLLM's cost map at
# start-up, and a race in its retry thread sometimes kills the server before it is healthy.
ENV PATH=/opt/oh/bin:/usr/local/bin:/usr/bin:/bin \
    OPENHANDS_SUPPRESS_BANNER=1 \
    LITELLM_LOCAL_MODEL_COST_MAP=True \
    IS_SANDBOX=1

RUN git config --system user.name "weave agent" \
 && git config --system user.email "agent@weave.invalid" \
 && git config --system init.defaultBranch main \
 && mkdir -p /workspace

COPY entrypoint.sh /usr/local/bin/weave-sandbox-entrypoint

WORKDIR /workspace
ENTRYPOINT ["/usr/local/bin/weave-sandbox-entrypoint"]
