# Agent runs happen inside one long-lived Product agent server, not a fresh sandbox per run

ADR 0002 starts a fresh Docker sandbox for every agent run, and ADR 0012 runs a Story's backend as a plain process on the Sandbox host. That gives a human no live view into what agents are doing (they want to watch, message, pause and approve runs remotely, which is why OpenHands was chosen), and it runs agent-written code unboxed on the host. We therefore run every agent run of every Story of a Product inside **one long-lived Product agent server**: one OpenHands agent server in one container per Product, set up once by a human as part of setting up the Product and registered in OpenHands' Agent Canvas as a "backend". A run never creates one. Each Story has a **Story workspace** inside it (every Repo the Story needs, touched ones at their Integration branch); the agent works there and the Story's backends run there too, each on a port the wrapper hands out. The container wall protects the Sandbox host; nothing walls Stories of the same Product off from each other, which we accept for Phase 1. Data stays on a long-lived Dolt/Doltgres server per Product (ADR 0012), in its own container beside the agent server, one branch per Story.

## Considered Options

- **A fresh sandbox per run (ADR 0002, Spec 1 as built)** — clean and isolated, but nothing long-lived for Agent Canvas to attach to, and a backend inside it restarts every run.
- **One container per Story** — keeps Stories apart, but every Story needs its own Canvas registration and container lifecycle.
- **Backends as plain host processes (ADR 0012 as first written)** — no container work at all, but agent-written code runs with the host user's full access.
- **Per-Story containers started from inside a Product container** — needs nested Docker, which is what sysbox was for and what would not install (ADR 0012).

## Consequences

- Setup is a human step (`weave product setup <product>` or similar): it builds the Product's image from one Dockerfile per Product kept with its config in this repo (every Repo's toolchain in it), starts the agent server and the Dolt server, and gives the URL and API key to paste into Agent Canvas. The Dispatcher never starts or restarts it.
- A run against a Product with no agent server set up ends `needs-setup`; one whose agent server is set up but not answering ends `infra-failure` (retried once).
- Restarting or upgrading the agent server stops every running Story of the Product, so the setup command refuses while runs are in flight unless forced; a forced restart ends those runs `infra-failure`.
- Stories share one filesystem, process list and `localhost`. Each (Story, Repo service) gets its own port and each Run recipe reads its port from the environment; each run gets its own home folder, since agents keep logins and our staged skills (ADR 0002) there.
- Unproven, and the first Task of the spec: that a conversation our wrapper starts through the SDK shows in Agent Canvas and can be watched, messaged and paused there; that one agent server runs many conversations at once; and that a home folder can be set per conversation, ACP agents included.
- Spec 1's sandbox code (one container per run, Repos cloned in from a bundle) is replaced, not extended.
- Amends ADR 0002 (fresh sandbox per run) and ADR 0012 (backend as a host process; host-level worktree per Story and Repo, now the Story workspace).
