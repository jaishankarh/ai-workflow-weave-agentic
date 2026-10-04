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
The mapping between a Code host's PRs and the workflow: PR status, merge, and the PR's link back to its Task. Paired freely with any Tracker adapter.

## Story
One idea, feature request or bug. May span Repos, and names every Repo it touches. Lives in the Product's Tracker, never in a code Repo. Jira: L0 story, its Repos as components. GitLab: an issue in the Product's story repo, its Repos as labels.

## Task
A sub-task of a Story. Touches **exactly one Repo** and produces **exactly one PR**. A Task needing two Repos is split. Lives under its Story in the Tracker and names its one Repo; the PR in that Repo links back to it. Jira: sub-task of the L0 story. GitLab: child task of the Story issue.

## PR
The single change request a Task produces in its Repo. GitLab calls it a merge request (MR); the two words are interchangeable.

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
What the Dispatcher remembers about work in flight: agent run IDs, attempt counts, deploy batches. Never consulted to decide a State.
_Avoid:_ "workflow state" for this.

## Grilling session
A live, human-in-the-loop conversation held in the editor that turns a Story into specs and Tasks.

## Grilling record
A comment posted on the Story when a Grilling session ends: decisions made, questions asked, links to research and prototypes. Historical only; downstream agents work from specs and Tasks, not from the record.
