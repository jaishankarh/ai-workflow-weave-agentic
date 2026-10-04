---
ticket: wayfinder/tickets/02-research-agent-runtimes.md
label: wayfinder:research
status: findings (no decision; decision belongs to 05-grilling-agent-runtime)
researched: 2026-10-05
---

# Remote agent runtimes for the AFK nodes

## Question

What does each candidate runtime offer for running the AFK nodes remotely and headlessly? The nodes are: implement a Task (one Repo, one PR) with custom `SKILL.md` skills (implement, tdd, code-review), run the code-review skill, fix review comments, run the full test suite, produce proofs (browser screenshots, logs, test reports), and deploy to staging.

Candidates:
1. **OpenHands**: OpenHands Cloud, self-hosted (OSS / Enterprise), Software Agent SDK and agent server, Cloud REST API, the built-in resolver, and the GitHub / GitLab / Jira integrations.
2. **Anthropic**: Claude Code headless (`claude -p`), the Claude Agent SDK, Claude Code GitHub Actions, Claude Code cloud sessions and routines, and Claude Managed Agents.
3. **OpenAI Codex**: `codex exec`, the Codex SDK and app-server, `openai/codex-action`, and Codex Cloud.

Criteria: how a run is started, observed and cancelled from code; loading custom `SKILL.md` skills and repo docs (`CONTEXT.md`, `coding-standards.md`); checking out more than one Repo; running tests and a headless browser inside the sandbox; secrets; concurrency and cost; model choice; how results come back (PRs, comments, artifacts).

All facts come from vendor docs and official repos, fetched 2026-10-05. Where the docs say nothing, this file says so; it does not guess. Product names and versions change quickly in this space, so re-check anything that is load-bearing before building on it.

---

## 0. Two shapes of runtime

Every candidate comes in two shapes. The decision ticket has to pick a shape as well as a vendor.

- **Library / CLI you host.** The agent loop runs in a container or CI runner you control: the OpenHands SDK with `DockerWorkspace` or a self-hosted agent server, `claude -p` or the Agent SDK, `codex exec` or the Codex SDK. You own the sandbox image, secrets, concurrency and checkout. You pay model tokens plus your own compute.
- **Vendor-hosted session.** The vendor runs the sandbox and the loop, and you call an API or trigger: OpenHands Cloud (API or `OpenHandsCloudWorkspace`), Claude Managed Agents, Claude Code routines and cloud sessions, Codex Cloud. Setup is lighter, but the vendor decides the image, limits and identity.

The GitHub Actions variants (`anthropics/claude-code-action`, `openai/codex-action`, the OpenHands PR-review composite action) are the first shape, with GitHub-hosted runners as the sandbox.

---

## 1. OpenHands

### 1.1 Surfaces

