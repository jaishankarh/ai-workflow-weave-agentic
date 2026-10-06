# Context: AI Workflow Weave

Glossary only. No implementation details.

## Product
One logical platform (the "super project"). Bound to exactly one **Tracker**; spans many **Repos**.
_Avoid:_ "Project" (GitHub, GitLab and Jira each use it for something else).

## Repo
One code repository belonging to a Product. Hosted on exactly one **Code host**; the Repos of one Product may sit on different Code hosts.

## Tracker
The issue platform a Product uses (GitHub Issues, GitLab, Jira). Reached only through a **Tracker adapter**.

## Tracker adapter
The mapping between canonical **States** and a Tracker's labels and hierarchy. A State is always a label; a Tracker's own status, where it has one, only mirrors the State for humans and is never read to decide it. Covers everything about Stories and Tasks; knows nothing about PRs.

## Code host
The platform a Repo's code and PRs live on (GitLab, Bitbucket, GitHub). Independent of the Tracker: a Jira Product may keep its Repos on Bitbucket and GitLab.

## Code host adapter
The mapping between a Code host's PRs and the workflow: PR status, merge, and the PR's links back to its Tasks. Paired freely with any Tracker adapter.

## Story
One idea, feature request or bug. Has exactly one **Kind** and names at least one Repo from the moment it is created. May span Repos, and names every Repo it touches; grilling may change which. Lives in the Product's Tracker, never in a code Repo. Jira: L0 story, its Repos as components. GitLab: an issue in the Product's story repo, its Repos as labels.

## Kind
Whether a Story is a bug or a feature. Exactly one per Story, set at intake, changeable at triage.

## Intake session
A live, human-in-the-loop session in the editor where pasted meeting notes or transcripts (freeform, from any source) are split into Stories. Each item becomes a new Story, detail added to an existing Story, a Follow-up Story, or is left out; nothing reaches the Tracker until the human confirms it.
_Avoid:_ "import", "ingest" (both suggest an unattended step).

## Meeting notes
Whatever the human pastes into an Intake session: a transcript, the client's own notes, a summary. Never an item in the Tracker; each Story created from them quotes its excerpt and carries the full notes as an attachment.

## Parked
A Story State for work recorded now but deliberately not started. The Dispatcher never acts on it; only a human moves it on, to triage.
_Avoid:_ "backlog" (on most boards that means "next up").

## Task
A sub-task of a Story, and one ticket of the Story's spec. Touches **exactly one Repo**; a Task needing two Repos is split. A Story may have several Tasks in the same Repo, and they all land in that Repo's one PR for the Story. Lives under its Story in the Tracker and names its one Repo; the PR in that Repo links back to it. Jira: sub-task of the L0 story. GitLab: child task of the Story issue.

## PR
The single change request a Story produces in one Repo, opened from that Repo's **Integration branch** and covering every Task of the Story in that Repo. The unit of human review and merge. GitLab calls it a merge request (MR); the two words are interchangeable.
_Avoid:_ "the Task's PR" (a PR may carry several Tasks).

## Acceptance criterion
One testable statement on a Task of what its change must do, written in the Grilling session, with any edge cases grilling names alongside it. Every test an agent writes traces to exactly one Acceptance criterion and is named after it; a test that traces to none is a blocking Finding.

