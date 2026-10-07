# Our own Central skills

This folder holds the workflow's own skills, one folder per skill (`<name>/SKILL.md`), next to the
upstream copy in `../upstream/`.

- **Ours win on name.** When a skill here has the same name as an upstream skill, this one is staged
  and upstream's is not.
- **Never touched by the sync.** `weave-skills sync <commit>` replaces only `../upstream/`; anything
  here survives every upstream update.
- Put a skill here when the workflow needs its own behaviour (for example a multi-Repo
  `implement-spec`), instead of editing the upstream copy, which is kept unchanged.

The Central skills location is set by `central_skills.location` in `weave.yaml`.
