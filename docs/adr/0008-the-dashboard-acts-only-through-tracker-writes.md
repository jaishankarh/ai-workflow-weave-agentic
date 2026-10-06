# The Workflow dashboard can act, but only by writing to the Tracker

The deployables decision made each Dispatcher's dashboard read-only, so every human action happened on the Tracker. We now want to manage the workflow from the dashboard too: approve, restart, move a State, post a Human Finding. We keep ADR 0001 by making every dashboard action a Tracker write through the Tracker adapter, exactly what a human would do on the Tracker:

- The dashboard holds no State and no Gate of its own; it reads them from the Tracker like the Dispatcher does.
- A dashboard action is the same Tracker write a human would make: a State label change by compare-and-set (ADR 0003), or a comment such as a `/weave finding` Human Finding.
- So the workflow cannot tell a dashboard action from one taken on the Tracker, and needs to: a State moved from the dashboard cancels a live run exactly as a label change on the Tracker does (ADR 0007).

## Considered Options

- **Keep it read-only** (the earlier decision): one place to act, nothing to build, but the user wants to manage the workflow from one screen across Repos and Stories.
- **Let the dashboard hold State and sync it to the Tracker:** faster to build, but two sources of truth, which is what ADR 0001 rules out.

## Consequences

- Every dashboard action needs a Tracker adapter write; anything the adapter can't express can't be done from the dashboard.
- The dashboard needs a login, and its writes go out with the Product's Tracker credentials, so they are attributed on the Tracker to that human, not to whoever clicked.
- The Tracker remains a complete place to act; the dashboard is a convenience, never a requirement.
