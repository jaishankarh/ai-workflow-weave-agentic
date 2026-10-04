# State is a label on every Tracker; a Tracker's own status only mirrors it

Every Tracker we support has labels, but their native statuses differ wildly: GitLab Free has only open/closed, Jira workflows restrict which status may follow which, and GitHub has no status at all outside Projects. We therefore keep each canonical State as exactly one label on the Story or Task (`state::x` scoped labels on GitLab, which are exclusive; `state:x` on Jira and GitHub, where the adapter enforces exactly one). Where a Tracker has a status, the adapter moves it to match after writing the label, so humans browsing Jira see sensible statuses, but it is never read to decide a State. Zero or several State labels is an Invalid State, flagged and never guessed.

The contract is split into a **Tracker adapter** (Stories, Tasks, State, links, comments, attachments, events) and a **Code host adapter** (PRs), paired freely per Repo, because Products already pair Jira with Bitbucket and GitLab with GitLab and the pairing may cross-match.

## Considered Options

- **Jira workflow status as the State** — one native truth, but every Jira project's workflow would have to allow every transition the State model uses, and Jira would behave differently from GitLab and GitHub.
- **GitHub Projects v2 Status field** — single-valued, but GraphQL-only, org-level webhooks, and it lives outside the issue.
- **Status and label both authoritative** — two sources of truth to reconcile, the problem ADR 0001 exists to avoid.

## Consequences

- Moving the Jira status is best-effort. Per Jira project it is *direct* (one transition) or *walk* (one intermediate status at a time); direct is tried first, and a failed status move never changes the State.
- A human who changes only the Jira status has not changed the State; humans change State by changing the label.
- GitLab Free has no blocking links, so a Deploy blocker there is a `deploy-blocker` label on the Follow-up Story plus a relates-to link; Premium, Jira and GitHub use native blocking links behind the same adapter call.
- The Dispatcher marks a Task `merged` from the merge it observes on the Code host, not from cross-project auto-close.
