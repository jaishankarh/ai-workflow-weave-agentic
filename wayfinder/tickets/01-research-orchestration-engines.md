---
title: Research orchestration engines for a gated, long-running ticket workflow
label: wayfinder:research
type: AFK
blocked_by: []
assignee:
status: open
---

## Question

Which orchestration options fit a workflow whose nodes are long-running agent runs and whose Gates wait hours or days for a human, driven by tracker events?

Compare LangGraph (incl. LangGraph Platform, interrupts, checkpointing), n8n, Langflow, Temporal, and a thin custom state machine where tracker State *is* the source of truth (webhook/poll + dispatcher). For each: durable waits on human Gates, resuming from external events (webhooks), self-hosting, multi-repo/multi-Product support, observability, how it calls an external agent runtime (e.g. OpenHands), and the risk of a second source of truth drifting from the Tracker.
