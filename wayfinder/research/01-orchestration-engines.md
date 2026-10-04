---
ticket: wayfinder/tickets/01-research-orchestration-engines.md
label: wayfinder:research
status: findings (no decision; decision belongs to 04-grilling-orchestration-architecture)
researched: 2026-10-05
---

# Orchestration engines for a gated, long-running ticket workflow

## Question

Which orchestration options fit a workflow whose nodes are long-running agent runs (AFK, e.g. OpenHands) and whose Gates wait hours or days for a human, driven by Tracker events? Options compared: LangGraph (and LangGraph Platform, now "LangSmith Deployment"), n8n, Langflow, Temporal, and a thin custom dispatcher where Tracker State is the single source of truth.

Workflow shape assumed (from `CONTEXT.md` and `wayfinder/map.md`): Stories and Tasks move through States on a Tracker (GitHub first, Jira and GitLab later via a Tracker adapter). AFK nodes are remote agent runs lasting minutes to hours. Gates (Grilling session in the editor, human PR review, manual merge) wait hours or days. Review→fix loops repeat. A scheduled staging deploy runs every few hours. A Product spans many Repos; each Task is one Repo and one PR.

All facts below are from primary sources (vendor docs, official repos, package registries), fetched 2026-10-05. Where docs were silent, that is stated.

---

## 1. LangGraph (OSS) and LangSmith Deployment (formerly LangGraph Platform)

