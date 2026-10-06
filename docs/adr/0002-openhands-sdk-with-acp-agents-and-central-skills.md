# Agent runs use the OpenHands SDK, mostly through ACP agents, with skills staged from one central repo

Most AFK work will run on Cursor and Claude Code subscriptions, with API-key models (GLM, Gemini Flash) used occasionally. We therefore run every agent run through the OpenHands Software Agent SDK in a fresh sandbox per run: vendor agents through `ACPAgent`, API-key models through OpenHands' native loop, both chosen per node by an **Agent profile** (Product default, overridable per Task by a Tracker label). We build no agent loop of our own. A thin wrapper, a Python module inside the Dispatcher's job worker, does three things only: stages skills for the chosen Agent profile, gives the Dispatcher one start/status/cancel contract, and classifies each outcome.

Skills and `coding-standards.md` live in one central skills repo (one `coding-standards.md` per Product) and are copied per run into the folder that agent reads at **user level inside the sandbox** (for Claude Code, `~/.claude/skills/`), together with an always-on file at the same level (`~/.claude/CLAUDE.md`) that points at the Repo's own `CONTEXT.md` and its selected Coding standards. Nothing is ever staged into the Repo's working copy: the Repo's own `CLAUDE.md` and skills load as they normally would, and nothing staged can be committed into a PR. A Skill override is applied by not staging that central skill. _(Amended in the Spec 1 grilling, #20.)_ Each agent reads skills from a different place, and ACP does not standardise it, so staging is the only way every agent sees the same skills.

## Considered Options

- **Our own agent loop** — re-implements context management, edit tools, retries and cost tracking, and still needs ACP to drive Cursor and Claude Code.
- **Claude Agent SDK / Managed Agents** — Claude-only; doesn't drive Cursor or API-key models.
- **Rivet Sandbox Agent** — closest multi-agent alternative, but TypeScript-only.
- **Skills committed in every Repo** — N Repos × several agent folders drift apart.

## Consequences

- Outcomes are `succeeded`, `infra-failure` (retried once), `agent-gave-up`, `quota-exhausted`, and `needs-setup` (added in the Spec 1 grilling, #20). `needs-setup` means the run cannot do useful work until a human fixes its setup: the Repo is not onboarded (no `CONTEXT.md`, or a selected Coding standards file is missing), or the Agent profile's credential is rejected. It is never retried; the item goes to needs-human with a comment naming the problem and the error. An unreachable skills repo stays `infra-failure`. On `quota-exhausted` the Task stalls in `ready-for-agent` with a comment and retries after the subscription window resets; no automatic fallback to another Agent profile.
- Review prefers a different Agent profile than the one that implemented the Task, but may use the same one when no other is configured; the review result then says so.
- Running subscription agents unattended inside the sandbox is unproven and must be shown by a spike before build.
- Each run records the skills-repo version it used.
- Claude Code must let a user-level skill win over a same-named project skill for "central wins" to need no extra logic; verify during the build, else the wrapper resolves clashes itself. Cursor was only shown (#13) to read the project-level `.claude/skills/`; re-check user level before adding its Agent profile.