- **Software Agent SDK (Python, MIT, model-agnostic).** Offers "a unified Python API that enables you to run agents locally or in the cloud", ready-made tools (bash, file edit, browser, MCP), and a REST/WebSocket **agent server** that "runs agents anywhere, including Docker and Kubernetes" ([SDK overview](https://docs.openhands.dev/sdk)).
- **Workspaces.** `LocalWorkspace`, `DockerWorkspace` (prebuilt `ghcr.io/openhands/agent-server:latest-python`), `APIRemoteWorkspace` (OpenHands Runtime API) and `OpenHandsCloudWorkspace`. "The same SDK API works across all … workspace types" ([agent server overview](https://docs.openhands.dev/sdk/guides/agent-server/overview), [Docker sandbox](https://docs.openhands.dev/sdk/guides/agent-server/docker-sandbox)).
- **Cloud REST API (V1).** `POST https://app.all-hands.dev/api/v1/app-conversations` with a bearer API key. The V0 `/api/conversations` endpoint is deprecated; its deprecation window ended 2026-04-01 ([Cloud API](https://docs.openhands.dev/openhands/usage/cloud/cloud-api)).
- **CLI headless mode.** `openhands --headless -t "task"` (or `-f task.txt`), plus `--json` for JSONL events. "Headless mode always runs in `always-approve` mode." ([headless](https://docs.openhands.dev/openhands/usage/cli/headless)).
- **Built-in resolver and integrations.** GitHub, GitLab, Bitbucket, Jira Cloud and Data Center, Slack, Linear, and event or webhook "automations" (see 1.8).
- **Self-hosted.** Open-source Agent Canvas (Docker, process or Kubernetes backends), and **OpenHands Enterprise** (commercial; Helm or VM install, with Sysbox for sandboxes) ([enterprise vs OSS](https://docs.openhands.dev/enterprise/enterprise-vs-oss), [k8s install](https://docs.openhands.dev/enterprise/k8s-install/index)).

### 1.2 Start, observe, cancel

- **Cloud API.** The start call returns a start-task `id`. Poll `GET /api/v1/app-conversations/start-tasks?ids=…` until it reports `READY` and an `app_conversation_id`, then poll `GET /api/v1/app-conversations?ids=…`. The terminal states are `finished`, `error`, `stuck` and `waiting_for_confirmation` ([Cloud API](https://docs.openhands.dev/openhands/usage/cloud/cloud-api)).
- **Other API endpoints.** There are endpoints to search events ([search events](https://docs.openhands.dev/openhands/usage/cloud/api-reference/events/search-events)) and send follow-up messages. The start-conversation OpenAPI documents **no pause or delete endpoint** ([start app conversation](https://docs.openhands.dev/openhands/usage/cloud/api-reference/conversations/start-app-conversation)).
- **Start request fields.** `selected_repository`, `selected_branch`, `git_provider` (github, gitlab, bitbucket, bitbucket_data_center, forgejo, azure_devops, enterprise_sso), `llm_model`, `agent_type` (`default` or `plan`), `pr_number[]`, `system_message_suffix`, `plugins` ("their skills/MCP config are merged"), `secrets`, `processors` (e.g. "posts a summary comment back to the originating PR"), and observability tags and metadata.
- **SDK.** Event callbacks and WebSocket streaming ([agent server overview](https://docs.openhands.dev/sdk/guides/agent-server/overview)). `conversation.pause()` from another thread, then `run()` to resume; cost is available from `llm.metrics.accumulated_cost` ([pause/resume](https://docs.openhands.dev/sdk/guides/convo-pause-and-resume)). The agent-server REST API has a pause endpoint ([pause conversation](https://docs.openhands.dev/sdk/guides/agent-server/api-reference/conversations/pause-conversation)).

### 1.3 Skills and repo docs

- **Agent Skills standard.** The preferred skill location is `.agents/skills/<name>/SKILL.md`. The legacy `.openhands/skills/` and `.openhands/microagents/` still work. User-level skills live in `~/.agents/skills/` ([skills overview](https://docs.openhands.dev/overview/skills)).
- **Always-on repo context.** `AGENTS.md` goes into the initial system prompt; `CLAUDE.md` and `GEMINI.md` are also recognised. Skills activate in three ways: always-on, keyword-triggered, or path-triggered ("injected deterministically whenever the agent reads, edits, or creates a file") ([skills overview](https://docs.openhands.dev/overview/skills), [path rules](https://docs.openhands.dev/overview/skills/path)).
- **Organisation and user skills.** These "apply to all repositories belonging to the organization or user" ([org skills](https://docs.openhands.dev/overview/skills/org)).
- **SDK.**
  - `load_project_skills()` "automatically finds AGENTS.md, CLAUDE.md, GEMINI.md at workspace root".
  - `load_skills_from_dir()` returns repo skills, knowledge skills and agent skills.
  - `load_public_skills()` pulls from the [OpenHands/extensions](https://github.com/OpenHands/extensions) registry.
  - Skills pass in via `AgentContext(skills=…)`, with progressive disclosure (only the description sits in the prompt) ([SDK skills](https://docs.openhands.dev/sdk/guides/skill)).
- **What the docs don't cover.** No mechanism auto-loads `CONTEXT.md` or `coding-standards.md` by name. They reach the agent if `AGENTS.md` references them, or through `system_message_suffix` or the initial message.

### 1.4 Multi-repo checkout

- **Cloud API.** `selected_repository` is a single string.
- **Sandbox docs.** Nothing on attaching more than one repo ([sandboxes overview](https://docs.openhands.dev/openhands/usage/sandboxes/overview), [custom sandbox](https://docs.openhands.dev/openhands/usage/advanced/custom-sandbox-guide)).
- **SDK.** The workspace offers `execute_command` and file upload/download, so a caller can `git clone` more repos itself. That is a do-it-yourself pattern, not a documented feature.

### 1.5 Tests and browser in the sandbox

- **Default image.** `agent-server:<ver>-python` includes Python and Node.js "and includes VSCode and VNC" ([custom sandbox](https://docs.openhands.dev/openhands/usage/advanced/custom-sandbox-guide)). `DockerWorkspace(extra_ports=True)` exposes VS Code on host port +1 and a VNC browser view on +2 ([Docker sandbox](https://docs.openhands.dev/sdk/guides/agent-server/docker-sandbox)).
- **Browser tool.** Built on [browser-use](https://github.com/browser-use/browser-use) (navigate, click, extract) ([browser use](https://docs.openhands.dev/sdk/guides/agent-browser-use)). Sessions can be recorded and replayed with rrweb ([recording](https://docs.openhands.dev/sdk/guides/browser-session-recording)). The fetched page does not say whether the tool saves screenshots to disk.
- **Custom images.** In OSS and the SDK, build `--target binary` on any `BASE_IMAGE` and set `AGENT_SERVER_IMAGE_REPOSITORY` and `AGENT_SERVER_IMAGE_TAG`; `DockerDevWorkspace(base_image=…)` builds on the fly ([custom sandbox](https://docs.openhands.dev/openhands/usage/advanced/custom-sandbox-guide)).
- **Plan limits.** "Custom runtime images" are listed as Enterprise-only in the product comparison ([enterprise vs OSS](https://docs.openhands.dev/enterprise/enterprise-vs-oss)). Enterprise also documents Docker-in-sandbox "without privileged access" ([docker in sandbox](https://docs.openhands.dev/enterprise/docker-in-sandbox)).
- **Resource limits.** Neither the sandbox nor the Cloud docs give resource limits.

### 1.6 Secrets

- **Cloud UI.** Secrets set under Settings > Secrets are "automatically exported as environment variables in the agent's runtime environment" ([secrets settings](https://docs.openhands.dev/openhands/usage/settings/secrets-settings)). The Cloud API also takes a `secrets` object.
- **SDK.** The secret registry takes `update_secrets()` with static strings or a `SecretSource` (lazy `get_value()`). It injects secrets as env vars when a bash command references them and "masks secret values in command outputs" ([SDK secrets](https://docs.openhands.dev/sdk/guides/secrets)). With `OpenHandsCloudWorkspace.get_secrets()`, "raw values never transit through the SDK client" ([cloud workspace](https://docs.openhands.dev/sdk/guides/agent-server/cloud-workspace)).
- **GitHub token.** The Cloud GitHub App uses "short-lived tokens (8-hour expiration)" with actions, contents, PR and workflows permissions ([GitHub install](https://docs.openhands.dev/openhands/usage/cloud/github-installation)).

### 1.7 Concurrency, cost, models

- **Plans.**
  - Open Source (local): free, unlimited.
  - Individual SaaS: "10 max" daily conversations, Jira and Slack integrations, bring your own key or "OpenHands provider at-cost with no markup".
  - Enterprise (SaaS or self-hosted in your VPC): custom price, unlimited conversations, SSO and RBAC ([pricing](https://openhands.dev/pricing)).
- **Concurrency.** The Cloud API has "concurrent conversation limits … Older conversations pause if you exceed them". The number is not published; contact support to raise it ([Cloud API](https://docs.openhands.dev/openhands/usage/cloud/cloud-api)).
- **Budgets.** Monthly org cap, a default per-user budget, and per-user overrides. "When the organization reaches 100% … new conversations may be blocked" ([budgets](https://docs.openhands.dev/openhands/usage/cloud/organizations/budgets)). LiteLLM gateway budgeting is Enterprise-only ([enterprise vs OSS](https://docs.openhands.dev/enterprise/enterprise-vs-oss)).
- **Models.** Model-agnostic (Claude, OpenAI, Qwen, Devstral and others). The per-conversation `llm_model` field lets each node choose its model.

### 1.8 How results come back

- **Resolver.** Triggered on GitHub by the `openhands` label or an `@openhands` mention in issues, PR comments or inline reviews. It posts an eyes reaction and an "I'm on it!" comment, does repository operations "using the triggering user's credentials", submits formal PR reviews under the triggering user's identity, and leaves a completion comment from the bot ([enterprise GitHub](https://docs.openhands.dev/enterprise/integrations/github)).
  - There is "no supported setting that makes formal reviews run as the GitHub App bot" and no way to turn off the acknowledgement comment.
  - `@openhands` in a PR only works when the PR is "both *to* and *from* a repository that you have added" ([GitHub install](https://docs.openhands.dev/openhands/usage/cloud/github-installation)).
- **GitLab.** Same label and mention triggers; it opens an MR and posts a summary. Group projects need GitLab Premium or Ultimate ([GitLab](https://docs.openhands.dev/openhands/usage/cloud/gitlab-installation)).
- **Jira Cloud.** Triggered by the `openhands` label or an `@openhands` comment. The repo must be named in the ticket ("Repository: AcmeCo/WebApp"). Output is a PR ([Jira](https://docs.openhands.dev/openhands/usage/cloud/project-management/jira-integration)). Jira Data Center is Enterprise ([Jira DC](https://docs.openhands.dev/enterprise/integrations/jira-data-center)).
- **Event automations.** GitHub events (with JMESPath filters) or custom webhooks. They need a team organisation and a "claimed" GitHub org, otherwise events "will be silently dropped" ([event automations](https://docs.openhands.dev/openhands/usage/automations/event-automations)).
- **PR review in GitHub Actions.** A composite action from the SDK repo, triggered by the `review-this` label or by requesting `openhands-agent` as reviewer. It takes `llm-model`, `llm-api-key` and `github-token`, and loads skills from OpenHands/extensions plus your repo's `.agents/skills/` ([PR review](https://docs.openhands.dev/sdk/guides/github-workflows/pr-review)).
- **Artifacts.** The SDK has workspace file download. The docs describe no built-in artifact or proof store.

---

## 2. Anthropic: Claude Code headless, Agent SDK, GitHub Actions, cloud sessions and routines, Managed Agents

### 2.1 Surfaces

- **`claude -p` (headless CLI).** `--output-format text|json|stream-json`, `--json-schema`, `--allowedTools`, `--permission-mode`, `--permission-prompts none` (v2.1.259+), `--resume <session_id>`, `--append-system-prompt[-file]`, `--settings`, `--mcp-config`, `--plugin-dir`, `--add-dir`.
  - `--bare` "is the recommended mode for scripted and SDK calls". It skips auto-discovery of hooks, skills, plugins, MCP, auto memory and CLAUDE.md.
  - Exit code 0 means success; SIGTERM exits with 143 ([headless](https://code.claude.com/docs/en/headless)).
- **Claude Agent SDK** (`@anthropic-ai/claude-agent-sdk` for TypeScript, `claude-agent-sdk` for Python). It spawns the `claude` binary as a subprocess with the same tools, hooks, subagents, MCP, sessions, skills and plugins ([overview](https://code.claude.com/docs/en/agent-sdk/overview)).
  - Billed with an API key (or Bedrock, Vertex/Agent Platform, Foundry).
  - "Anthropic does not allow third party developers to offer claude.ai login or rate limits for their products" built on the SDK.
- **Claude Code GitHub Actions** (`anthropics/claude-code-action@v1`, built on the SDK) ([GitHub Actions](https://code.claude.com/docs/en/github-actions)). There is also a GitLab CI/CD guide ([GitLab](https://code.claude.com/docs/en/gitlab-ci-cd)).
- **Cloud sessions and routines** (claude.ai subscription; Pro, Max, Team, Enterprise premium seats).
  - Cloud sessions run in Anthropic-managed VMs, or on an org's self-hosted environment ([cloud sessions](https://code.claude.com/docs/en/claude-code-on-the-web)).
  - Routines are saved prompt + repos + connectors, with schedule, API or GitHub triggers. They are a **research preview** ([routines](https://code.claude.com/docs/en/routines)).
- **Claude Managed Agents** (Claude API, **beta**, header `managed-agents-2026-04-01`). A hosted agent harness with agents, environments, sessions and an SSE event stream. Sandboxes can be cloud or self-hosted ([overview](https://platform.claude.com/docs/en/managed-agents/overview)).

### 2.2 Start, observe, cancel

- **`claude -p` and the SDK.**
  - **Start.** A process call or `query()`.
  - **Observe.** `stream-json` events. `system/init` reports the model, tools, MCP status, plugins, `plugin_errors`, `mcp_server_errors` and the loaded `skills`. `system/api_retry` reports retries. The final `result` carries `total_cost_usd`. Subagent messages carry `parent_tool_use_id` ([headless](https://code.claude.com/docs/en/headless)).
  - **Cancel.** In the SDK: `abortController`, `interrupt()` (streaming-input mode) or `close()`. In the CLI: SIGINT ends the turn, and SIGTERM kills the process tree and runs `SessionEnd` hooks ([TS reference](https://code.claude.com/docs/en/agent-sdk/typescript), [headless](https://code.claude.com/docs/en/headless)).
  - **Bounds.** `maxTurns`, `maxBudgetUsd` (a client-side estimate), `fallbackModel`. "No top-level session timeout", so wall-clock limits come from the container ([hosting](https://code.claude.com/docs/en/agent-sdk/hosting)).
  - **Telemetry.** OpenTelemetry traces, metrics and logs via `CLAUDE_CODE_ENABLE_TELEMETRY` and the `OTEL_*` env vars ([hosting](https://code.claude.com/docs/en/agent-sdk/hosting)).
- **Routines API trigger.**
  - Call `POST https://api.anthropic.com/v1/claude_code/routines/<trig_id>/fire` with a per-routine bearer token and beta header `experimental-cc-routine-2026-04-01`, and optionally `{"text": …}`. It returns `claude_code_session_id` and `claude_code_session_url`.
  - Fire text arrives wrapped as untrusted `<routine-fire-payload>`.
  - A green run status "does not mean the task … succeeded".
  - The docs describe **no cancel API**: you can pause or disable the routine and archive or delete sessions in the UI.
  - Follow-ups can be queued with `claude -p "msg" --cloud <session-id>` ([routines](https://code.claude.com/docs/en/routines), [cloud sessions](https://code.claude.com/docs/en/claude-code-on-the-web)).
- **Managed Agents.**
  - **Start.** `POST /v1/sessions` (agent, `environment_id`, `resources`, `vault_ids`, `initial_events`, `budget.max_list_cost`), then `POST /v1/sessions/{id}/events`.
  - **Observe.** Stream over SSE.
  - **Statuses.** `idle`, `running`, `rescheduling`, `terminated`.
  - **Cancel.** Send a `{"type":"user.interrupt"}` event, then archive or delete the session (a running session must be interrupted first) ([sessions](https://platform.claude.com/docs/en/managed-agents/sessions), [session operations](https://platform.claude.com/docs/en/managed-agents/session-operations)).

### 2.3 Skills and repo docs

- **SDK and CLI skills.** Skills are `.claude/skills/<name>/SKILL.md`, found in `<cwd>` and every parent up to the repo root, in `~/.claude/skills/`, and in each `additionalDirectories` / `--add-dir` directory.
  - The `skills` option takes `"all"`, a list of names, or `[]`.
  - Dispatch a skill by sending `/<name>` in the prompt.
  - Plugins (`plugins` option, `--plugin-dir`, `--plugin-url`) load skills from any path, and `system/init` lists the loaded skills ([SDK skills](https://code.claude.com/docs/en/agent-sdk/skills)).
  - The paths differ from OpenHands and Codex, which use `.agents/skills` (see the cross-cutting facts in section 4).
- **Repo docs.** `CLAUDE.md` loads at session start and can import other files with `@path/to/import`, up to 4 hops deep, so `@CONTEXT.md @docs/coding-standards.md` loads both. Claude Code also reads `AGENTS.md` when no `CLAUDE.md` is present, or alongside one ([memory](https://code.claude.com/docs/en/memory)). `--append-system-prompt-file` is an alternative.
- **GitHub Action.** Run `actions/checkout` first so the repo's `.claude/skills/` exists, then pass `prompt: "/skill-name"`. Plugin skills go through `plugin_marketplaces` and `plugins` ([GitHub Actions](https://code.claude.com/docs/en/github-actions)).
- **Cloud sessions and routines.** These read the repo's `CLAUDE.md`, `.claude/skills/`, `.claude/agents/` and `.claude/rules/`, plus skills enabled on claude.ai. They do **not** read user `~/.claude/*`, and they do not install plugins declared in repo settings. With several repos, a session "starts above the clones" and doesn't read any repo's `.claude/settings.json` hooks or `.mcp.json` ([cloud environments](https://code.claude.com/docs/en/cloud-environments)).
- **Managed Agents.**
  - Agents attach `skills: [{type: anthropic|custom, skill_id, version}]`. Custom skills are uploaded through the Skills API (`ant apply skills/<dir>`), up to 500 per session.
  - Skills in a mounted repo's `.claude/skills/<name>/SKILL.md` are also discovered automatically ([MA skills](https://platform.claude.com/docs/en/managed-agents/skills)).
  - The fetched pages don't say whether `CLAUDE.md` in a mounted repo is read.

### 2.4 Multi-repo checkout

- **SDK and CLI.** Any number of repos, cloned by your code. Each one is reachable through `cwd` plus `additionalDirectories`.
- **Routines.** "Add one or more GitHub repositories". Each is cloned from the default branch on every run, and changes go to `claude/`-prefixed branches ([routines](https://code.claude.com/docs/en/routines)).
- **`claude --cloud`.** One repo at a time ([cloud sessions](https://code.claude.com/docs/en/claude-code-on-the-web)).
- **Managed Agents.** Multiple `github_repository` resources, each with `mount_path`, `checkout` (branch or SHA) and an `authorization_token`. Repos are fixed for the life of the session; the token can be rotated ([MA GitHub](https://platform.claude.com/docs/en/managed-agents/github)).

### 2.5 Tests and browser in the sandbox

- **SDK.** The sandbox is your own container. The hosting guide suggests 1 GiB RAM, 5 GiB disk and 1 CPU per agent as a starting floor, and points to cookbook Dockerfiles for Docker, Modal and Kubernetes ([hosting](https://code.claude.com/docs/en/agent-sdk/hosting)). Browsers and Playwright are whatever you install.
- **GitHub Action.** A GitHub-hosted runner: whatever your workflow installs.
- **Cloud sessions and routines (Anthropic-hosted).**
  - **Pre-installed.** Python, Node 20/21/22 (with chromedriver), Java 21, Go, Rust, Ruby, PHP, C/C++, Docker with compose, PostgreSQL 16, Redis 7, gh. Playwright or Chromium is not listed as pre-installed; add it with a setup script.
  - **Resources.** 4 vCPU, 16 GB RAM, 30 GB disk.
  - **Timeouts.** Foreground commands default to 2 min (max 10 min), raisable with `BASH_DEFAULT_TIMEOUT_MS` / `BASH_MAX_TIMEOUT_MS`.
  - **Setup scripts.** Cached when they finish in about 5 minutes.
  - **Base image.** Replacing it "isn't supported yet" ([cloud environments](https://code.claude.com/docs/en/cloud-environments)).
- **Managed Agents cloud sandbox.**
  - Ubuntu 24.04, x86_64, up to 8 GB RAM and 10 GB disk.
  - Playwright (Python and Node) with Chromium at `/opt/pw-browsers/chromium` is pre-installed; there is no Firefox or WebKit.
  - `docker` has "limited availability".
  - Packages are declared in the environment config (apt, pip, npm, cargo, gem, go) ([sandbox reference](https://platform.claude.com/docs/en/managed-agents/cloud-sandboxes-reference), [environments](https://platform.claude.com/docs/en/managed-agents/environments)).

### 2.6 Secrets

- **SDK.** The subprocess reads `ANTHROPIC_API_KEY`, or `ANTHROPIC_BASE_URL` pointing at a proxy that injects the key. The guide recommends keeping tool credentials out of the agent environment and adding them at an egress proxy ([hosting](https://code.claude.com/docs/en/agent-sdk/hosting)). For a shared container, use `settingSources: []`, `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1` and a per-tenant `CLAUDE_CONFIG_DIR`.
- **GitHub Action.**
  - Credentials: the `ANTHROPIC_API_KEY` or `CLAUDE_CODE_OAUTH_TOKEN` secret, OIDC workload-identity federation (`anthropic_federation_rule_id` and related inputs), or Bedrock, Agent Platform or Foundry via OIDC.
  - Who can trigger: the triggering user needs write access, and bots are rejected unless listed in `allowed_bots`.
  - Pushes made with the default `GITHUB_TOKEN` don't trigger CI; use the Claude App or a custom app token instead ([GitHub Actions](https://code.claude.com/docs/en/github-actions)).
- **Cloud sessions and routines.**
  - Env vars are "visible to anyone who uses the environment".
  - "API credentials", which a proxy injects so the key never enters the VM, exist on Pro and Max only, **not yet on Team or Enterprise**.
  - GitHub credentials stay outside the VM, behind a GitHub proxy ([cloud environments](https://code.claude.com/docs/en/cloud-environments), [cloud sessions](https://code.claude.com/docs/en/claude-code-on-the-web)).
- **Managed Agents.** `vault_ids` handle MCP auth, and each repo carries its own `authorization_token`, which is not echoed back ([sessions](https://platform.claude.com/docs/en/managed-agents/sessions), [MA GitHub](https://platform.claude.com/docs/en/managed-agents/github)).

### 2.7 Concurrency, cost, models

- **SDK.** One session is one subprocess. Concurrency is bounded by host RAM, and the guide notes that "large parallel-subagent fanouts can hit rate limits". "Anthropic token cost typically dominates container infrastructure cost by an order of magnitude" ([hosting](https://code.claude.com/docs/en/agent-sdk/hosting)).
- **GitHub Action.** Runner minutes plus API tokens, bounded with `--max-turns` and GitHub `concurrency:` groups ([GitHub Actions](https://code.claude.com/docs/en/github-actions)).
- **Routines.**
  - Usage draws on the claude.ai subscription, plus overage credits if enabled.
  - Hourly caps: 100 scheduled runs per account; 30 Run-now / API fires per routine; 100 API fires per account. GitHub events have per-routine and per-account caps, and excess events are dropped.
  - Routines belong to an individual account. Commits and PRs carry that user's GitHub identity ([routines](https://code.claude.com/docs/en/routines)).
- **Managed Agents.**
  - Runtime: $0.08 per session-hour, counted only while `running`.
  - Rate limits: 300 create requests and 1,200 reads per minute per org. The fetched pages publish no limit on concurrent sessions.
  - Not eligible for ZDR or HIPAA ([pricing](https://platform.claude.com/docs/en/about-claude/pricing), [reference](https://platform.claude.com/docs/en/managed-agents/reference), [overview](https://platform.claude.com/docs/en/managed-agents/overview)).
- **Token prices (per MTok, input / output).** Opus 5.5 $4 / $20; Sonnet 5.5 $2 / $10; Haiku 4.5 $1 / $5 ([pricing](https://platform.claude.com/docs/en/about-claude/pricing)).
- **Models.** Claude models only, chosen per run with `--model` or the `model` option. Inference can route through Bedrock, Vertex/Agent Platform, Foundry or an LLM gateway (`ANTHROPIC_BASE_URL`).

### 2.8 How results come back

- **SDK and CLI.** The `result` text and `structured_output` (through `--json-schema`). Files stay in the working directory. PRs, comments and artifact uploads are whatever the agent does with `gh` or tools, or what your orchestrator does after the run.
- **GitHub Action.**
  - Interactive mode replies in a comment on the issue or PR. Automation mode writes to the run log unless the prompt and tools tell it to post.
  - The review example posts inline PR comments through `mcp__github_inline_comment__create_inline_comment`.
  - Runner artifacts (screenshots, reports) need a normal `actions/upload-artifact` step ([GitHub Actions](https://code.claude.com/docs/en/github-actions)).
- **Cloud sessions and routines.** A session URL with diff view. PRs are created from the session or by Claude pushing `claude/*` branches.
  - **Auto-fix PRs.** Claude subscribes to CI failures and review comments on a PR and pushes fixes. Replies are posted under the user's GitHub account, labelled as Claude Code.
  - Auto-fix "can't react to" merge conflicts ([cloud sessions](https://code.claude.com/docs/en/claude-code-on-the-web)).
- **Managed Agents.** SSE events. Session-scoped files are downloaded through the Files API before deletion. PRs come from the agent using the GitHub MCP or its token ([session operations](https://platform.claude.com/docs/en/managed-agents/session-operations), [MA GitHub](https://platform.claude.com/docs/en/managed-agents/github)).

---

## 3. OpenAI Codex

### 3.1 Surfaces

- **`codex exec` (non-interactive CLI).** `--json` (JSONL), `--output-last-message <path>`, `--output-schema <path>`, `--sandbox read-only|workspace-write|danger-full-access` (default read-only), `--ephemeral`, `--skip-git-repo-check`, and `codex exec resume --last`. CI auth uses `CODEX_API_KEY` ([non-interactive](https://learn.chatgpt.com/docs/non-interactive-mode)).
- **Codex SDK.**
  - TypeScript `@openai/codex-sdk` spawns the CLI and exchanges JSONL. It has `startThread()`, `run()`, `runStreamed()` (events such as `item.completed` and `turn.completed`), `resumeThread()` (threads persist in `~/.codex/sessions`), and `workingDirectory` / `skipGitRepoCheck` options.
  - Python `openai-codex` (Python 3.10+) drives the app-server over JSON-RPC.
  - Sandbox presets are `read_only`, `workspace_write` and `full_access` ([SDK](https://learn.chatgpt.com/docs/codex-sdk), [TS README](https://github.com/openai/codex/blob/main/sdk/typescript/README.md)).
- **Codex app-server.** JSON-RPC 2.0 over stdio, WebSocket (experimental) or Unix socket. Methods include `thread/start`, `thread/resume`, `turn/start` (per-turn model, effort and sandbox overrides), `turn/interrupt` (ends with `status: "interrupted"`) and `skills/list`, with streamed `item/*` and `turn/*` notifications. Auth can be an API key, ChatGPT OAuth, external tokens or Bedrock ([app-server](https://learn.chatgpt.com/docs/app-server)).
- **`openai/codex-action@v1`.** Inputs: `prompt` / `prompt-file`, `openai-api-key` (it starts a Responses API proxy so the key isn't exposed), `sandbox`, `safety-strategy` (`drop-sudo` default, `unprivileged-user`, `read-only`, `unsafe`), `model`, `effort`, `codex-args`, `output-file`. Its single output is `final-message`; posting comments is a separate `github-script` step ([GitHub Action](https://learn.chatgpt.com/docs/github-action), [repo](https://github.com/openai/codex-action)).
- **Codex Cloud.** The "all new Codex Cloud" was announced 2026-09-30, with reusable cloud environments and control from phone, web or desktop ([community announcement](https://community.openai.com/t/meet-the-all-new-codex-cloud/1402399), [cloud](https://learn.chatgpt.com/docs/cloud)). The previous environment docs are now labelled **"Codex Cloud (Legacy)"** ([legacy env](https://learn.chatgpt.com/docs/environments/cloud-environment)).
  - Tasks start from the ChatGPT web, mobile or desktop app, from `@codex` in GitHub, from Linear and Slack, and from `codex cloud exec QUERY --env ENV_ID [--attempts 1-4]` ([CLI reference](https://learn.chatgpt.com/docs/cli/reference)).

### 3.2 Start, observe, cancel

- **Local or self-hosted (`exec`, SDK, app-server).** Fully programmatic. Observe through JSONL events or app-server notifications; cancel with `turn/interrupt`. The fetched pages don't document AbortSignal on the TS SDK.
- **Codex Cloud.** The fetched docs show **no public REST API** for starting, observing or cancelling cloud tasks. The SDK page covers local threads only ([SDK](https://learn.chatgpt.com/docs/codex-sdk)). `codex cloud exec` can submit a task from a script, but its output and status contract isn't documented on the fetched pages. Pricing says an API key gives "Codex in the CLI, SDK, or IDE extension" **"without cloud-based features"** ([pricing](https://learn.chatgpt.com/docs/pricing)), so Codex Cloud needs a ChatGPT plan seat.

### 3.3 Skills and repo docs

- **Skills.** Agent Skills standard (`SKILL.md` with `name` and `description`). Discovery covers repo `.agents/skills` (from cwd up to the root), `~/.agents/skills`, `/etc/codex/skills`, and built-ins. Invoke with `$skill` or implicitly by description match ([skills](https://learn.chatgpt.com/docs/build-skills)).
  - Cloud: "Skills stored in your repository are available in cloud tasks. Personal skills from your local computer aren't synced." ([cloud environments](https://learn.chatgpt.com/docs/environments/cloud-environments)).
- **AGENTS.md.** Loaded from `~/.codex`, then from the git root down to cwd, with `AGENTS.override.md` taking precedence. Files are concatenated root-first, capped by `project_doc_max_bytes` (32 KiB default). Configurable fallback names allow, for example, `TEAM_GUIDE.md` ([AGENTS.md](https://learn.chatgpt.com/docs/agent-configuration/agents-md)). The docs show no `@import` syntax, so `CONTEXT.md` and `coding-standards.md` must be referenced from `AGENTS.md` and read by the agent, or added to the fallback names.
- **GitHub Action.** The action page doesn't say whether `AGENTS.md` and skills load. It runs `codex exec` in the checked-out repo, so the CLI discovery above would apply, but that is an inference.

### 3.4 Multi-repo checkout

- **Local.** Any repos you clone.
- **New Codex Cloud environments.** "Select the GitHub repositories to check out" (plural) ([cloud environments](https://learn.chatgpt.com/docs/environments/cloud-environments)).
- **GitHub only.** GitLab and self-hosted GitHub Enterprise Server are listed as unsupported in cloud.

### 3.5 Tests and browser in the sandbox

- **New Codex Cloud.**
  - Environments have an **Install script** and a **Start skill** ("instructions to start services and check that they're ready").
  - VMs are 4 vCPU, 16 GiB RAM and 32 GiB disk on Pro, Business and Enterprise.
  - "**Computer and browser use**" is listed under **current limitations, unsupported** ([cloud environments](https://learn.chatgpt.com/docs/environments/cloud-environments)). Running headless Playwright as a shell command is not addressed either way.
- **Legacy.** The `universal` image ([openai/codex-universal](https://github.com/openai/codex-universal)); setup scripts run in a separate shell; containers cache for up to 12 h ([legacy env](https://learn.chatgpt.com/docs/environments/cloud-environment)).
- **Local / Action.** Whatever your container or runner has. The sandbox modes limit file and network access, which matters for Playwright downloads and staging calls.

### 3.6 Secrets

- **New cloud.**
  - "Environment variable" values are passed straight to programs.
  - "Network secret" values are credentials for a specific HTTPS host; "Programs receive a placeholder" and the value is attached on the way out ([cloud environments](https://learn.chatgpt.com/docs/environments/cloud-environments)).
- **Legacy cloud.** Secrets were "only available to setup scripts … removed before the agent phase starts" ([legacy env](https://learn.chatgpt.com/docs/environments/cloud-environment)).
- **Action.** The API key goes through a proxy, and `drop-sudo` protects runner secrets ([GitHub Action](https://learn.chatgpt.com/docs/github-action)).

### 3.7 Concurrency, cost, models

- **API key (exec, SDK, Action).** Pay per token.
- **Cloud.** Requires a ChatGPT plan: Plus $20, Pro $100–$500, Business $20 per user per month, or Enterprise. Usage limits are per 5-hour window, extendable with credits ([pricing](https://learn.chatgpt.com/docs/pricing)). Cloud lets "multiple tasks … run simultaneously from the same environment"; no number is published ([cloud](https://learn.chatgpt.com/docs/cloud)).
- **Models.** OpenAI models (current names include GPT-6.1 Sol, GPT-6 Luna and GPT-6 Astra, per the [pricing](https://learn.chatgpt.com/docs/pricing) and [SDK](https://learn.chatgpt.com/docs/codex-sdk) pages). The CLI also supports custom `model_providers` (`base_url`, `env_key`, `wire_api`), Azure, and `--oss` with Ollama or LM Studio ([advanced config](https://learn.chatgpt.com/docs/config-file/config-advanced)). Cloud model choice beyond the plan's models is not documented.

### 3.8 How results come back

- **Cloud.** "Commit or open a pull request when you're ready", from the chat UI ([cloud](https://learn.chatgpt.com/docs/cloud)).
- **GitHub.** `@codex review` posts a standard GitHub review limited to P0 and P1 issues, guided by `AGENTS.md`. `@codex fix the P1 issue` starts a cloud chat. Automatic reviews can be turned on in settings ([GitHub](https://learn.chatgpt.com/docs/third-party/github)). There is also a GitLab MR review page ([GitLab](https://learn.chatgpt.com/docs/third-party/gitlab)).
- **Linear.** Assign the issue to Codex or mention `@Codex`. It posts a summary and a chat link "so you can create a pull request", so the PR is not opened automatically ([Linear](https://learn.chatgpt.com/docs/third-party/linear)).
- **Action / exec.** `final-message`, an `--output-last-message` file, `--output-schema` JSON. Artifacts go through normal CI upload steps.

---

## 4. Cross-cutting facts

1. **Two skill-folder conventions.**
   - Claude Code, the Agent SDK, cloud sessions and the GitHub Action discover `.claude/skills/<name>/SKILL.md`, and Managed Agents also discovers that path in mounted repos.
   - OpenHands and Codex discover `.agents/skills/<name>/SKILL.md`.
   - All of them use the same `SKILL.md` format (Agent Skills standard). One skill set can serve every runtime if it is kept in one place and either copied into or symlinked from both folders, or loaded explicitly (Claude: `plugins` / `--plugin-dir` / `--add-dir`; OpenHands: `load_skills_from_dir` / Cloud `plugins`). Whether symlinks survive each vendor's clone or upload path was not verified.
2. **Repo docs.** None of the runtimes auto-loads `CONTEXT.md` or `coding-standards.md` by name. The always-on file differs: `CLAUDE.md` (with `@import`) or `AGENTS.md` for Claude Code, `AGENTS.md` / `CLAUDE.md` for OpenHands, `AGENTS.md` (32 KiB cap, no import syntax shown) for Codex. A short `AGENTS.md` that points at both files works for all three. Claude Code can inline them with `@CONTEXT.md` in `CLAUDE.md`.
3. **One PR per Task.** No runtime enforces this. The hosted resolvers (OpenHands, Codex `@codex`, Claude GitHub Action interactive mode) decide for themselves whether to open a PR, and on which branch. Routines push to `claude/*` by default. A dispatcher that wants a deterministic branch name and exactly one PR has to say so in the prompt or skill, or open the PR itself after the run.
4. **Identity.** OpenHands' resolver acts with the triggering user's credentials. Claude routines and auto-fix act as the user's GitHub account. The Claude GitHub Action uses the Claude App or a custom app token. Managed Agents and self-hosted SDK runs use whatever token you mount. This affects branch protection, CODEOWNERS, and whether bot pushes trigger CI.
5. **Browser proofs.** The only runtime that documents Playwright and Chromium pre-installed is the Claude Managed Agents cloud sandbox. OpenHands images ship a browser-use tool and VNC. Claude cloud sessions ship chromedriver but no listed Playwright. New Codex Cloud lists browser use as unsupported. For self-hosted or CI runs the image is yours.
6. **Cancel.**
   - Programmatic cancel is documented for: the OpenHands SDK (pause), the Claude SDK (`abortController` / `interrupt` / `close`), Managed Agents (`user.interrupt`), and the Codex app-server (`turn/interrupt`).
   - It is **not documented** for: the OpenHands Cloud API, Claude routines and cloud sessions, and Codex Cloud.
7. **Preview / beta status.** Claude routines are a research preview, Claude Managed Agents is beta, and the new Codex Cloud is days old (announced 2026-09-30, with the old environment docs marked legacy). OpenHands Cloud V1 API is GA; V0 is deprecated.

---

## 5. Comparison table

| Criterion | OpenHands (Cloud / SDK / self-host) | Claude: SDK / `claude -p` / GH Action | Claude: routines and cloud sessions | Claude: Managed Agents | Codex: exec / SDK / app-server / Action | Codex Cloud |
|---|---|---|---|---|---|---|
| Start from code | Cloud REST `POST /api/v1/app-conversations`; SDK `Conversation`; agent-server REST/WS; CLI `--headless` | Subprocess / `query()`; GH Action on any event | `POST …/routines/{id}/fire` (beta header); schedule; GitHub PR/release events | `POST /v1/sessions` + events (beta) | `codex exec`; SDK threads; app-server JSON-RPC; GH Action | `codex cloud exec --env`; UI; `@codex`; Linear/Slack. No documented REST |
| Observe | Poll conversations + search events (Cloud); WS events (SDK) | `stream-json` (init, retries, result with cost); OTEL | Session URL; run list; `/schedule` history | SSE event stream; status | JSONL events; app-server notifications | UI / chat link |
| Cancel | SDK `pause()`; no cancel/delete in Cloud API docs | `abortController`, `interrupt()`, `close()`, SIGINT/SIGTERM | Not documented (pause routine, archive session) | `user.interrupt`, then archive/delete | `turn/interrupt` | Not documented |
| Custom SKILL.md | `.agents/skills`, legacy `.openhands/*`; org/user skills; SDK loaders; Cloud `plugins` | `.claude/skills`, `--add-dir`, plugins; `skills` allowlist; `/name` dispatch | Repo `.claude/skills` + claude.ai-enabled skills; not user `~/.claude` | Uploaded custom skills (≤500) + repo `.claude/skills` | `.agents/skills` (repo, user, admin); `$skill` | Repo skills yes; personal no |
| Repo docs auto-loaded | `AGENTS.md`, `CLAUDE.md`, `GEMINI.md` | `CLAUDE.md` (+`@imports`) or `AGENTS.md` | Repo `CLAUDE.md`, `.claude/rules` | Not stated for `CLAUDE.md` | `AGENTS.md` hierarchy, 32 KiB | `AGENTS.md` |
| Multi-repo | Cloud API: one repo field; SDK: DIY clone | DIY clone + `additionalDirectories` | Routines: one or more repos; `--cloud`: one | Multiple `github_repository` mounts | DIY | Environments: multiple GitHub repos |
| Tests / services | Custom images (Enterprise for Cloud runtime images); Docker-in-sandbox (Enterprise) | Your image / runner | 4 vCPU / 16 GB / 30 GB; Docker + compose; Postgres, Redis; 10-min max per command (configurable) | ≤8 GB / 10 GB; packages declared; docker limited | Your image / runner | 4 vCPU / 16 GiB / 32 GiB; install script + start skill |
| Headless browser | browser-use tool; VNC; rrweb recording | Install yourself | chromedriver; Playwright via setup script | Playwright + Chromium pre-installed | Install yourself | Browser use listed unsupported |
| Secrets | Env vars (UI/API); SDK registry with output masking | Env / secret manager; egress-proxy pattern; OIDC in GH Action | Env vars visible to environment users; proxy-injected API credentials Pro/Max only | Vaults (MCP); per-repo tokens | Env; Action key proxy + `drop-sudo` | Env vars + network secrets (placeholder injection) |
| Concurrency limits | Cloud: unpublished concurrent cap, oldest paused; Individual 10/day | Your infra + API rate limits | Hourly fire caps (30/routine, 100/account); subscription usage | 300 creates/min; concurrency not published | Your infra + API limits | Unpublished; plan usage windows |
| Cost model | BYO key or at-cost LLM; Enterprise custom | API tokens (+ runner/infra) | Subscription (+ overage credits) | Tokens + $0.08/session-hour | API tokens | ChatGPT plan seat (API key excluded) |
| Model choice | Any (LiteLLM-style); per conversation | Claude only; per run; Bedrock/Vertex/Foundry/gateway | Claude; model per routine | Claude; per agent/session override | OpenAI + custom providers / OSS | OpenAI plan models |
| Results | Resolver comments + PR/MR as triggering user; Cloud `processors` summary comment | Result JSON / structured output; GH Action comments / inline reviews; PRs via `gh` | Session diff, `claude/*` branch, PR; auto-fix on CI/reviews | SSE + session files (Files API); PR via token/MCP | `final-message` / file / schema JSON; post via separate step | PR from chat; `@codex` reviews (P0/P1); Linear summary |
| GitLab / Jira | GitLab (Premium/Ultimate for groups), Jira Cloud/DC, Bitbucket | GitLab CI/CD guide; Jira via MCP/your code | GitHub only for clone/PR | GitHub resource type only | Your CI | GitHub only; GitLab MR review page exists; Linear |
| Status | GA (V1 API) | GA | Research preview | Beta | GA | New (2026-09-30) |

---

## 6. Facts the decision ticket will need

1. **Open-model portability exists only in OpenHands** (any LLM per conversation) and in self-hosted Codex CLI (custom `model_providers`). Claude runtimes are Claude-only; Codex Cloud is OpenAI-only.
2. **Programmatic lifecycle (start + stream + cancel) is fully documented only for the libraries you host:** the OpenHands SDK and agent server, the Claude Agent SDK and `claude -p`, the Codex SDK and app-server. Claude Managed Agents adds it for a hosted option, in beta. The OpenHands Cloud API documents no cancel. Claude routines and Codex Cloud document no cancel and only partial status.
3. **Tracker coverage out of the box:** OpenHands has native GitHub, GitLab, Bitbucket, Jira and Linear triggers. Claude cloud and routines are GitHub-only. Codex Cloud covers GitHub and Linear (plus GitLab MR review). Under the "Tracker adapter" constraint, a self-hosted dispatcher calling an SDK avoids depending on any vendor's tracker integration.
4. **Multi-repo per run** is documented for Managed Agents (mounts), Claude routines, and Codex Cloud environments. The OpenHands Cloud API takes one repo; the SDK path is do-it-yourself. Under the "Task = one Repo" rule, extra repos would be read-only context (for example a shared contracts repo).
5. **Skill folders differ** (`.claude/skills` vs `.agents/skills`); the format is the same. Keeping one skill source of truth needs a sync or explicit-load step per runtime.
6. **Browser proofs:** only the Managed Agents sandbox ships Playwright and Chromium. For every other runtime, the proof node needs a custom image or setup script. New Codex Cloud lists browser use as unsupported.
7. **Secrets that don't enter the sandbox** (proxy injection): Claude cloud API credentials (Pro/Max only, not Team/Enterprise), Codex Cloud network secrets, OpenHands Cloud workspace secrets (lazy, not via the client), and the Agent SDK egress-proxy pattern (you build it). Staging-deploy credentials are the main case where this matters.
8. **Identity on PRs and comments:** OpenHands resolver acts as the triggering user. Claude routines and auto-fix act as the routine owner's GitHub user. GitHub Action uses the App or a custom app. SDK and Managed Agents use whatever token you supply. This feeds the "merge stays manual" and human-review Gate design, and whether CI fires on agent pushes (the default `GITHUB_TOKEN` does not trigger workflows).
9. **Cost levers:** Managed Agents $0.08 per running session-hour plus tokens. Routines draw on subscription usage, with hourly fire caps and per-user ownership (not team-shared). Codex Cloud needs a ChatGPT seat, since an API key excludes cloud. OpenHands Cloud Individual is capped at 10 conversations per day; Enterprise is custom. The SDK and CLI paths cost tokens plus your own compute (Anthropic: about 1 GiB RAM per agent as a floor).
10. **Budget and turn caps per run:** Claude `maxTurns` / `--max-turns` and `maxBudgetUsd`; Managed Agents `budget.max_list_cost` (hard ceiling); OpenHands org and user budgets (Cloud) and `accumulated_cost` (SDK). Codex: not documented on the fetched pages.
11. **Maturity risk:** Claude routines are a research preview, Managed Agents is beta, and new Codex Cloud is days old with its legacy docs superseded. These APIs may change while the workflow is being built.
12. **Not found in docs (verify before deciding):** OpenHands Cloud concurrent-conversation number, sandbox resources, and whether screenshots are saved as files; Managed Agents concurrent-session cap, max session duration, and whether it reads `CLAUDE.md` in mounts; the Codex Cloud task API, status contract and concurrency; and whether symlinked skill folders survive each vendor's clone or upload.
