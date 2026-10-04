---
label: wayfinder:map
title: Developer workflow as an executable graph
---

## Destination

A build-ready spec of the developer workflow graph (every State, transition, Gate, failure and retry path, and the agent/skill and tool behind each node) precise enough that `to-tickets` can slice it into agent-executable build work.

## Notes

- Domain: developer workflow automation. Vocabulary lives in `/CONTEXT.md` (Product, Repo, Story, Task, State, Gate, Grilling session, Grilling record). Use those words.
- Skills every session should consult: `grill-with-docs` + `domain-modeling` for grilling tickets; `research` for research tickets; `prototype` for prototype tickets.
- Standing constraints (decided while charting):
  - Tracker-agnostic: GitHub first, Jira and GitLab must plug in via a Tracker adapter.
  - A Product spans many Repos; a Story may span Repos; a Task is exactly one Repo and one PR (hard rule).
  - Grilling and triage are live sessions in the editor; a Grilling record is posted on the Story afterwards as history only.
  - Merge to main stays manual (human).
- Tracker for this map: local markdown under `wayfinder/` until the Claude GitHub App has write access to this repo; then migrate to GitHub Issues (label `wayfinder:map`, child issues, native blocking).

## Decisions so far

<!-- one line per closed ticket -->

## Not yet specified

- **Staging deploys:** what counts as a deployable unit per Product (backends, frontends, APKs, other), batching cadence ("every few hours"), rollback on failed deploy, and the hand-off to docs updates.
- **Docs updates after deploy:** which docs (user, API, CONTEXT, changelog), who writes them, and whether that is its own State.
- **Notifications:** which channel tells you "ready for your review" / "deploy finished", and what the review link bundles (code explainer, show-me, review results, test runs, proofs).
- **Human review Gate and rework loop:** review at Task (PR) level vs Story rollup; how a rejection comment routes back to an agent; how re-proofs are triggered.
- **Skill fixes as part of the spec:** content of `coding-standards.md`; blast-radius and affected-feature check in grilling; rules for implement/TDD to avoid tautological and structural tests.
- **Bug path:** when a bug skips grilling and goes triage → implement directly vs needs a spec.
- **Operational guardrails:** concurrency limits, cost/time budgets per agent run, secrets for sandboxes, observability of runs.

## Out of scope

<!-- work ruled beyond the destination -->
