---
title: Research remote agent runtimes (OpenHands vs headless alternatives)
label: wayfinder:research
type: AFK
blocked_by: []
assignee:
status: open
---

## Question

What does each candidate runtime offer for running the AFK nodes (implement, code review, fix-review-comments, proofs, deploy) remotely and headlessly?

Compare OpenHands (cloud and self-hosted, SDK/API, sandboxing), Claude Code headless / Claude Agent SDK, and Codex. For each: how a run is started and observed programmatically, how custom skills and repo files (CONTEXT.md, coding-standards.md) are loaded, multi-repo checkout, running a full test suite and browsers for screenshots inside the sandbox, secrets handling, cost/concurrency limits, and how results come back (PR, comments, artifacts).
