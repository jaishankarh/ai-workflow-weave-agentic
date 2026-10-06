# Agent runs only push commits; they never touch the Tracker or the Code host

The skills we stage expect to drive the work themselves: upstream `implement-spec` reads its spec and tickets from the issue tracker, opens a draft PR, marks it ready and closes tickets. That collides with ADR 0001 and 0003 (only the Dispatcher writes State), with one PR per Repo per Story opened from the Integration branch and tracked through the Code host adapter, and with the #13 finding that any credential in the sandbox is readable by the agent. We therefore make an agent run's only output commits pushed to its Integration branches. The wrapper stages the spec and its Tasks into the run as read-only local files and points the skills at a local-files tracker, so upstream skills run unforked and "closing" a ticket only marks the local file, which the Dispatcher reads back. The sandbox gets a git credential scoped to pushing the run's Integration branches and nothing else: no Tracker token, no PR rights. Opening PRs and moving States stay with the Dispatcher.

## Considered Options

- **Agents use the real Tracker and PRs; the Dispatcher reconciles afterwards** — keeps upstream behaviour, but State gets written without a signed Workflow comment and every sandbox holds a token that can relabel any Story in the Product.
- **Fork the skills to strip their Tracker and PR steps** — same outcome as ours, but every upstream sync has to re-apply the fork.

## Consequences

- Spec 1's by-hand demo succeeds on a pushed Integration branch with typecheck and build green; no PR is involved.
- A run that tries a Tracker or Code host write fails for lack of credentials rather than being trusted; its outcome is classified like any other failure.
- Decided "for now": giving runs scoped Tracker access later means revisiting this ADR, not just a config change.
