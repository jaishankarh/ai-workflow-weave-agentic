"""Each Repo's Coding standards, and the always-on file that points the agent at them (#41).

**Where the standards come from.** A Repo's `coding_standards` mode (Product config)
selects them:

- `central`: the Product's one coding-standards file, kept in the Central skills at
  `<Central skills location>/products/<Product>/coding-standards.md`;
- `central+repo`: that file and the Repo's own `rules_files`;
- `repo`: the Repo's own `rules_files` alone.

A rules file is a path in the Repo: a file, or a folder whose files all count. It is
always read as it stands on the Repo's **Base branch** (`git show <base>:<path>` on
the Sandbox host), never from the working branch or the agent's working copy, so a
run cannot relax a rule in the change that breaks it. A selected file that does not
exist refuses the run as `needs-setup`.

**What the agent sees.** The resolved files are copied into the sandbox at user
level, under `~/.claude/weave/coding-standards/` (`product/coding-standards.md`, and
`repos/<Repo>/<path>` for a Repo's rules files), and an always-on file is staged as the
user-level `~/.claude/CLAUDE.md`. It points at each Repo's own `CONTEXT.md` in its
working copy and at those copies. Nothing is written into a working copy, so the Repo's
own root `CLAUDE.md` stays exactly as committed and still loads as the project memory.
"""

from __future__ import annotations

import io
import subprocess
import tarfile
from dataclasses import dataclass
from pathlib import Path

from .config import ProductConfig, RepoConfig

# Under the Central skills location; one file per Product.
PRODUCT_STANDARDS = "products/{product}/coding-standards.md"
# Relative to the agent's user-level folder (`~/.claude` for Claude Code).
ALWAYS_ON_FILE = "CLAUDE.md"
STAGED_STANDARDS_DIR = "weave/coding-standards"


@dataclass(frozen=True)
class StandardsFile:
    """One Coding standards file as staged: its path under the user-level folder, and its text."""

    staged_as: str
    content: bytes
    origin: str  # for people: where it was read from


@dataclass(frozen=True)
class RepoStandards:
    repo: str
    mode: str
    files: list[StandardsFile]


@dataclass(frozen=True)
class ResolvedStandards:
    product_file: StandardsFile | None
    repos: list[RepoStandards]

    def always_on(self, user_dir: str, workdir: str) -> str:
        """The always-on file's text. `user_dir` is the user-level folder's absolute path."""
        lines = [
            "# Weave run: vocabulary and Coding standards",
            "",
            "This file is staged by the workflow for this run. Before you start, read each Repo's",
            "`CONTEXT.md` (its vocabulary: use its words in code, tests and commits) and the Coding",
            "standards listed for it. Your work is reviewed against those standards; they were read",
            "from the Repo's Base branch and are not changed by edits in the working copy.",
            "",
        ]
        for repo in self.repos:
            lines += [f"## Repo `{repo.repo}`", "", f"- Vocabulary: `{workdir}/{repo.repo}/CONTEXT.md`"]
            lines.append(f"- Coding standards ({repo.mode}):")
            lines += [f"  - `{user_dir}/{f.staged_as}` ({f.origin})" for f in repo.files]
            lines.append("")
        return "\n".join(lines)

    def tarball(self, user_dir: str, workdir: str) -> bytes:
        """The always-on file and the standards copies, to unpack into the user-level folder."""
        files = {ALWAYS_ON_FILE: self.always_on(user_dir, workdir).encode()}
        for repo in self.repos:
            files |= {f.staged_as: f.content for f in repo.files}
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            for name, data in sorted(files.items()):
                info = tarfile.TarInfo(name)
                info.size = len(data)
                info.mode = 0o644
                tar.addfile(info, io.BytesIO(data))
        return buf.getvalue()


class MissingStandards(Exception):
    """Selected Coding standards files that do not exist (the run needs setup)."""

    def __init__(self, missing: list[str]) -> None:
        super().__init__("; ".join(missing))
        self.missing = missing


def product_standards_path(central_location: Path, product: str) -> Path:
    return central_location / PRODUCT_STANDARDS.format(product=product)


