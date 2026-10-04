# The Tracker is the only workflow state; no orchestration engine

Gates wait hours to days, humans change Tracker State by hand, and GitHub never redelivers failed webhooks, so any engine that holds a Task's place at a Gate (Temporal, LangGraph, n8n) becomes a second source of truth that must be reconciled against the Tracker anyway. We therefore keep every State and every Gate on the Tracker and drive transitions with a small Python Dispatcher (webhook receiver, job worker, reconcile poll every 5 minutes). Agent runs and the staging deploy are plain jobs started and polled by the Dispatcher; the Dispatcher's own database holds only Run bookkeeping and is never read to decide a State.

## Considered Options

- **Temporal** — strongest durable waits and retries, but a cluster to operate, determinism and Worker Versioning under days-long open executions, and still a mirror of Tracker State.
- **LangGraph** — `interrupt()` re-runs the node on resume; self-hosted server needs an Enterprise plan; OSS has no webhooks or cron.
- **n8n** — per-execution resume URLs need a correlation table; Git-based workflows are paid.
- **GitHub Actions as the Dispatcher** — GitHub-only and per-Repo, which breaks the Tracker adapter and multi-Repo Product rules.

## Consequences

- Retries, timeouts, concurrency limits and fan-in (Story derived from its Tasks) are written by hand in the Dispatcher. If retry logic grows beyond retry-once, revisit running agent runs as Temporal activities (never across a Gate).
- Any State change the Dispatcher did not make while an agent run is live cancels that run and discards its result.
