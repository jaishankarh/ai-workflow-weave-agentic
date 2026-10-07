"""Which skills a run stages at the agent's user level, and why (ADR 0002 as amended in #20).

The staged set is the requested skill plus every skill it calls, collected
transitively from the Central skills (upstream categories flattened, unstable
ones left out, ours beating upstream on name; see `central_skills`).

**How "calls" are detected.** A skill calls another when a line of its
`SKILL.md` mentions the Skill tool and names the other skill in backticks or
double quotes, as upstream skills write it:

    calls the Skill tool with `tdd` to build the ticket;
    Call the Skill tool twice, for "grilling" and "domain-modeling".

Only names of existing Central skills count, so prose in quotes is ignored.

**A Repo's own skills** are the folders of `.claude/skills/<name>/SKILL.md` in
the Repo as committed on its Base branch. Two Repos' own skills are identical
when their skill folders are the same git tree.

**Skill overrides.** A central skill named in a Repo's `skill_overrides` is not
staged, so the Repo's own skill is the one the agent loads. In a run over
several Repos this happens only when every Repo has the override and identical
own skills; otherwise the central skill is staged and the disagreement
reported. Every clash between a central skill and a Repo's own skill is
reported too.

**Hiding a shadowed Repo skill.** Claude Code loads a user-level skill over a
project skill of the same name, but in a sandbox its session starts in the
workspace folder, so a Repo's own skills are *nested* skills to it
(`<repo>:<name>`). Claude Code keeps a nested skill available beside the
user-level one and tells the agent to prefer it for files under that Repo
(checked with Claude Code 2.1.287). So for every clash the plan also lists the
nested name to turn off in the agent's user-level settings (`skillOverrides`),
which leaves the working copy untouched.
"""

from __future__ import annotations

import hashlib
import io
import re
import subprocess
import tarfile
from dataclasses import dataclass, field
from pathlib import Path

from workflow_weave.central_skills import SKILL_FILE, CentralSkills, Skill

REPO_SKILLS_DIR = ".claude/skills"
_SKILL_TOOL_LINE = re.compile(r"Skill tool", re.IGNORECASE)
_NAMED = re.compile(r"`/?([A-Za-z0-9_-]+)`|\"/?([A-Za-z0-9_-]+)\"")


class StagingError(RuntimeError):
    """The Central skills or a Repo's own skills could not be read (an infrastructure failure)."""


def called_skills(skill: Skill, known: set[str]) -> set[str]:
    """Central skills this skill calls, by the rule in the module docstring."""
    names: set[str] = set()
    for line in (skill.path / SKILL_FILE).read_text().splitlines():
        if _SKILL_TOOL_LINE.search(line):
            names |= {a or b for a, b in _NAMED.findall(line)}
    return (names & known) - {skill.name}


@dataclass(frozen=True)
class RepoSkills:
    """One Repo's own skills on its Base branch: name -> git tree id of the skill folder."""

    repo: str
    overrides: frozenset[str]
    own: dict[str, str]


def read_repo_skills(repo: str, source: str, base_branch: str, overrides: frozenset[str]) -> RepoSkills:
    r = subprocess.run(
        ["git", "-C", source, "ls-tree", f"{base_branch}:{REPO_SKILLS_DIR}"],
        capture_output=True, text=True,
    )
    own: dict[str, str] = {}
    if r.returncode == 0:
        for line in r.stdout.splitlines():
            meta, name = line.split("\t", 1)
            _mode, kind, tree = meta.split()
            if kind == "tree":
                own[name] = tree
    else:
        # No `.claude/skills` on the Base branch is normal; an unreadable Repo is not.
        ok = subprocess.run(["git", "-C", source, "rev-parse", "--verify", "-q", f"{base_branch}^{{commit}}"],
                            capture_output=True)
        if ok.returncode != 0:
            raise StagingError(f"cannot read Repo {repo}'s Base branch {base_branch!r} at {source}")
    return RepoSkills(repo, overrides, own)


