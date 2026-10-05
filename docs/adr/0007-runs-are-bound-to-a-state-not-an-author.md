# A live run is bound to its State, not to who wrote the label; humans steer the loop with Human Findings

The Dispatcher writes labels and comments with a human's Tracker credentials (ADR 0006), so no Tracker event tells its writes from a human's. ADR 0001 still needs "a State change the Dispatcher did not make cancels a live run". We therefore bind every live run to the State it started in and never ask who changed a label:

- The Dispatcher leaves a run's State only when that run finishes, in this order: mark the run finished in Run bookkeeping, then write the next State with compare-and-set from the run's State (ADR 0003).
- An item whose State differs from a run still marked live was moved by a human: the run is cancelled and its result discarded.
- A human and the Dispatcher changing the same item within seconds: the Dispatcher's compare-and-set fails against the human's State, the run's result is discarded, the human's change stands.
- Run bookkeeping only decides whether to cancel a run, never what a State is, so ADR 0001 holds.

Comments are recognised by signature (ADR 0006), tightened: the HMAC covers the whole body, the Story and the round, and a marker inside a quote is ignored. Humans steer the review → fix loop only through a **Human Finding** (`/weave finding` on the Story): High, numbered within the round (`R2-H1`), answered by the next fix run, limited to a bug or an unmet existing Acceptance criterion (new scope goes through grilling). Posted at the proofs wait or the human review Gate it counts as a rejection; declined by the fixer it sends the Story to `needs-human` at once.

## Considered Options

- **A signed Workflow comment with every State change** — Tracker-held and portable, but doubles every write, can't be atomic with the label write, and webhook order between the two isn't guaranteed.
- **Compare each label event to the last State the Dispatcher wrote** — works most of the time, but a human setting the same label is indistinguishable, and it needs per-item write history.
- **A service account where the Tracker allows one** — no bot account exists, and it would behave differently per Tracker.
- **Every human comment as context for the next run** — chatter and questions become instructions; runs stop being repeatable.

## Consequences

- The Dispatcher cannot attribute label changes; a human setting the State the Dispatcher would have set looks identical, which is harmless.
- PR comments are never read, including `/weave finding` posted on a PR.
