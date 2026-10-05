# One implement run and one review → fix loop per Story; one PR per Repo per Story

Grilling produces one spec per Story whose tickets are the Story's Tasks, and the `implement-spec` skill builds a whole spec in one run: Tasks in parallel worktrees, TDD, its own self-review, everything merged into an Integration branch. We therefore run implementation once per Story, extend `implement-spec` to keep one **Integration branch per Repo**, and open **one PR per Repo per Story** carrying every Task of the Story in that Repo. The skill runs as written, self-review included; that self-review is part of implementing and never decides a State.

After it finishes, a separate **Agent review** loop runs per Story, driven by the Dispatcher:

1. The Dispatcher runs **Checks** (full test suite, lint, typecheck, build) in every touched Repo, in the Story's Environment. Red → straight to a fix run, no Agent review spent.
2. Green → an Agent review run (its own Agent profile, preferably a different model) reviews all the Story's PRs and returns **Findings**, each blocking or non-blocking.
3. No blocking Findings → **Review passed**: every Task → `awaiting-story-proofs`. Non-blocking Findings go to the human reviewer as a comment.
4. Blocking Findings → a **fix run** (the implementer's Agent profile, blocking Findings and/or Checks output as input, `tdd` per fix, committing to the existing Integration branches; it may decline a Finding with a reason) → back to 1.

A round is one fix run with its Checks and Agent review. ~~The loop sends the Story's Tasks to `needs-human` after **3 rounds** (Product config) or as soon as a round ends with the **same** blocking Finding or failing test as the round before.~~ *Amended by [ADR 0006](0006-fix-runs-answer-findings-and-stop-on-repeats.md):* the loop stops on the **Repeat limit** instead: a round whose Repeats are all Low stops at once; a High Repeat keeps the loop going until some Finding's chain reaches the Product's configured length (default 3); a loop reporting only new Findings runs until the Product's round backstop.

The round count decides a State, so it is kept on the Tracker, not in Run bookkeeping: the Dispatcher posts a review-result comment per round and a loop-start comment (with its reason) each time the Story enters the loop, and counts review results since the last loop start. A loop starts only on: a failing Proof, a human rejection at review, or a human moving the Story out of `needs-human`. Fix runs, Checks reruns, infrastructure retries and agents never reset it.

## Considered Options

- **One implement run and PR per Task** — keeps "one Task, one PR", but dependent Tasks in one Repo need stacked PRs merged in order by hand, and `implement-spec` would be cut down to a single ticket.
- **Dropping `implement-spec`'s own code review** — avoids a double review, but loses the review it does while building; the two reviews have different jobs.
- **Checks after every Task inside the implement run** — far slower; `implement-spec` already runs single tests as it goes and the full suite once at the end.
- **Round count in the Dispatcher's database** — simpler, but it would be bookkeeping that decides a State, which ADR 0001 forbids.
- **Loop States on the Story instead of its Tasks** — names the true unit, but breaks "a Story is derived from its Tasks while they are built".

## Consequences

- A Task is still exactly one Repo, but no longer exactly one PR; a PR links back to every Task it carries.
- All of a Story's Tasks move through `agent-running`, `agent-review` and `agent-fixing` together, and the Story's derived label always matches them. If a human changes one Task's State mid-loop, the run is cancelled and the Story is flagged and left until the Tasks agree again; the Dispatcher never guesses.
- A blocking Finding or red Check in one Repo holds the whole Story in the loop.
- A Task added to the Story later pulls the whole Story into a new implement run.
- `implement-spec` must be extended to handle several Repos (one Integration branch and worktree set per Repo) before build.
