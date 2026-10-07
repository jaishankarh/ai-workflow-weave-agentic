# Environments start from prebuilt Run images and tracked Seed scripts, not from scratch

ADR 0004 brings every Environment up from Run recipes and "seeds it fresh". Taken literally, every agent run rebuilds every image and reloads every database from empty, although most Stories change only source code. To cut bring-up time we read "fresh" as **no run inherits another run's state**, not "built from nothing". Each Repo may have **Run images**, built by a human from its Base branch: app images with dependencies installed, and database images with the base test data baked into the image itself (not a volume), so every container gets its own copy-on-write view of that data. At bring-up an app service reuses its Run image with the working copy's source put over it, unless the files the image was built from (its declared image inputs, never source code) differ from the hash recorded as a label on the image; then that one service is built for that run only. Databases start from their Run image, then run pending migrations and only the **Seed scripts** not yet recorded in them, tracked in the database like migrations. Only a human's rebuild ever replaces a Run image; a Story's branch never does.

## Considered Options

- **Build every image and seed from empty on every run** — simplest and what ADR 0004 implied, but pays the full install and load cost on every implement, review, fix and proofs run.
- **Reuse images and let the agent install whatever changed** — fast, but each agent does it differently and Checks run on an image that no longer matches what ships.
- **Base data in a snapshot file restored into a fresh volume** — uses the database images unchanged, but restore time grows with the data on every run; for small test data, baking it into the image starts faster.
- **Frontends always served from their production build** — Proofs see exactly what ships, but every run rebuilds them; a Repo may still choose this per service, and Checks already run the build.

## Consequences

- Seed scripts are append-only, like migrations: one that has run is never edited; a change is a new script.
- Run images age: the longer since a rebuild, the more pending migrations and Seed scripts each run replays. Rebuilding is a human's call.
- A service the recipe marks as rebuilt every run (e.g. a frontend from its production build) uses its Run image only as a build cache.
- Each sandbox's nested Docker starts empty, so Run images and public images are served from a registry with a pull-through cache on the Sandbox host.
- A Repo without Run images still works; it takes the slow path every time.