@dataclass
class StagingPlan:
    staged: dict[str, Skill] = field(default_factory=dict)
    overridden: list[str] = field(default_factory=list)
    # Repos' own skills shadowed by a staged central skill, as the agent names them
    # (`<repo>:<name>`); turned off at user level so the agent cannot load them.
    hidden_repo_skills: list[str] = field(default_factory=list)
    # Human-readable lines for the run's log: clashes and override disagreements.
    notes: list[str] = field(default_factory=list)

    def tarball(self) -> bytes:
        """The staged skills as `<name>/...` folders, ready to unpack into the user-level skills folder."""
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            for name, skill in sorted(self.staged.items()):
                tar.add(skill.path, arcname=name)
        return buf.getvalue()


def plan(central: CentralSkills, skill: str, repos: list[RepoSkills]) -> StagingPlan:
    if not central.location.is_dir():
        raise StagingError(f"Central skills location is unreachable: {central.location}")
    available = central.skills()
    known = set(available)

    result = StagingPlan()
    wanted: list[str] = [skill]
    seen: set[str] = set()
    while wanted:
        name = wanted.pop()
        if name in seen:
            continue
        seen.add(name)
        if name not in available:
            if name == skill and _override_applies(name, repos):
                result.overridden.append(name)
                continue
            raise StagingError(f"skill {name!r} is not among the Central skills at {central.location}")
        wanted.extend(sorted(called_skills(available[name], known)))
        if _override_applies(name, repos):
            result.overridden.append(name)
        else:
            result.staged[name] = available[name]

    for name in sorted(seen):
        result.notes.extend(_report(name, repos, name in result.overridden))
        if name in result.staged:
            result.hidden_repo_skills.extend(f"{r.repo}:{name}" for r in repos if name in r.own)
    result.overridden.sort()
    return result


def _override_applies(name: str, repos: list[RepoSkills]) -> bool:
    if not repos or not all(name in r.overrides for r in repos):
        return False
    trees = {r.own.get(name) for r in repos}
    return None not in trees and len(trees) == 1


def _report(name: str, repos: list[RepoSkills], overridden: bool) -> list[str]:
    lines = []
    wanting = [r.repo for r in repos if name in r.overrides]
    if overridden:
        lines.append(f"skill override: `{name}` uses the Repo's own skill ({', '.join(wanting)}); central not staged")
    elif wanting:
        why = []
        missing = [r.repo for r in repos if name not in r.overrides]
        if missing:
            why.append(f"no override in {', '.join(missing)}")
        without = [r.repo for r in repos if name not in r.own]
        if without:
            why.append(f"no own skill in {', '.join(without)}")
        if len({r.own.get(name) for r in repos if name in r.own}) > 1:
            why.append("the Repos' own skills differ")
        lines.append(
            f"skill override disagreement: `{name}` override in {', '.join(wanting)} not applied "
            f"({'; '.join(why)}); central skill staged"
        )
    for r in repos:
        if name in r.own and not overridden:
            lines.append(f"skill clash: central `{name}` shadows Repo {r.repo}'s own `{name}`")
    return lines


def central_skills_version(central: CentralSkills) -> dict:
    """The Central skills version and upstream commit a run used.

    The version is the git commit holding the location (suffixed `-dirty` when it has
    uncommitted changes there), or a content digest when it is not in git.
    """
    loc = str(central.location)
    head = subprocess.run(["git", "-C", loc, "log", "-1", "--format=%H", "--", "."], capture_output=True, text=True)
    if head.returncode == 0 and head.stdout.strip():
        dirty = subprocess.run(["git", "-C", loc, "status", "--porcelain", "--", "."], capture_output=True, text=True)
        version = head.stdout.strip() + ("-dirty" if dirty.stdout.strip() else "")
    else:
        h = hashlib.sha256()
        for f in sorted(p for p in central.location.rglob("*") if p.is_file()):
            h.update(str(f.relative_to(central.location)).encode() + b"\0" + f.read_bytes() + b"\0")
        version = "sha256:" + h.hexdigest()
    upstream = central.upstream_record() or {}
    return {"version": version, "upstream_commit": upstream.get("commit"), "upstream_repository": upstream.get("repository")}
