---
title: Choose the orchestration architecture
label: wayfinder:grilling
type: HITL
blocked_by: [01-research-orchestration-engines, 03-grilling-canonical-state-model]
assignee:
status: open
---

## Question

Should workflow logic live in a separate orchestrator (LangGraph / n8n / Temporal / other) or in a thin dispatcher where the Tracker's State is the only source of truth, and which tool exactly?
