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
A mark on an Acceptance criterion saying no agent can capture its Proof, so a human checks it at review instead. Set only in the Grilling session, never by an agent, and only for a fixed list of reasons: the Repo has no Run recipe; it needs iOS; it is mobile UI while Environments cannot run an Android emulator; or it depends on an outside system that has no test account or sandbox. The criterion is still tested.

## Run recipe
A file committed in a Repo that says how to bring that Repo's software up in an **Environment**: what to build, which other Repos it depends on, which services it needs, what data to seed, which secrets it uses, and how to tell it is ready. Changes with the code, in the same PR. A Repo without a Run recipe cannot be run by agents: its Acceptance criteria are **Not provable here**.
_Avoid:_ "setup", "dev env" (both also mean a developer's machine).

## Environment
An isolated, throwaway set of containers running a Story's software for one agent run (implement, review or proofs), brought up from Run recipes and seeded fresh. Each Repo in it is at: the agent's working copy for the Task being worked on; the Task branch for any other Repo the Story touches; the **Base branch** for a Repo the Story does not touch. A proof round uses one Environment for every Proof of the Story, captured one after another, once all the Story's Tasks have passed agent review; a round after rework starts from a fresh one.
_Avoid:_ "staging" (the shared deploy target after merge), "sandbox" alone (the agent's own workspace), "proof environment".

## Base branch
The branch of a Repo that an Environment uses when the Story has no Task in that Repo (e.g. `dev`). Configured per Repo.

## Checks
The test suite, lint, typecheck and build of a Repo, run by the Dispatcher itself against an agent's work, never taken on an agent's word.
_Avoid:_ "CI" (the Code host's own pipeline, which may run different things).

## Agent review
The workflow step, after implementation, in which a separate agent run (its own Agent profile, preferably a different model from the implementer's) reviews all of a Story's PRs and reports Findings. Distinct from any review an implement run does of its own work before it finishes, which is part of implementing and never decides a State.
_Avoid:_ "code review" alone (also names the implementer's self-review and the human's review).

## Finding
One problem the Agent review reports on a Task's change. Either **blocking** (a bug, an unmet Acceptance criterion, a broken test, a test that is off-Seam, untraced to an Acceptance criterion, tautological, implementation-coupled or structural, a breach of the Product's coding standards, a security issue) or **non-blocking** (a style nit, naming, an optional refactor). Only blocking Findings cause a fix; non-blocking ones are passed to the human reviewer.

## Review passed
A Story's PRs have no blocking Findings and their Checks are green.

## Review round
One pass of a Story's review → fix loop: a fix run, then Checks, then Agent review. Counted on the Tracker from the Story's comments since the loop last started; the loop starts afresh only on a failing Proof, a human rejection at review, or a human moving the Story out of needs-human.

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
The service that moves Stories and Tasks from one State to the next. It reads State from the Tracker and writes State back to it, but never holds State itself; what it keeps of its own is **Run bookkeeping**.

## Agent profile
A named pairing of an agent (e.g. Cursor, Claude Code, or a model-agnostic loop) and a model, used for an agent run. A Product sets a default Agent profile per workflow node; a Task may override it.
_Avoid:_ "agent" alone when the model matters too.

## Run bookkeeping
What the Dispatcher remembers about work in flight: agent run IDs, infrastructure retry counts, deploy batches. Never consulted to decide a State; anything that decides one, such as Review rounds, is kept on the Tracker instead.
_Avoid:_ "workflow state" for this.

## Grilling session
A live, human-in-the-loop conversation held in the editor that turns a Story into specs and Tasks.

## Grilling record
A comment posted on the Story when a Grilling session ends: decisions made, questions asked, links to research and prototypes. Historical only; downstream agents work from specs and Tasks, not from the record.
