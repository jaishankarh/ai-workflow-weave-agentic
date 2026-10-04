# Agent runs use the OpenHands SDK, mostly through ACP agents, with skills staged from one central repo

Most AFK work will run on Cursor and Claude Code subscriptions, with API-key models (GLM, Gemini Flash) used occasionally. We therefore run every agent run through the OpenHands Software Agent SDK in a fresh sandbox per run: vendor agents through `ACPAgent`, API-key models through OpenHands' native loop, both chosen per node by an **Agent profile** (Product default, overridable per Task by a Tracker label). We build no agent loop of our own. A thin wrapper, a Python module inside the Dispatcher's job worker, does three things only: stages skills for the chosen Agent profile, gives the Dispatcher one start/status/cancel contract, and classifies each outcome.

Skills and `coding-standards.md` live in one central skills repo (one `coding-standards.md` per Product) and are copied per run into whichever folder that agent reads, together with an always-on file (`AGENTS.md` / `CLAUDE.md`) that points at the Repo's own `CONTEXT.md`. Each agent reads skills from a different place, and ACP does not standardise it, so staging is the only way every agent sees the same skills.

## Considered Options

- **Our own agent loop** — re-implements context management, edit tools, retries and cost tracking, and still needs ACP to drive Cursor and Claude Code.
- **Claude Agent SDK / Managed Agents** — Claude-only; doesn't drive Cursor or API-key models.
- **Rivet Sandbox Agent** — closest multi-agent alternative, but TypeScript-only.
- **Skills committed in every Repo** — N Repos × several agent folders drift apart.

## Consequences

- Outcomes are `succeeded`, `infra-failure` (retried once), `agent-gave-up`, and `quota-exhausted`. On `quota-exhausted` the Task stalls in `ready-for-agent` with a comment and retries after the subscription window resets; no automatic fallback to another Agent profile.
- Review prefers a different Agent profile than the one that implemented the Task, but may use the same one when no other is configured; the review result then says so.
- Running subscription agents unattended inside the sandbox is unproven and must be shown by a spike before build.
- Each run records the skills-repo version it used.
