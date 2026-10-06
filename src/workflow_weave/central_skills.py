"""Central skills: where they live and the upstream sync (Seam B).

Layout under the configured Central skills location::

    <location>/
      upstream/          copy of mattpocock/skills' `skills/` tree, unchanged
        LICENSE          upstream's licence, travels with the copy
        UPSTREAM.json    {"repository": ..., "commit": ...}
      ours/              the workflow's own skills; win on the same name

The sync replaces `upstream/` with one upstream commit's skills and never
touches anything else, so its result is an ordinary change to review in a PR.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path

import yaml

DEFAULT_CONFIG = "weave.yaml"
CONFIG_ENV = "WEAVE_CONFIG"
DEFAULT_UPSTREAM = "https://github.com/mattpocock/skills.git"
UPSTREAM_DIR = "upstream"
OURS_DIR = "ours"
RECORD_FILE = "UPSTREAM.json"
# What is taken from an upstream commit: its skills tree and its licence.
UPSTREAM_SKILLS_PATH = "skills"
UPSTREAM_LICENCE_FILES = ("LICENSE", "LICENSE.md", "LICENSE.txt", "LICENCE")
# Upstream category folders whose skills are never offered to a run.
UNSTABLE_CATEGORIES = frozenset({"deprecated", "in-progress"})
SKILL_FILE = "SKILL.md"


class CentralSkillsError(Exception):
    """The Central skills location or an upstream commit could not be used."""


@dataclass(frozen=True)
class CentralSkills:
    location: Path

    @property
    def upstream(self) -> Path:
        return self.location / UPSTREAM_DIR

    @property
    def ours(self) -> Path:
        return self.location / OURS_DIR

    def upstream_record(self) -> dict | None:
        record = self.upstream / RECORD_FILE
        return json.loads(record.read_text()) if record.is_file() else None

    def skills(self) -> dict[str, "Skill"]:
        """The Central skills by name: upstream categories flattened, unstable ones left out,
        and on the same name our own skill instead of upstream's."""
        resolved: dict[str, Skill] = {}
        if self.upstream.is_dir():
            for category in sorted(p for p in self.upstream.iterdir() if p.is_dir()):
                if category.name in UNSTABLE_CATEGORIES:
                    continue
                for folder in sorted(p for p in category.iterdir() if (p / SKILL_FILE).is_file()):
                    resolved[folder.name] = Skill(folder.name, "upstream", folder)
        if self.ours.is_dir():
            for folder in sorted(p for p in self.ours.iterdir() if (p / SKILL_FILE).is_file()):
                resolved[folder.name] = Skill(folder.name, "ours", folder)
        return resolved


@dataclass(frozen=True)
class Skill:
    name: str
    source: str  # "ours" | "upstream"
    path: Path


def load(config_path: Path) -> CentralSkills:
    """Read the Central skills location from configuration.

    `central_skills.location` is resolved relative to the config file's folder.
    """
    if not config_path.is_file():
        raise CentralSkillsError(f"configuration file not found: {config_path}")
    config = yaml.safe_load(config_path.read_text()) or {}
    location = (config.get("central_skills") or {}).get("location")
    if not location:
        raise CentralSkillsError(f"{config_path}: `central_skills.location` is not set")
    return CentralSkills((config_path.parent / location).resolve())


def _git(*args: str, cwd: Path) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        raise CentralSkillsError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def _fetch_commit(repository: str, commit: str, into: Path) -> str:
    """Fetch one upstream commit (shallow) and extract its skills and licence into `into`."""
    work = into / "repo"
    work.mkdir()
    _git("init", "-q", cwd=work)
    _git("fetch", "-q", "--depth", "1", repository, commit, cwd=work)
    sha = _git("rev-parse", "FETCH_HEAD^{commit}", cwd=work)
    names = _git("ls-tree", "--name-only", sha, cwd=work).splitlines()
    if UPSTREAM_SKILLS_PATH not in names:
        raise CentralSkillsError(f"upstream commit {sha} has no `{UPSTREAM_SKILLS_PATH}/` folder")
    paths = [UPSTREAM_SKILLS_PATH] + [n for n in names if n in UPSTREAM_LICENCE_FILES]
    archive = into / "upstream.tar"
    _git("archive", "--format=tar", "-o", str(archive), sha, *paths, cwd=work)
    extracted = into / "tree"
    with tarfile.open(archive) as tar:
        tar.extractall(extracted, filter="data")
    return sha


def sync(central: CentralSkills, commit: str, repository: str = DEFAULT_UPSTREAM,
         source: str | None = None) -> str:
    """Replace the upstream copy with `commit`'s skills and record it. Returns the full sha.

    `repository` is what gets recorded; `source` (default: `repository`) is where the
    commit is fetched from, e.g. a local clone of it.
    """
    with tempfile.TemporaryDirectory(prefix="weave-skills-sync-") as tmp:
        tmp_path = Path(tmp)
        sha = _fetch_commit(source or repository, commit, tmp_path)
        extracted = tmp_path / "tree"
        staged = tmp_path / "copy"
        shutil.copytree(extracted / UPSTREAM_SKILLS_PATH, staged)
        for licence in UPSTREAM_LICENCE_FILES:
            if (extracted / licence).is_file():
                shutil.copy2(extracted / licence, staged / licence)
        record = {"repository": repository, "commit": sha}
        (staged / RECORD_FILE).write_text(json.dumps(record, indent=2) + "\n")

        central.location.mkdir(parents=True, exist_ok=True)
        if central.upstream.exists():
            shutil.rmtree(central.upstream)
        shutil.copytree(staged, central.upstream)
    return sha


def main(argv: list[str] | None = None) -> int:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", type=Path,
                        default=Path(os.environ.get(CONFIG_ENV) or DEFAULT_CONFIG),
                        help=f"weave configuration file (default: ${CONFIG_ENV}, else ./{DEFAULT_CONFIG})")
    parser = argparse.ArgumentParser(prog="weave-skills", description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    sync_cmd = commands.add_parser("sync", parents=[common],
                                   help="replace the upstream copy with one upstream commit")
    sync_cmd.add_argument("commit", help="upstream commit (sha, branch or tag) to sync to")
    sync_cmd.add_argument("--repository", default=None,
                          help="upstream repository to record (default: --from, else the recorded "
                               f"one, else {DEFAULT_UPSTREAM})")
    sync_cmd.add_argument("--from", dest="source", default=None,
                          help="where to fetch the commit from, e.g. a local clone "
                               "(default: the repository)")
    list_cmd = commands.add_parser("list", parents=[common],
                                   help="list the Central skills a run may stage (ours win on name)")
    list_cmd.add_argument("--json", action="store_true", help="print JSON")
    args = parser.parse_args(argv)

    if args.command == "list":
        try:
            skills = load(args.config).skills()
        except CentralSkillsError as error:
            print(f"weave-skills: {error}", file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps({n: {"source": s.source, "path": str(s.path)} for n, s in skills.items()},
                             indent=2))
        else:
            for name, skill in sorted(skills.items()):
                print(f"{name}\t{skill.source}")
        return 0

    try:
        central = load(args.config)
        recorded = central.upstream_record() or {}
        repository = (args.repository or args.source or recorded.get("repository")
                      or DEFAULT_UPSTREAM)
        sha = sync(central, args.commit, repository, source=args.source)
    except CentralSkillsError as error:
        print(f"weave-skills: {error}", file=sys.stderr)
        return 1
    print(f"upstream copy at {central.upstream} synced to {repository} {sha}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
