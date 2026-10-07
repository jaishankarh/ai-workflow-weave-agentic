# Claude Code Agent profile image: the base sandbox image plus pinned Node.js,
# Claude Code CLI and its ACP adapter (claude-agent-acp). See README "Images".
#
# Build (after the base sandbox image):
#   docker build --build-arg SANDBOX_IMAGE=weave/sandbox:dev -t weave/claude-code:dev sandbox/claude-code/
# Add `--network host` on a host whose builds need it (see README).
#
# Upgrading an agent CLI is a deliberate change: bump the version ARG here (and
# the expected versions in tests/test_claude_code_profile.py).
ARG SANDBOX_IMAGE=weave/sandbox:dev
FROM ${SANDBOX_IMAGE}

# Node.js comes from the npm registry's per-platform packages (node-linux-<arch>,
# repackaged official release binaries), pinned by version and integrity, so the
# whole image builds from PyPI and npm alone.
ARG NODE_VERSION=22.22.0
ARG NODE_SHA512_X64=IUPhYvEZMuxK5isuiosZwtFGZb7Q/M12pG3JrWF12azSAxSnAoRira5Nlt6FuWvxQJglRQVltLoBwPNPOUYXKw==
ARG NODE_SHA512_ARM64=/QRxQQbxM+ociUFSUNGqBNASBrIu9swJtf5pRfqSfeyIIyAV5GR1OG2cdIPPP/sLfcBik0T7KEJniQq5y4LdMA==
ARG NPM_VERSION=10.9.4
ARG NPM_SHA512=OnUG836FwboQIbqtefDNlyR0gTHzIfwRfE3DuiNewBvnMnWEpB0VEXwBlFVgqpNzIgYo/MHh3d2Hel/pszapAA==
# The agent CLIs. CLAUDE_CODE_VERSION matches the Claude Code version that
# claude-agent-acp's Agent SDK is built against.
ARG CLAUDE_CODE_VERSION=2.1.287
ARG CLAUDE_AGENT_ACP_VERSION=0.86.0

# Node and Claude Code trust the system CA store (and so any CA the base image adds).
ENV NODE_EXTRA_CA_CERTS=/etc/ssl/certs/ca-certificates.crt

RUN set -eu; \
    case "$(uname -m)" in \
      x86_64) arch=x64; sum="$NODE_SHA512_X64" ;; \
      aarch64) arch=arm64; sum="$NODE_SHA512_ARM64" ;; \
      *) echo "unsupported architecture $(uname -m)" >&2; exit 1 ;; \
    esac; \
    fetch() { curl -fsSL -o "$1" "$2"; echo "$3  $1" | python3 -c 'import base64,hashlib,sys; s,f=sys.stdin.read().split(); d=base64.b64encode(hashlib.sha512(open(f,"rb").read()).digest()).decode(); sys.exit(0 if d==s else f"integrity mismatch for {f}")'; }; \
    cd /tmp; \
    fetch node.tgz "https://registry.npmjs.org/node-linux-${arch}/-/node-linux-${arch}-${NODE_VERSION}.tgz" "$sum"; \
    fetch npm.tgz "https://registry.npmjs.org/npm/-/npm-${NPM_VERSION}.tgz" "$NPM_SHA512"; \
    mkdir -p /opt/node/lib/node_modules/npm; \
    tar -xzf node.tgz -C /opt/node --strip-components=1 package/bin package/LICENSE; \
    tar -xzf npm.tgz -C /opt/node/lib/node_modules/npm --strip-components=1; \
    ln -s ../lib/node_modules/npm/bin/npm-cli.js /opt/node/bin/npm; \
    ln -s ../lib/node_modules/npm/bin/npx-cli.js /opt/node/bin/npx; \
    rm -f node.tgz npm.tgz; \
    PATH=/opt/node/bin:$PATH; \
    node --version; npm --version; \
    npm install -g --no-fund --no-audit --loglevel=error \
      "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
      "@agentclientprotocol/claude-agent-acp@${CLAUDE_AGENT_ACP_VERSION}"; \
    npm cache clean --force; \
    claude --version; claude-agent-acp --help >/dev/null 2>&1 || true

# claude-agent-acp runs the pinned Claude Code CLI above, not the copy bundled in its Agent SDK.
# IS_SANDBOX=1 (set in the base image) lets Claude Code bypass permission prompts as root.
ENV PATH=/opt/node/bin:${PATH} \
    CLAUDE_CODE_EXECUTABLE=/opt/node/bin/claude \
    DISABLE_AUTOUPDATER=1
