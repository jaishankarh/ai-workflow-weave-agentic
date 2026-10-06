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

RUN python3 -m venv /opt/oh \
 && /opt/oh/bin/pip install --no-cache-dir -q \
      openhands-agent-server==${OPENHANDS_VERSION} \
      openhands-tools==${OPENHANDS_VERSION}

# IS_SANDBOX tells agent CLIs (Claude Code) they may skip permission prompts.
ENV PATH=/opt/oh/bin:/usr/local/bin:/usr/bin:/bin \
    OPENHANDS_SUPPRESS_BANNER=1 \
    IS_SANDBOX=1

RUN git config --system user.name "weave agent" \
 && git config --system user.email "agent@weave.invalid" \
 && git config --system init.defaultBranch main \
 && mkdir -p /workspace

WORKDIR /workspace
ENTRYPOINT ["/opt/oh/bin/python", "-m", "openhands.agent_server"]
