# Proofs run once per Story, in one throwaway Environment shared by every agent node

A Story's Tasks often depend on each other across Repos (a frontend Task calls an endpoint a backend Task adds), so proving each Task on its own would prove it against unfinished or missing sibling work. We therefore capture Proofs once per Story: each Task waits after agent review until all of the Story's Tasks are there, then one **Environment** is brought up with every touched Repo at its Task branch and every other dependency at its **Base branch**, and the proofs agent captures every Task's Proofs in it one after another. The same Environment mechanism, driven by each Repo's committed **Run recipe**, is used by the implement and review agents too, so there is one way to run the software, not three.

## Considered Options

- **Proofs per Task, fresh environment each** — independent and simple, but a cross-Repo change is proven against the sibling Repo's Base branch, which doesn't have the change yet.
- **One long-lived environment per Story, reused by each Task as it arrives** — proves a Task against half-finished siblings, and leftover data makes Proofs pass or fail for the wrong reason.
- **Mounting the host Docker socket into the sandbox** — the simplest way to start a compose stack, but any agent run gets root on the host.
- **Run recipes kept in the central skills repo** — Repos stay untouched, but the recipe drifts from the code it describes.

## Consequences

- The State model gains a Task waiting State after agent review (`awaiting-story-proofs`), and `proofs` becomes a Story-level step that fans in from its Tasks. A failing Proof sends only its own Task back to `agent-fixing`; the next round re-proves the whole Story in a fresh Environment.
- One slow Task delays Proofs for the whole Story. A single-Task Story is unaffected.
- Environments run nested Docker inside the sandbox (sysbox), never the host socket; this, and an Android emulator with KVM inside it, are unproven and must be shown by a spike before build. Until then mobile UI is "not provable here"; iOS is always "not provable here" (needs macOS).
- Secrets come from a per-Product set of sandbox/test credentials (e.g. Korona test businesses), never staging or production; seed data is synthetic and committed in the Repo.
  *Amended by [ADR 0011](0011-environments-start-from-run-images-and-tracked-seed-scripts.md):* "seeded fresh" means no run inherits another run's state; Environments start from prebuilt Run images and run only pending Seed scripts. The secrets set is named **Test secrets** (decided in #21's grilling).
- A Repo without a Run recipe cannot be run by agents; its spec items are "not provable here".
