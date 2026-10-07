# Probe Agent profile image: the base sandbox image plus the probe ACP agent.
# Built by the test suite (tests/conftest.py); see README "Running the tests".
ARG SANDBOX_IMAGE=weave/sandbox:dev
FROM ${SANDBOX_IMAGE}
COPY probe_agent.py /opt/probe/probe_agent.py