## Seam
A public boundary of a Repo's code where a Task's tests observe behaviour (an API endpoint, a module's public function, a CLI command). Agreed in the Grilling session and listed on each Task; an agent writes tests only at its Task's Seams and never adds one itself.
_Avoid:_ "test point", "unit" (both invite testing internals).

## Integration branch
The one branch per Story per Repo into which every Task of that Story in that Repo is merged as it is built. A PR is opened from it.

## Proof
Evidence, captured by an agent from the running software, that one Acceptance criterion works: screenshots or a recording for UI, the request, response and server log lines for an API, the command and its output for a CLI or job. Every Acceptance criterion has one, unless it is Not provable here. A test-run report travels with Proofs but is never a Proof on its own. A Proof that shows the thing failing is a defect, not a Proof. An Acceptance criterion the proofs agent cannot prove counts as a failing Proof.
_Avoid:_ "logs" alone, "test results" as proof, "spec item" (say Acceptance criterion).

## Not provable here
A mark on an Acceptance criterion saying no agent can capture its Proof, so a human checks it at review instead. Set only in the Grilling session, never by an agent, and only for a fixed list of reasons: the Repo has no Run recipe; it needs iOS; or it depends on an outside system that has no test account or sandbox. The criterion is still tested.

## Run recipe
A file committed in a Repo that says how to bring that Repo's software up in an **Environment**: what to build, which other Repos it depends on, which services it needs, what data to seed, which secrets it uses, and how to tell it is ready. Changes with the code, in the same PR. A Repo without a Run recipe cannot be run by agents: its Acceptance criteria are **Not provable here**.
_Avoid:_ "setup", "dev env" (both also mean a developer's machine).

## Environment
An isolated, throwaway set of containers running a Story's software for one agent run (implement, review or proofs), brought up from Run recipes and seeded fresh. Each Repo in it is at: the agent's working copy for the Task being worked on; the Task branch for any other Repo the Story touches; the **Base branch** for a Repo the Story does not touch. A proof round uses one Environment for every Proof of the Story, captured one after another, once all the Story's Tasks have passed agent review; a round after rework starts from a fresh one.
_Avoid:_ "staging" (the shared deploy target after merge), "sandbox" alone (the agent's own workspace), "proof environment".

## Base branch
The branch of a Repo that an Environment uses when the Story has no Task in that Repo (e.g. `dev`). Configured per Repo.

## Checks
The test suite, lint, typecheck and build of a Repo, run by the Dispatcher itself against an agent's work, never taken on an agent's word. Also runs the tests a PR adds against the Base branch, where each must fail (tests for a pure refactor excepted); one that passes there tests nothing.
_Avoid:_ "CI" (the Code host's own pipeline, which may run different things).

## Agent review
The workflow step, after implementation, in which a separate agent run (its own Agent profile, preferably a different model from the implementer's) reviews all of a Story's PRs, reports every Acceptance criterion of every Task as met or unmet, and reports Findings. A review that leaves any Acceptance criterion out is a failed run, never a pass. It judges only tests the PRs add or change. Distinct from any review an implement run does of its own work before it finishes, which is part of implementing and never decides a State.
_Avoid:_ "code review" alone (also names the implementer's self-review and the human's review).

## Finding
One problem the Agent review reports on a Task's change, with an id unique within its Review round (e.g. `R2-F3`). Either **blocking** or **non-blocking** (a style nit, naming, an optional refactor). Only blocking Findings cause a fix; non-blocking ones are passed to the human reviewer. A blocking Finding has a **Severity** fixed by its category:
- **High:** a bug, an unmet Acceptance criterion, an Acceptance criterion with no test, a broken test, a security issue.
- **Low:** a breach of the Product's coding standards; a test that is off-Seam, untraced to an Acceptance criterion, tautological, implementation-coupled or structural.

## Human Finding
A blocking Finding a human writes as a comment on the Story starting with the `/weave finding` command, numbered within its round (e.g. `R2-H1`) and always High. It may only name a bug or an unmet existing Acceptance criterion; new scope goes through grilling as a new Task or a Follow-up Story. Posted while a run is live, it waits for the next fix run; posted while the Story waits for Proofs or human review, it counts as a rejection. The fix run must answer it; a declined Human Finding sends the Story to needs-human at once, never to the Agent review.
_Avoid:_ "instruction", "feedback comment" (both suggest any comment steers the loop).

## Repeat
A Finding the Agent review reports again after an earlier round's Finding was answered (fixed or declined), naming the Finding it repeats (`R3-F1 repeats R2-F3`). A Finding and its Repeats form one chain; the chain's length is how many times the problem has been reported.
_Avoid:_ "same Finding" without naming which one.

## Review result
The comment the Dispatcher posts on the Story at the end of each Review round, carrying that round's Findings and the ruling on every Acceptance criterion. The only place Findings are kept; any copy on a PR is a mirror for humans and never read back.

## Fix reply
The comment the Dispatcher posts on the Story at the end of a fix run, answering every blocking Finding of the last Review result by id: fixed (with its commit) or declined (with a reason). A decline is judged by the next Agent review, which either drops the Finding or reports it as a Repeat.

## Workflow comment
A Review result, Fix reply, or other comment the Dispatcher posts that the workflow reads back. Recognised only by a signed marker in its body, never by its author, since the Dispatcher posts with a human's credentials. The signature covers the whole body, the Story and the round, so an edited or copied Workflow comment fails it; a marker inside a quote is ignored. A comment with no valid signature is human discussion and never affects the workflow, unless it is a Human Finding; a marked comment whose signature fails is flagged, never guessed.

## Review passed
A Story's PRs have no blocking Findings and their Checks are green.

## Review round
One pass of a Story's review → fix loop: a fix run, then Checks, then Agent review. Counted on the Tracker from the Story's Workflow comments since the loop last started; the loop starts afresh only on a failing Proof, a human rejection at review, or a human moving the Story out of needs-human.

## Repeat limit
The rule that ends a review → fix loop that is stuck rather than making progress, sending the Story to needs-human. If a round's Repeats are all Low, the Story stops at once. If any Repeat is High, the loop goes on until some chain reaches the Product's configured length (default 3). A loop that only ever reports new Findings goes on until the Product's round backstop.

## State
A named, unambiguous stage of a Story or Task in the workflow graph (e.g. ready-for-agent, ready-for-human-review). Replaces the overloaded word "done".

A Story's State is set directly until its Tasks exist, is derived from its Tasks while they are built, and is set directly again after every Task is merged.

## Invalid State
A Story or Task whose Tracker shows no State or more than one. The Dispatcher never guesses a State from it: it flags the item and leaves it untouched until a human fixes it.

## Follow-up Story
A new Story linked to an earlier one, created when work is discovered after the earlier Story was tasked.
_Avoid:_ reopening the earlier Story or moving it back to grilling.

## Deploy blocker
A link saying a Story cannot go to staging until other work is merged. The other work is either a Task added to the same Story or a Follow-up Story. Work that is nice to have, rather than required, never becomes a Deploy blocker.

## Gate
A State where the workflow waits for a human (grilling, triage, human review, merge). A Gate exists only as a State on the Tracker; nothing else holds a Story or Task's place while it waits.

## Dispatcher
The deterministic service that moves Stories and Tasks from one State to the next; one per Product. It starts agent runs and Checks and collects their outcomes, but never judges code itself and never waits on a run. It reads State from the Tracker and writes State back to it, but never holds State itself; what it keeps of its own is **Run bookkeeping**.

## Sandbox host
The one machine where every agent sandbox and Environment runs, shared by all Products, each Product capped in how many runs it may have at once. The Dispatchers live on it too.
_Avoid:_ "server" alone, "staging" (the shared deploy target after merge).

## Run dashboard
A read-only page each Dispatcher serves showing its Run bookkeeping: live, queued and retried runs, their Agent profiles, durations, logs and cost. Never a place to act: every human action, from approving to restarting, happens on the Tracker.
_Avoid:_ "console", "control panel" (both suggest it can change things).

## Agent profile
A named pairing of an agent (e.g. Cursor, Claude Code, or a model-agnostic loop) and a model, used for an agent run. A Product sets a default Agent profile per workflow node; a Task may override it.
_Avoid:_ "agent" alone when the model matters too.

## Agent toolset
The skills and MCP servers staged into one agent run. Skills come from the central skills repo plus the Repo's own; MCP servers come from a Product-wide catalog. A database MCP server only ever connects to that run's own Environment, wired from the services its Run recipes bring up, and is enabled only when a Run recipe brings up that database; never to a shared or production database.
_Avoid:_ "plugins", "tools" alone (both also name an agent's built-in abilities).

## Run bookkeeping
What the Dispatcher remembers about work in flight: agent run IDs, infrastructure retry counts, deploy batches. Never consulted to decide a State; anything that decides one, such as Review rounds, is kept on the Tracker instead.
_Avoid:_ "workflow state" for this.

## Grilling session
A live, human-in-the-loop conversation held in the editor that turns a Story into specs and Tasks.

## Grilling record
A comment posted on the Story when a Grilling session ends: decisions made, questions asked, links to research and prototypes. Historical only; downstream agents work from specs and Tasks, not from the record.
