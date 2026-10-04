# Context: AI Workflow Weave

Glossary only. No implementation details.

## Product
One logical platform (the "super project"). Bound to exactly one **Tracker**; spans many **Repos**.
_Avoid:_ "Project" (GitHub, GitLab and Jira each use it for something else).

## Repo
One code repository belonging to a Product.

## Tracker
The issue platform a Product uses (GitHub Issues, GitLab, Jira). Reached only through a **Tracker adapter**.

## Tracker adapter
The mapping between canonical **States** and a Tracker's native labels, statuses and hierarchy.

## Story
One idea, feature request or bug. May span Repos. Jira: L0 story. GitLab/GitHub: parent issue.

## Task
A sub-task of a Story. Touches **exactly one Repo** and produces **exactly one PR**. A Task needing two Repos is split.

## State
A named, unambiguous stage of a Story or Task in the workflow graph (e.g. ready-for-agent, ready-for-human-review). Replaces the overloaded word "done".

## Gate
A State where the workflow waits for a human (grilling, triage, human review, merge).

## Grilling session
A live, human-in-the-loop conversation held in the editor that turns a Story into specs and Tasks.

## Grilling record
A comment posted on the Story when a Grilling session ends: decisions made, questions asked, links to research and prototypes. Historical only; downstream agents work from specs and Tasks, not from the record.