def missing(product: ProductConfig, targets: list[tuple[str, str]], central_location: Path | None) -> list[str]:
    """Each selected Coding standards file that does not exist, described for a human.

    `targets` are (Repo, Base branch) pairs. A Repo or Base branch that cannot be
    read at all, or a Central skills location that is unreachable, is left for the
    run to report as an infra-failure, not counted here.
    """
    problems: list[str] = []
    repos = [(product.repos[name], base) for name, base in targets]
    if any(r.uses_product_standards for r, _ in repos) and central_location is not None and central_location.is_dir():
        path = product_standards_path(central_location, product.name)
        if not path.is_file():
            users = ", ".join(repr(r.name) for r, _ in repos if r.uses_product_standards)
            problems.append(
                f"the Product's Coding standards file {PRODUCT_STANDARDS.format(product=product.name)} is missing "
                f"from the Central skills at {central_location} (selected by Repo {users})"
            )
    for repo, base in repos:
        if not repo.uses_repo_rules or not _readable(repo.source, base):
            continue
        absent = [p for p in repo.rules_files if not _rules_paths(repo.source, base, p)]
        if absent:
            problems.append(
                f"Repo {repo.name!r} has no rules file {', '.join(absent)} on its Base branch ({base}), "
                f"though its Coding standards ({repo.coding_standards}) name it"
            )
    return problems


def resolve(product: ProductConfig, targets: list[tuple[str, str]], central_location: Path) -> ResolvedStandards:
    """Read every selected Coding standards file; raises MissingStandards if any is absent."""
    if problems := missing(product, targets, central_location):
        raise MissingStandards(problems)
    product_file = None
    repos: list[RepoStandards] = []
    for name, base in targets:
        repo = product.repos[name]
        files: list[StandardsFile] = []
        if repo.uses_product_standards:
            if product_file is None:
                path = product_standards_path(central_location, product.name)
                product_file = StandardsFile(
                    f"{STAGED_STANDARDS_DIR}/product/coding-standards.md", path.read_bytes(),
                    f"the Product's coding-standards file, {PRODUCT_STANDARDS.format(product=product.name)} "
                    "in the Central skills",
                )
            files.append(product_file)
        if repo.uses_repo_rules:
            files += _repo_rules(repo, base)
        repos.append(RepoStandards(name, repo.coding_standards, files))
    return ResolvedStandards(product_file, repos)


def _repo_rules(repo: RepoConfig, base: str) -> list[StandardsFile]:
    out: list[StandardsFile] = []
    seen: set[str] = set()
    for named in repo.rules_files:
        for path in _rules_paths(repo.source, base, named):
            if path in seen:
                continue
            seen.add(path)
            content = _git(repo.source, "show", f"{base}:{path}", text=False)
            out.append(StandardsFile(
                f"{STAGED_STANDARDS_DIR}/repos/{repo.name}/{path}", content,
                f"`{path}` in Repo {repo.name} as on its Base branch {base}",
            ))
    return out


def _rules_paths(source: str, base: str, named: str) -> list[str]:
    """The files a named rules path stands for on the Base branch (a file, or every file in a folder)."""
    r = subprocess.run(
        ["git", "-C", source, "ls-tree", "-r", "--name-only", "-z", base, "--", named],
        capture_output=True,
    )
    if r.returncode != 0:
        return []
    paths = [p.decode() for p in r.stdout.split(b"\0") if p]
    return [p for p in paths if p == named or p.startswith(named + "/")]


def _readable(source: str, base: str) -> bool:
    return subprocess.run(
        ["git", "-C", source, "rev-parse", "--verify", "--quiet", f"{base}^{{commit}}"], capture_output=True
    ).returncode == 0


def _git(source: str, *args: str, text: bool = True):
    r = subprocess.run(["git", "-C", source, *args], capture_output=True, text=text)
    if r.returncode != 0:
        err = r.stderr if text else r.stderr.decode(errors="replace")
        raise RuntimeError(f"git {' '.join(args)} in {source} failed: {err.strip()}")
    return r.stdout