**Status.** `langgraph` 1.2.12, released 2026-09-21, MIT licence ([PyPI](https://pypi.org/project/langgraph/), [repo](https://github.com/langchain-ai/langgraph)). Described as "a low-level orchestration framework for building, managing, and deploying long-running, stateful agents".

**Durable human waits.** `interrupt()` inside a node saves graph state and pauses; the docs state the "graph waits indefinitely until you resume execution with a response". Requires a checkpointer and a `thread_id`; resume with `Command(resume=value)` ([interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts)).
- Caveat: "The node restarts from the beginning of the node where the `interrupt` was called when resumed, so any code before the `interrupt` runs again." Side effects before an interrupt (e.g. starting an agent run, posting a comment) must be idempotent or moved to a separate node.
- Checkpointers: `InMemorySaver` (lost on restart), `SqliteSaver` (dev), `PostgresSaver`/`AsyncPostgresSaver` (recommended for production) ([persistence](https://docs.langchain.com/oss/python/langgraph/persistence)).

**Resuming from external events.** OSS LangGraph has no webhook ingress: your own service receives the GitHub webhook, maps it to a `thread_id`, and calls `graph.invoke(Command(resume=...), config)`. LangSmith Deployment exposes an HTTP API (threads, runs) that a webhook receiver can call, and can POST a `webhook` URL "at the completion of a run" ([use webhooks](https://docs.langchain.com/langsmith/use-webhooks)). Inbound: still needs a small receiver in front.

**Scheduling.** LangSmith Deployment has cron jobs (`client.crons.create(...)`, UTC only, stateless or per-thread) ([cron jobs](https://docs.langchain.com/langsmith/cron-jobs)). OSS LangGraph has no scheduler.

**Self-hosting & cost.**
- OSS library: free, you host the process + Postgres.
- LangSmith Deployment self-hosted (full platform, hybrid, or standalone Agent Server): "Self-hosted deployments require an Enterprise plan and the LangSmith license key delivered with that plan" ([self-hosted overview](https://docs.langchain.com/langsmith/deploy-to-self-hosted-overview)). Standalone server needs Postgres + Redis and `LANGGRAPH_CLOUD_LICENSE_KEY`; "Do not run standalone servers in serverless environments" ([standalone](https://docs.langchain.com/langsmith/deploy-standalone-server)).
- Cloud: Developer/Plus plans are cloud-only; Plus includes "1 free small serverless deployment"; billed in LSU per vCPU-hr / GiB-hr plus database uptime ("the duration your deployment's database is live and persisting state") ([pricing](https://www.langchain.com/pricing)). A graph waiting days on a Gate keeps a deployment's DB live.

**Observability / replay.** Checkpoint history via `get_state_history()`; replay from a `checkpoint_id` and fork via `update_state()`. "Replay re-executes nodes—it doesn't just read from cache. LLM calls, API requests, and interrupts fire again" ([time travel](https://docs.langchain.com/oss/python/langgraph/use-time-travel)). LangSmith tracing integrates natively.

**Calling an external agent runtime.** Plain Python in a node: start an OpenHands run, then either poll inside the node (holds a worker for hours) or end the node and `interrupt()` until a completion event resumes the thread. The second pattern is the durable one but must be designed by hand.

**Multi-repo / multi-Product.** No built-in concept; a `thread_id` per Story/Task and Repo as state field. Fine, but you design it.

**Second source of truth.** High. The checkpointed graph state (which node a Task is at) duplicates Tracker State. A human relabelling an issue, closing a PR, or merging manually must be translated into `Command(resume=...)`/`update_state`; anything missed leaves the graph waiting at the wrong node.

**Effort.** Medium. Graph is natural to express; you still build: webhook receiver, thread mapping, idempotent side effects, scheduler (if OSS), deployment.

---

## 2. n8n

**Status.** Sustainable Use License: "You may use or modify the software only for your own internal business purposes or for non-commercial or personal use"; files with `.ee.` require an n8n Enterprise License ([LICENSE.md](https://github.com/n8n-io/n8n/blob/master/LICENSE.md)). Internal use at KYG is permitted; embedding n8n in a product sold to others is not. Version 2.x line is current (Execute Command node "disabled by default from n8n 2.0" ([Execute Command](https://docs.n8n.io/integrations/builtin/core-nodes/n8n-nodes-base.executecommand.md))).

**Durable human waits.** Wait node modes: after interval, at specified time, on webhook call (`$execution.resumeUrl`), on form submitted. "When the workflow pauses it offloads the execution data to the database" for waits over 65 s; webhook/form modes support an optional wait limit ([Wait node](https://docs.n8n.io/integrations/builtin/core-nodes/n8n-nodes-base.wait/)).
- Caveat: "Partial executions of your workflow changes the `$resumeWebhookUrl`" so the node that hands out the URL must run in the same execution as the Wait.
- AI Agent tool calls can require human approval via Slack, Teams, Telegram, email, n8n Chat etc. ([HITL for tools](https://docs.n8n.io/build/integrate-ai/ai-examples/human-in-the-loop-for-tools.md)); docs do not state a timeout. This is tool-call approval, not a general multi-day Gate.

**Resuming from external events.** Resume URL is unique per execution, so a GitHub webhook cannot hit it directly; a routing workflow must look up which execution is waiting for which issue/PR (stored somewhere — an n8n data table or external DB) and call its `resumeUrl`. Native webhook trigger nodes and a large integration catalogue (GitHub, Jira, GitLab) make the receiving side easy.

**Scheduling.** Schedule Trigger node (built in).

**Self-hosting & cost.** Community Edition free for internal use. Production: queue mode with Redis + Postgres ("Running n8n with execution mode set to `queue` with an SQLite database isn't recommended"), optional webhook processors routing `/webhook/*` and `/webhook-waiting/*` ([queue mode](https://docs.n8n.io/deploy/host-n8n/configure-n8n/scaling/enable-queue-mode.md)). Not in Community Edition: environments and Git version control, external secrets, log streaming, multi-main, projects, SSO, sharing ([CE features](https://docs.n8n.io/deploy/host-n8n/community-edition-features.md)). Workflow-as-code in Git therefore needs Business/Enterprise or an export/import script.

**Observability / replay.** Execution list per workflow; "Debug in editor" copies a past execution's data into the editor and pins it, available on self-hosted Registered Community and above ([debug executions](https://docs.n8n.io/build/understand-workflows/understand-executions/debug-executions.md)). No time-travel of a waiting execution.

**Calling an external agent runtime.** HTTP Request node to OpenHands API, then Wait (interval loop polling, or webhook resume). OpenHands Cloud API is poll-based (see §6), so a poll loop with Wait nodes is likely.

**Multi-repo / multi-Product.** Parameterised workflows; no first-class concept. Projects (multi-team scoping) are paid.

**Second source of truth.** High. Waiting executions hold position in the workflow; Tracker changes made outside n8n must be routed back to the right execution or the execution must be cancelled. Workflow definitions live in n8n's DB unless Git source control (paid) is used.

**Effort.** Low to start (visual, integrations), rising for review→fix loops, correlation of events to executions, and testing (graph logic in a UI, not code).

---

## 3. Langflow

**Status.** `langflow` 1.12.4, released 2026-09-29, MIT ([PyPI](https://pypi.org/project/langflow/)). Positioned as "a powerful tool for building and deploying AI-powered agents and workflows" with visual builder, API and MCP serving ([repo](https://github.com/langflow-ai/langflow)).

**Durable human waits.** New and immature. v1.11.0 release notes list "durable background execution service (store + default backend)" and "durable background execution + HITL suspend/resume schema" ([releases](https://github.com/langflow-ai/langflow/releases)). The Playground prompts approve/reject "when a flow pauses for human approval from a Human Input component or an Agent" ([Playground](https://docs.langflow.org/concepts-playground)). Docs do not state whether paused runs survive restarts or how to resume via API. A nested Run Flow "cannot use Human-in-the-Loop … because a nested run cannot pause for a decision" ([Run Flow](https://docs.langflow.org/run-flow)). Open bug (2026-09-30): approving a paused human-input step fails when the paused flow contains agent tools ([issue #15480](https://github.com/langflow-ai/langflow/issues/15480)).

**Resuming from external events.** `POST /v1/webhook/{flow}` starts a flow in the background ("Task started in the background") ([Webhook](https://docs.langflow.org/webhook)); `POST /v1/run/{flow}` runs one ([API](https://docs.langflow.org/api-reference-api-examples)). No documented API to resume a specific paused run from a webhook.

**Scheduling.** Not documented.

**Self-hosting & cost.** Free, MIT, self-hostable.

**Observability.** Traces page and observability integrations ([traces](https://docs.langflow.org/traces)); no documented replay of a paused run.

**Fit.** Langflow is an agent/flow builder (the inside of a node), not a multi-day workflow orchestrator. It could implement an AFK node, but adds little next to OpenHands.

**Second source of truth / effort.** Same drift risk as any engine holding paused runs; effort high because the missing pieces (correlation, durable resume, scheduling) would be built around it.

---

## 4. Temporal

**Status.** Server MIT, v1.31.2 released 2026-07-08 ([repo](https://github.com/temporalio/temporal)). SDKs for Go, Java, TypeScript, Python, .NET, others.

**Durable human waits.** Signals and Updates deliver external input; `await workflow.wait_condition(lambda: self.approved, timeout=...)` blocks durably ([Python message passing](https://docs.temporal.io/develop/python/message-passing)). Timers last "as brief as one second to several years"; "Workers consume no additional resources while waiting for a Timer to fire"; timers survive worker/service downtime ([timers](https://docs.temporal.io/workflow-execution/timers-delays)).

**Resuming from external events.** Signal by Workflow ID from any client, CLI or other workflow; Signal-With-Start starts the workflow if absent ([messages](https://docs.temporal.io/sending-messages)). Deterministic Workflow IDs (e.g. `task:<tracker>:<repo>#<issue>`) remove the correlation table. A webhook receiver is still needed (Temporal has no HTTP webhook ingress).

**Long agent runs.** Activities with retry policies and heartbeats ("last recorded Heartbeat details are made available … on the next attempt") ([activities](https://docs.temporal.io/activities)). Asynchronous Activity completion: the activity returns without completing and "the Temporal Client can then be used from anywhere to both Heartbeat … and eventually complete the Activity Execution" — fits a remote agent that calls back, or a poller ([activity execution](https://docs.temporal.io/activity-execution)). Docs warn a long Start-To-Close timeout (e.g. one week for human review) delays retries.

**Scheduling.** Schedules with overlap policy (Skip default, BufferOne, CancelOther, AllowAll …), pause with notes, and backfill ([schedules](https://docs.temporal.io/schedule)). Fits "staging deploy every few hours, skip if one is running".

**Limits.** Event history 51,200 events or 50 MB (warning at 10,240 / 10 MB); max 2,000 pending activities/child workflows/signals per execution; use Continue-As-New for long loops ([limits](https://docs.temporal.io/workflow-execution/limits)). A review→fix loop with many iterations should continue-as-new or use a child workflow per iteration.

**Code-change risk.** Workflow code must be deterministic; changing code under open executions requires Worker Versioning (recommended) or patching ([workflow definition](https://docs.temporal.io/workflow-definition)). With Gates open for days, every workflow change ships while executions are in flight, so versioning discipline is required from day one.

**Self-hosting & cost.** Self-host: server + UI + PostgreSQL/MySQL/Cassandra, optional Elasticsearch for advanced visibility ([deployment](https://docs.temporal.io/self-hosted-guide/deployment)). Cloud: pay-as-you-go "$50 per million actions" (down to $25 with volume), no base fee; actions include workflow, activity, timer, signal, query, schedule; active storage $0.042/GB-hr ([pricing](https://temporal.io/pricing)). At this workflow's volume (tens to hundreds of Tasks/week, tens of actions each) action cost is small; operating a self-hosted cluster is the larger cost.

**Observability / replay.** Web UI shows each execution's full event history; Queries (including built-in `__stack_trace`) work even on completed workflows ([messages](https://docs.temporal.io/sending-messages)); deterministic replay of history is the core model.

**Multi-repo / multi-Product.** Namespaces and task queues; Workflow ID scheme carries Product/Repo.

**Second source of truth.** High in principle (workflow state is authoritative inside Temporal), but Signal-by-ID and Signal-With-Start make reconciliation cheap: any Tracker event can be forwarded as a signal, and a workflow can re-read Tracker State in an activity at each Gate.

**Effort.** Medium-high: new infrastructure and programming model (determinism, versioning), strongest guarantees.

---

## 5. Thin custom dispatcher (Tracker State is the source of truth)

Design: no engine-held workflow position. Each State is a Tracker label/status. A dispatcher reacts to Tracker events (webhook or poll), reads current State, and performs the one transition allowed from it (e.g. `ready-for-agent` → start OpenHands run, set `agent-running`). Gates are simply States no automation acts on until a human changes them. Persistence beyond the Tracker is limited to run bookkeeping (agent run IDs, attempt counts), ideally stored on the Tracker too (hidden comment, label, PR body).

**Hosting variants.**
- *GitHub Actions*: `issues`/`pull_request`/`pull_request_review` events (activity types include `labeled`, `unlabeled`, `assigned` …) and `schedule` ([events](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows)). Constraints: schedule shortest interval 5 min, not guaranteed to start on time ("Activity surges can cause runners to be delayed, which may cause your scheduled workflow to not start"), default branch only, auto-disabled after 60 days without activity in public repos. Events created with `GITHUB_TOKEN` do not trigger new runs (except `workflow_dispatch`/`repository_dispatch`), so label changes made by the dispatcher need a GitHub App token to chain. Job limit 6 h on GitHub-hosted, 5 days self-hosted; workflow run 35 days ([limits](https://docs.github.com/en/actions/reference/limits)). Per-Repo workflows conflict with a Product spanning many Repos unless a central repo receives `repository_dispatch` or an org-level App is used. GitHub-only: no Jira/GitLab path.
- *Small service (GitHub App webhook receiver + worker + periodic reconcile)*: GitHub webhooks must be answered "within 10 seconds", are unordered, and identified by `X-GitHub-Delivery` for idempotency ([best practices](https://docs.github.com/en/webhooks/using-webhooks/best-practices-for-using-webhooks)); "GitHub does not automatically redeliver failed deliveries" ([failed deliveries](https://docs.github.com/en/webhooks/using-webhooks/handling-failed-webhook-deliveries)) and deliveries are viewable for "the past 3 days" ([viewing deliveries](https://docs.github.com/en/webhooks/testing-and-troubleshooting-webhooks/viewing-webhook-deliveries)). A periodic reconcile poll is therefore required anyway, which also makes it adapter-friendly for Jira/GitLab.

**Durable human waits.** Trivial: a Gate is a label; waiting costs nothing and survives any restart.

**Resuming from external events.** Native: the event *is* the State change.

**Calling an external agent runtime.** Start run, record run ID on the Tracker, detect completion by poll (OpenHands Cloud API is poll-based, §6) or callback; reconcile catches lost completions.

**Observability / replay.** The Tracker timeline is the audit log; agent runs have their own logs. No built-in replay; retries are "set the label back".

**Second source of truth.** Lowest by construction. Remaining risk: the run-bookkeeping store and in-flight agent runs whose Tracker State was changed by a human (needs a cancel-on-state-change rule).

**Effort.** Low for the happy path; grows with retries, budgets, concurrency limits, timeouts on AFK runs, and fan-in (Story done when all Tasks done) — all of which an engine gives for free and here are hand-written. Tracker label semantics differ per Tracker (GitHub labels vs Jira workflow statuses), so the Tracker adapter carries more weight.

**Hybrid note (trade-off, not a recommendation).** Options are not exclusive: the Tracker can stay the source of truth for State while an engine (Temporal, LangGraph) runs only the *inside* of an AFK node or the scheduled deploy, i.e. short-lived executions with no Gate inside them. That keeps durable retry/heartbeat for agent runs without engine-held Gate state.

---

## 6. Agent runtime interface (relevant to every option)

OpenHands Cloud API: `POST https://app.all-hands.dev/api/v1/app-conversations` with `initial_message` and `selected_repository`; poll `GET /api/v1/app-conversations/start-tasks?ids=…` until `READY`, then poll `GET /api/v1/app-conversations?ids=…` for `execution_status`; terminal states `finished`, `error`, `stuck`, `waiting_for_confirmation`. The page documents no completion webhook ([OpenHands Cloud API](https://docs.openhands.dev/openhands/usage/cloud/cloud-api)). Every option therefore needs a poll loop (or the agent posting its own completion to the Tracker, e.g. opening the PR). Ticket 02 (agent runtimes) should confirm self-hosted OpenHands callbacks.

---

## Comparison

| Criterion | LangGraph OSS / LangSmith Deployment | n8n (CE) | Langflow | Temporal | Thin dispatcher (Tracker = truth) |
|---|---|---|---|---|---|
| Durable multi-day Gate | Yes: `interrupt()` + Postgres checkpointer; node re-runs on resume | Yes: Wait node offloads to DB; webhook/form/time resume | Immature: HITL suspend/resume added v1.11, open resume bug, restart survival undocumented | Yes: signals + timers lasting years, no worker cost | Yes: a label; zero cost |
| Resume from Tracker webhook | Own receiver → `Command(resume)` by thread_id | Own correlation → per-execution `resumeUrl` | No documented resume-by-API | Receiver → Signal / Signal-With-Start by Workflow ID | Native (event is the State change) |
| Long AFK agent run | Node code; poll or interrupt-until-callback | HTTP + Wait poll loop | Could be the agent itself | Activity with heartbeat/retry or async completion | Start + poll/reconcile, hand-written retries |
| Scheduled staging deploy | Platform crons (UTC); none in OSS | Schedule Trigger | Not documented | Schedules with overlap/pause/backfill | GHA `schedule` (best-effort, ≥5 min) or service cron |
| Self-host & cost | OSS free; self-hosted Platform = Enterprise plan; Cloud LSU + DB uptime | CE free for internal use (SUL); Git envs, secrets, log streaming paid | Free, MIT | Free, MIT; Cloud $50/M actions | Free; GHA minutes or one small service |
| Multi-repo / Product | DIY (thread per Task) | DIY; Projects paid | DIY | Namespaces / IDs, DIY | Tracker hierarchy; GHA per-repo awkward, service fine |
| Observability / replay | Checkpoint history, replay/fork (re-executes side effects), LangSmith traces | Execution list, debug-in-editor | Traces | Full event history UI, queries, deterministic replay | Tracker timeline + agent logs; no replay |
| Drift vs Tracker | High (graph position) | High (waiting executions) | High | High but cheap to reconcile via signals by ID | Lowest (bookkeeping only) |
| Workflow-change risk | Changing graph under open threads (undocumented migration story) | Edits apply to new executions; waiting ones resume on old/new definition (not documented) | n/a | Determinism; Worker Versioning/patching required | Low: logic is stateless per transition |
| Build effort | Medium | Low start, high for loops/correlation | High (gaps) | Medium-high (new infra + model) | Low happy path, grows with retries/fan-in/budgets |
| Licence | MIT (lib); Platform proprietary | Sustainable Use (internal OK) | MIT | MIT | Own code |

## Facts the decision ticket will need

1. LangGraph `interrupt()` waits indefinitely but **re-runs the whole node on resume**; side effects before it must be idempotent ([interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts)).
2. **Self-hosted LangSmith Deployment (incl. standalone Agent Server) requires an Enterprise plan**; Developer/Plus are cloud-only. OSS LangGraph alone has no HTTP API, cron or webhook features ([self-hosted](https://docs.langchain.com/langsmith/deploy-to-self-hosted-overview), [pricing](https://www.langchain.com/pricing)).
3. n8n is **Sustainable Use licensed** (internal business use OK); Git-based environments, external secrets and log streaming are **not in Community Edition**. Queue mode needs Postgres + Redis ([license](https://github.com/n8n-io/n8n/blob/master/LICENSE.md), [CE features](https://docs.n8n.io/deploy/host-n8n/community-edition-features.md)).
4. n8n Wait resume URLs are **per execution**; Tracker events need a correlation lookup to find the waiting execution ([Wait](https://docs.n8n.io/integrations/builtin/core-nodes/n8n-nodes-base.wait/)).
5. Langflow HITL suspend/resume arrived in v1.11 (2026) with an **open bug on approving paused agent-tool flows** (opened 2026-09-30) and no documented API resume or scheduler ([issue #15480](https://github.com/langflow-ai/langflow/issues/15480), [Run Flow](https://docs.langflow.org/run-flow)).
6. Temporal: timers up to years at no worker cost; Signal-With-Start by deterministic Workflow ID; Schedules with overlap policy; **history cap 51,200 events / 50 MB** (Continue-As-New for long review loops); **Worker Versioning required** for code changes under open executions ([timers](https://docs.temporal.io/workflow-execution/timers-delays), [limits](https://docs.temporal.io/workflow-execution/limits), [workflow definition](https://docs.temporal.io/workflow-definition)).
7. Temporal Cloud: $50 per million actions (signals, timers, activities all count), no base fee; self-host needs Postgres/MySQL/Cassandra + optional Elasticsearch ([pricing](https://temporal.io/pricing), [deployment](https://docs.temporal.io/self-hosted-guide/deployment)).
8. GitHub webhooks: 10 s response deadline, unordered, **no automatic redelivery**, 3-day delivery history. Any webhook-driven design needs a **periodic reconcile poll** regardless of engine ([best practices](https://docs.github.com/en/webhooks/using-webhooks/best-practices-for-using-webhooks), [failed deliveries](https://docs.github.com/en/webhooks/using-webhooks/handling-failed-webhook-deliveries)).
9. GitHub Actions as dispatcher: `schedule` is best-effort (may be delayed or skipped, ≥5 min, default branch only); `GITHUB_TOKEN`-made changes don't trigger workflows (need an App token); 6 h job limit on hosted runners; inherently GitHub-only ([events](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows), [limits](https://docs.github.com/en/actions/reference/limits)).
10. OpenHands Cloud API is **poll-based** (no documented completion webhook); terminal states include `stuck` and `waiting_for_confirmation`, which need mapping to States ([OpenHands Cloud API](https://docs.openhands.dev/openhands/usage/cloud/cloud-api)).
11. Every engine option introduces engine-held workflow position alongside Tracker State; the decision hinges on whether Gates live **inside** the engine (drift risk, reconciliation needed) or **only on the Tracker** (engine, if any, scoped to AFK node internals and the scheduled deploy).

## Gaps / not verified

- n8n behaviour when a workflow is edited while executions are waiting (docs silent).
- LangGraph migration story for changing a graph with open checkpointed threads (not found in docs fetched).
- Self-hosted OpenHands callback/webhook support (defer to ticket 02).
- GitHub repo metadata (stars, latest tags) could not be read via the API in this session; versions taken from PyPI and the repos' web pages.
