---
ticket: https://github.com/jaishankarh/ai-workflow-weave-agentic/issues/12
label: wayfinder:research
status: findings (no decision; decision belongs to "Choose the agent runtime and how skills are packaged into it")
researched: 2026-10-05
---

# A common platform for driving many coding agents

## Question

Is there one interface the Python Dispatcher can use to drive both (a) a model-agnostic agent loop over raw LLM API keys and (b) vendor agents with their own loops (Claude Code, Cursor, Kimi Code, Codex, Gemini CLI …), headlessly, one Repo per Task? Or should we build our own orchestration layer and our own agent loop?

Builds on [02-agent-runtimes.md](./02-agent-runtimes.md). Facts from vendor docs and official repos, fetched 2026-10-05.

---

## 1. Agent Client Protocol (ACP) is the common interface that already exists

ACP is an open JSON-RPC 2.0 protocol between a *client* and a coding *agent*, normally over stdio with the agent as a subprocess. Agent-side methods: `initialize`, `authenticate`, `session/new`, `session/load`, `session/prompt`, `session/cancel`, `session/set_mode`; streamed `session/update` notifications. Client-side: only `session/request_permission` is mandatory; `fs/*` and `terminal/*` are optional capabilities ([protocol overview](https://agentclientprotocol.com/protocol/overview)).

**Agents that speak it** ([agents list](https://agentclientprotocol.com/get-started/agents), [registry](https://agentclientprotocol.com/get-started/registry)):
- Native: **Cursor**, **Kimi CLI**, **Gemini CLI**, **GitHub Copilot**, **OpenHands**, **OpenCode**, **Goose**, Cline, Factory Droid, Junie, Kiro CLI, Qwen Code, Mistral Vibe, Augment, Docker cagent, and others.
- Via adapter: **Claude Code** (`claude-agent-acp`), **Codex CLI** (`codex-acp`), **Pi** (`pi-acp`).
- Kimi: `kimi acp` waits for `initialize` on stdin with logs on stderr; it needs a prior login token and returns `authRequired` otherwise ([kimi acp](https://www.kimi.com/code/docs/en/kimi-code-cli/reference/kimi-acp.html)).

**Python client:** official `agent-client-protocol` package with Pydantic models and helpers for both sides, plus client examples ([Python SDK](https://agentclientprotocol.com/libraries/python)).

**What ACP does not standardise:** where an agent reads skills, rules or `AGENTS.md`/`CLAUDE.md`; auth (each agent its own key or login); sandboxing; remote transport (designed for local subprocesses). Permission requests must be auto-answered by a headless client.

## 2. Platforms built on top of many agents

| Option | Agents | How it works | Fit for a Python Dispatcher |
|---|---|---|---|
| **OpenHands `ACPAgent`** ([SDK guide](https://docs.openhands.dev/sdk/guides/agent-acp), [blog, 2026-06-18](https://www.openhands.dev/blog/use-any-coding-agent-in-openhands-with-acp)) | Any ACP agent (examples: Claude Code, Codex, Gemini CLI, OpenCode) plus OpenHands' own LiteLLM loop | Python SDK; works in `DockerWorkspace` and `APIRemoteWorkspace`; auto-approves permissions; captures token cost | **High.** Python, sandboxed, same API for own loop and vendor loops. Loses custom tools, MCP config, condenser and critic for ACP agents (the agent owns them). One-repo limits from 02 still apply. |
| **Rivet Sandbox Agent** ([repo](https://github.com/rivet-dev/sandbox-agent)) | Claude Code, Codex, OpenCode, Cursor, Amp, Pi | Rust binary inside any sandbox (E2B, Daytona, Modal, Docker…), HTTP + SSE, normalised event schema; Apache-2.0, v0.4 | Medium. **TypeScript SDK only, Python on roadmap** (raw HTTP usable). Young. |
| **Coder AgentAPI** ([repo](https://github.com/coder/agentapi)) | Claude Code, Codex, Gemini, Cursor CLI, Goose, Aider, OpenCode, Copilot, Amp… | Drives each agent's **TUI via a terminal emulator** and diffs screen output; `/message`, `/status`, `/events` | Low. Fragile by design (TUI changes break parsing); built for interactive chat, not structured runs. |
| **GitHub Agent HQ** ([GitHub blog](https://github.blog/news-insights/company-news/pick-your-agent-use-claude-and-codex-on-agent-hq/)) | Copilot, Claude, Codex | Assign an issue or @-mention; Copilot Pro+/Enterprise | Low. GitHub-only (breaks the Tracker adapter rule), no documented API, per-request premium billing. |
| **Vendor cloud APIs** (Cursor Cloud Agents [API](https://cursor.com/docs/cloud-agent/api/endpoints), OpenHands Cloud, Codex Cloud, Claude Managed Agents) | One vendor each | REST launch / status / cancel; Cursor v1 webhooks "coming soon" | Per-vendor adapters, vendor sandboxes. Usable as individual backends, not a common platform. |

## 3. The API-key agent loop: existing model-agnostic agents

All of these run on raw provider keys and also speak ACP, so they sit behind the same interface as the vendor agents:
- **OpenHands** native agent (LiteLLM; Python SDK) — see 02.
- **OpenCode** — `opencode serve` exposes an OpenAPI 3.1 HTTP server with sessions and SSE events ([server docs](https://opencode.ai/docs/server/)); any provider.
- **Goose** — native ACP, any provider.
- **Pi** — minimal model-agnostic loop with an RPC mode ([RPC docs](https://github.com/badlogic/pi-mono/blob/main/packages/coding-agent/docs/rpc.md)).

## 4. Building our own agent loop

Not found as a need in any decision so far. A loop of our own would have to re-implement context-window management, tool safety, edit/patch tools, test running, retries and cost tracking, which the agents above already ship. It would still need ACP (or per-vendor adapters) to drive Cursor, Kimi and Claude Code, so it replaces nothing on the vendor side. The case for it is custom in-loop behaviour that no existing agent allows (e.g. Gates inside a run), which ADR 0001 already ruled out: Gates live only on the Tracker.

## Facts the decision ticket will need

1. **ACP covers every agent named** (Cursor, Kimi, Claude Code via adapter, Codex via adapter, Gemini CLI) plus the model-agnostic loops (OpenHands, OpenCode, Goose, Pi). A Python ACP client is official.
2. **OpenHands `ACPAgent`** is the only option found that is Python, sandboxed, and drives both its own API-key loop and any ACP agent through one API.
3. **ACP does not package skills or rules**: each agent still reads them from its own folder, so the "how skills are packaged" half of the decision is unchanged.
4. **Auth differs per agent** (API key env vars for Claude/OpenAI/Gemini/Cursor; Kimi needs a login token in the sandbox).
5. **Rivet** is the closest purpose-built alternative but TypeScript-only today; **AgentAPI** scrapes TUIs; **Agent HQ** is GitHub-only.

## Gaps / not verified

- Whether Cursor's and Kimi's ACP modes run unattended in a container (auth bootstrap, permission auto-approve) — needs a spike.
- Whether OpenHands `ACPAgent` multi-repo checkout differs from its native agent.
- Rivet cancellation semantics (not documented on the README).
