# Agent Subscriptions are owned by the Sandbox host and leased per run

#18 gave each Product its own Dispatcher container holding its own credentials, so Products never share credentials or state. Agent accounts don't fit that: one Cursor or Claude Code account may serve several Products, two accounts of the same agent may each be reserved for different Products, and an account's usage limit applies to the account, whichever Products draw on it. We therefore make each **Subscription** a named credential owned by the Sandbox host, in one Subscription store that holds the credentials, which Products each is associated with, a cap on runs at once per Subscription, and its current leases. The Product ↔ Subscription association is the only authority on which Product may use which Subscription. To start a run a Dispatcher leases a Subscription for its own Product; the store refuses one not associated with that Product or at its cap, and the run waits in the queue instead. The credential goes from the store straight into the sandbox environment; a Dispatcher never holds it. Tracker credentials, signing keys and Run bookkeeping stay per Product as #18 decided.

## Considered Options

- **Copy each Subscription into every Product's config, cap per Product only** — keeps #18 intact, but two Products sharing an account can together exceed its limit, and nothing stops a Product's config naming the wrong account.

## Consequences

- A Product lists Subscriptions per agent in fallback order. On `quota-exhausted` the run is retried at once on the next Subscription in its Product's list, same Agent profile; only when the list is exhausted does the Task stall until the window resets. This amends ADR 0002's "no automatic fallback", which still holds for falling back to a different Agent profile. A rejected credential stays `needs-setup` and never falls back.
- The sandbox gets only its leased Subscription's environment (for Claude Code `CLAUDE_CODE_OAUTH_TOKEN`, with `ANTHROPIC_API_KEY` and `ANTHROPIC_BASE_URL` unset, per #13).
- Spec 1 keeps Subscriptions in a config file on the Sandbox host; a host-level page to create Subscriptions and associate them with Products belongs to the Workflow dashboard spec (#24), outside any one Product's dashboard.
- A lease must be returned when its run ends, including on restart (runs interrupted by a restart release their leases when closed as `infra-failure`).
