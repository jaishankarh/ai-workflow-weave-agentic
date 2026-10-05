# Fix runs answer every Finding by id; the loop stops on Repeats, not a flat round count

ADR 0005 left the fix run as "the implementer's Agent profile with `tdd`" and stopped the loop after 3 rounds or a repeated Finding. That cuts off loops that are making progress and lets a stuck loop run its full 3 rounds, and the next review had no reliable way to see what the fixer did. We therefore give the fix run its own skill, `fix-findings` (central skills repo, calling `tdd` for High Findings), and make Findings addressable:

- Every Finding has an id within its round (`R2-F3`) and, if blocking, a **Severity** fixed by its category: **High** (bug, unmet Acceptance criterion, criterion with no test, broken test, security) or **Low** (coding-standards breach, off-Seam, untraced, tautological, implementation-coupled or structural test). Each red Check is answered the same way (`R2-C1`), including a new test that passes on the Base branch.
- The fix run must answer every blocking Finding and red Check in a **Fix reply**: `fixed` with its commit, or `declined` with a reason. Red Checks cannot be declined. It changes nothing beyond what it answers; anything else it notices goes in the Fix reply for the human.
- Test-first for High Findings extends the Acceptance criterion's **existing** test with the missing case; a new test is written only for a criterion that has none. A bug no criterion covers is fixed and flagged in the Fix reply as a spec gap; the fixer never invents a criterion or a test named after a fix. Low Findings are fixed without new tests.
- The next Agent review judges every decline and every claimed fix: a problem it reports again is a **Repeat** naming the Finding it repeats.
- **Repeat limit** (replaces the flat 3 rounds): all-Low Repeats in a round → `needs-human` at once; any High Repeat → loop on until some chain reaches the Product's configured length (default 3); only new Findings → loop on until the Product's round backstop (cost guard).
- The fixer uses the implementer's Agent profile; review keeps preferring a different one.

Review results and Fix replies live only as **Workflow comments** on the Story, posted through the Tracker adapter, so this works on every Tracker. Because the Dispatcher posts with a human's credentials, a Workflow comment is recognised by an HMAC-signed marker in its body (`weave:review-result round=2 sig=…`), never by author; unsigned comments are human discussion and ignored, a marked comment whose signature fails is flagged. PR inline comments may mirror Findings for humans and are never read back.

There is one deterministic Dispatcher per Product (its own config, database, credentials and signing secret), split internally into one handler per step with a job queue and concurrency limit per run kind. It never judges code and never waits on a run.

## Considered Options

- **`tdd` plus a prompt, no fix skill** — every Agent profile would answer Findings in its own format, and the review couldn't tell fixed from ignored.
- **Findings stored as PR review comments** — line-anchored and pleasant, but a Story spans several PRs and the Code host adapter knows nothing about Tasks or States.
- **Recognise Workflow comments by author (a bot account)** — no bot account exists; the Dispatcher posts as the human.
- **Comment ids kept in the Dispatcher's database** — Run bookkeeping deciding a State, forbidden by ADR 0001.
- **Separate Dispatchers for implementation and the review loop, or an agent loop per Story** — each would see the other's label writes as foreign changes and cancel live runs; an LLM deciding transitions is nondeterministic and a second holder of State.
- **One Dispatcher for all Products** — fewer deployments, but one Product's credentials and outages would reach every Product.

## Consequences

- `fix-findings` and the review skill's structured Findings output (ids, Severity, Repeats) must be written before build; `test-rules.md` gains the rule that fixes extend existing criterion tests.
- Product config gains the Repeat-chain length and the round backstop.
- Separate per-Product Dispatchers can't coordinate shared subscription quota; ADR 0002's stall-and-retry covers it.
- ADR 0001's "a State change the Dispatcher did not make cancels the live run" can no longer rely on author either; how the Dispatcher recognises its own label writes is an open question.
