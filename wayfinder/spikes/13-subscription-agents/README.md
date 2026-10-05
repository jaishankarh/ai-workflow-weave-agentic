# Spike: subscription agents unattended under OpenHands

Ticket: [Prove Cursor and Claude Code subscription agents run unattended under OpenHands](https://github.com/jaishankarh/ai-workflow-weave-agentic/issues/13)

`spike.py` runs Claude Code and Cursor through OpenHands `ACPAgent` inside a `DockerWorkspace` and records, per agent, in `results-<agent>.json`:

| Check | Passes when |
|---|---|
| auth | The agent answers using only a token passed as a conversation secret |
| permissions | The agent runs a shell command and writes `perm.txt` with nobody approving |
| skills | At least one staged skill folder loads; `loaded` shows which of `.claude/skills`, `.agents/skills`, `.cursor/skills`, `CLAUDE.md`, `AGENTS.md` did |
| cancel | After `interrupt()`, the agent's in-flight `sleep 901` is gone; `container_removed` shows the container went with the workspace |
| bad_token | Not a pass/fail: records what an auth failure looks like |
| error_events | Every `ConversationErrorEvent`, verbatim (secrets masked by the SDK) |

## Run it (your machine, ~20 minutes)

Needs Docker, Python 3.12+, and network access to `ghcr.io` and `cursor.com`.

1. **Get the two credentials.** Neither is written to disk by the spike; they go in as conversation secrets.
   - Claude Code: run `claude setup-token`, approve in the browser, copy the printed token (one-year, uses your Pro/Max subscription).
   - Cursor: create a user API key in the Cursor dashboard (Integrations → API keys). Note whether the dashboard says it bills against your plan.
2. **Build the sandbox image** (stock agent-server + Cursor CLI):
   ```bash
   cd wayfinder/spikes/13-subscription-agents
   docker pull ghcr.io/openhands/agent-server:latest-python
   RUNTIME_USER=$(docker inspect --format '{{.Config.User}}' ghcr.io/openhands/agent-server:latest-python)
   docker build --build-arg RUNTIME_USER=${RUNTIME_USER:-root} -t weave-spike-agent-server .
   ```
3. **Install the SDK and run:**
   ```bash
   python3 -m venv .venv && . .venv/bin/activate
   pip install openhands-sdk==1.51.0 openhands-workspace==1.51.0 openhands-tools==1.51.0
   export CLAUDE_CODE_OAUTH_TOKEN=...   # step 1
   export CURSOR_API_KEY=...            # step 1
   unset ANTHROPIC_API_KEY ANTHROPIC_BASE_URL   # these silently override the OAuth token
   python spike.py --agent all
   ```
   Results are saved after every check, so a Ctrl-C keeps what's done. The last check (bad token) deliberately spends 1–2 minutes on `401 Invalid bearer token` retries before failing; that's expected, not a hang. To rerun only some checks: `python spike.py --agent cursor --checks auth,skills`.
4. **Post `results-claude-code.json` and `results-cursor.json`** as a comment on the ticket (or commit them on this branch). Also say whether either agent popped up anything in a browser or on your screen (it shouldn't).
5. **Optional, only if an account is near its limit anyway:** run `python spike.py --agent <that one>` again while limited, so `error_events` captures the real quota failure. Don't burn quota just for this.

If the Docker build fails at the Cursor install step, paste the error; that alone is a finding (Cursor's installer may not support the image's distro or architecture).

## Already established without the logins (2026-10-05)

- **Claude Code, bad token, run locally through `ACPAgent`:** no interactive prompt, fails within seconds. OpenHands reports it as `ConversationErrorEvent` code **`ACPPromptError`** (not `ACPAuthRequired`) with detail `[-32603] Internal error: Authentication error … {"errorKind": "authentication_failed"}`; execution status `ERROR`. So the wrapper must classify from the `errorKind` in the detail, not from OpenHands' code.
- From the adapter source (`@agentclientprotocol/claude-agent-acp` 0.63.0): failed turns carry the Claude SDK's error kind (`authentication_failed`, `billing_error`, `rate_limit`, `invalid_request`, `server_error`, `unknown`) as `errorKind`; usage updates carry `_meta["_claude/rateLimit"]`. A quota hit is expected to show `"errorKind": "rate_limit"` (to be confirmed).
- Claude Code reads project skills from `.claude/skills/` and `CLAUDE.md` (the adapter loads user, project and local settings).
- Cursor docs list `.agents/skills/`, `.cursor/skills/` and, for compatibility, `.claude/skills/`; whether the **CLI** honours them is what the skills check answers. If `.claude/skills` loads for both, staging needs one folder.
- Cursor CLI is not a built-in OpenHands ACP provider yet ([OpenHands SDK issue 4872](https://github.com/OpenHands/software-agent-sdk/issues/4872)); it runs here through a custom `acp_command`. Its ACP `authenticate(cursor_login)` is interactive, but OpenHands only calls `authenticate` for methods it recognises, so with `CURSOR_API_KEY` set it should skip it.
- This cloud workspace cannot pull from Docker Hub or GHCR, which is why the DockerWorkspace run needs your machine.
