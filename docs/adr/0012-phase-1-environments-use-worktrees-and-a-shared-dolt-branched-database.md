# Phase 1 Environments use a worktree-started backend and a shared Dolt-branched database, not a throwaway isolated container

ADR 0004 brings every Environment up as a throwaway, isolated set of containers, using nested Docker inside the run's sandbox via sysbox. Sysbox would not install on the dev host (Ubuntu 26), which blocks that Environment entirely and, with it, everything after Spec 1 that needs one. Rather than block the Dispatcher (Spec 3) and the specs after it on a host dependency, Phase 1 builds the Environment a different way: a host-level `git worktree`, keyed by (Story, Repo) and checked out at the Story's Integration branch, builds and starts a Repo's backend on a free host port; a Repo's database is a branch of one long-lived Dolt or Doltgres server shared by the whole Product, cut per Story from a shared seeded base the first time the Story's Environment comes up, and reused across that Story's rounds. There is no container, no nested Docker, and no per-run teardown. The agent's own sandbox (Spec 1) is unchanged; only the software it calls over the network is brought up this way.

This is explicitly temporary. The Run recipe contract, the Environment MCP server / External MCP server split, the `weave-env` command, and the outcome set (`infra-failure`, `needs-setup`, `environment-broken`) ADR 0004 and its Spec 2 grilling defined are all unchanged — a Repo's recipe does not know or care which Environment implementation reads it. The original design is preserved in full as **Spec 2c** and resumes once a working sysbox host (or an equivalent nested-container runtime) exists, replacing this Environment under Spec 3/4 without those specs changing.

## Considered Options

- **Block on sysbox** — keeps one Environment design, but stalls the whole MVP (Spec 3 onward) on a host dependency with no workaround in sight.
- **Mount the host's Docker socket** — the Environment could still use containers without sysbox, but ADR 0004 already rejected this: any agent run gets root on the Sandbox host.
- **One throwaway database per run, started as a plain host process (no branching)** — closer to ADR 0004's isolation, but a throwaway Postgres per run still needs something to make and tear it down fast; Dolt/Doltgres branching does that more cheaply than a fresh `initdb` and reseed every run, and gives Stories a form of per-run isolation without one.
- **One shared database with no per-Story isolation at all** — simplest, but concurrent Stories would corrupt each other's data and a Story's own rounds couldn't be told apart from another's; ruled out as unsafe even for Phase 1.
- **Drop the agent's own Docker sandbox too, run everything on the host** — simpler still, but the agent itself would then have direct host filesystem/process access with no containment; rejected, since Spec 1's existing per-run Docker sandbox already works without sysbox and gives up nothing by staying.

## Consequences

- Two Stories' backend processes and worktrees coexist on the same host; two Stories' Postgres-shaped data are branches of the same Dolt/Doltgres server. Neither is process-isolated the way ADR 0004 describes; Spec 2c is where that isolation returns.
- Neo4j has no Dolt-style branching, so Neo4j-backed features are **not provable here** until Spec 2c. The chat app pilot's Postgres-backed features are in scope; its Neo4j-backed ones are not.
- A Story's Dolt branch is reused across its implement/fix/review/Proofs rounds and reset only when its review → fix loop restarts (the same triggers ADR 0005 already defines for the loop itself), then dropped once the Story merges. This makes Phase 1 Proofs possible — they run against a real backend and real (branched) data — at the cost of that data surviving across rounds the way a fresh container would not.
- A Repo's Run recipe gains no new required fields for Phase 1; the fields a container-based Environment would read (image names, published ports) are simply unused by this implementation.
- Migrating a Repo from this Environment to Spec 2c's later is swapping which code reads the same recipe, not a new recipe format or a Spec 3/4 change.
- Spec 2b (Run images, ADR 0011) has nothing to prebuild under this ADR — there is no container to rebuild from — so it now depends on Spec 2c, not on Spec 2 as rewritten.
