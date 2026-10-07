# Product Coding standards

One folder per Product, holding that Product's one Coding standards file:

    products/<Product>/coding-standards.md

A Repo whose `coding_standards` is `central` or `central+repo` (Product config) is held to it; the
run copies it into the sandbox at user level and the always-on file (`~/.claude/CLAUDE.md`) points
the agent at it. If it is missing, such a run is refused as `needs-setup`. Keep it to rules an agent
reviewer can judge from a diff. The sync (`weave-skills sync`) never touches this folder.
