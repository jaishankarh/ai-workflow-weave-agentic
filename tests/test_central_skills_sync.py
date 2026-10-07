"""Seam B: the Central skills sync command (ticket #39).

Every test drives the `weave-skills-sync` CLI against a fixture upstream git
repo with two commits and observes only the files it leaves behind and what it
prints.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
             "GIT_COMMITTER_EMAIL": "t@t", "HOME": str(repo), "PATH": "/usr/bin:/bin"},
    ).stdout.strip()


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


@pytest.fixture
def upstream(tmp_path: Path) -> dict:
    """A fixture upstream at two commits, X and Y."""
    repo = tmp_path / "upstream"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    write(repo / "LICENSE", "MIT License\nCopyright upstream\n")
    write(repo / "README.md", "not a skill\n")
    write(repo / "skills/engineering/tdd/SKILL.md", "tdd at X\n")
    write(repo / "skills/engineering/code-review/SKILL.md", "code-review at X\n")
    write(repo / "skills/misc/old-thing/SKILL.md", "removed by Y\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "X")
    x = git(repo, "rev-parse", "HEAD")
    write(repo / "skills/engineering/tdd/SKILL.md", "tdd at Y\n")
    write(repo / "skills/engineering/tdd/tests.md", "added by Y\n")
    (repo / "skills/misc/old-thing/SKILL.md").unlink()
    write(repo / "skills/in-progress/new-idea/SKILL.md", "new at Y\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "Y")
    y = git(repo, "rev-parse", "HEAD")
    return {"path": repo, "x": x, "y": y}


@pytest.fixture
def weave(tmp_path: Path) -> Path:
    """A weave checkout whose configuration puts the Central skills at `central/`."""
    root = tmp_path / "weave"
    write(root / "weave.yaml", "central_skills:\n  location: central\n")
    write(root / "central/ours/README.md", "our skills\n")
    write(root / "central/ours/tdd/SKILL.md", "our tdd\n")
    return root


def sync(weave: Path, upstream: dict, commit: str, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "workflow_weave.central_skills", "sync",
         "--from", str(upstream["path"]), commit, *extra],
        cwd=weave,
        capture_output=True,
        text=True,
    )


def tree(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def test_syncing_from_commit_x_to_y_makes_the_copy_match_y_and_records_y(weave, upstream):
    assert sync(weave, upstream, upstream["x"]).returncode == 0
    result = sync(weave, upstream, upstream["y"])
    assert result.returncode == 0, result.stderr

    copy = weave / "central/upstream"
    assert tree(copy) == {
        "LICENSE": b"MIT License\nCopyright upstream\n",
        "UPSTREAM.json": (copy / "UPSTREAM.json").read_bytes(),
        "engineering/code-review/SKILL.md": b"code-review at X\n",
        "engineering/tdd/SKILL.md": b"tdd at Y\n",
        "engineering/tdd/tests.md": b"added by Y\n",
        "in-progress/new-idea/SKILL.md": b"new at Y\n",
    }
    assert json.loads((copy / "UPSTREAM.json").read_text())["commit"] == upstream["y"]


def test_sync_never_changes_anything_in_our_own_skills_folder(weave, upstream):
    ours = weave / "central/ours"
    before = tree(ours)
    stray = weave / "central/notes.txt"
    write(stray, "beside the copy\n")

    assert sync(weave, upstream, upstream["x"]).returncode == 0
    assert sync(weave, upstream, upstream["y"]).returncode == 0

    assert tree(ours) == before
    assert stray.read_text() == "beside the copy\n"


def test_a_failed_sync_leaves_the_existing_copy_and_our_skills_as_they_were(weave, upstream):
    assert sync(weave, upstream, upstream["x"]).returncode == 0
    before = tree(weave / "central")

    result = sync(weave, upstream, "0" * 40)

    assert result.returncode != 0
    assert tree(weave / "central") == before


def test_upstreams_licence_travels_with_the_copy(weave, upstream):
    assert sync(weave, upstream, upstream["y"]).returncode == 0

    assert (weave / "central/upstream/LICENSE").read_text() == "MIT License\nCopyright upstream\n"


def test_the_central_skills_location_is_read_from_configuration(weave, upstream):
    write(weave / "weave.yaml", "central_skills:\n  location: elsewhere/skills\n")

    assert sync(weave, upstream, upstream["y"]).returncode == 0

    record = json.loads((weave / "elsewhere/skills/upstream/UPSTREAM.json").read_text())
    assert record["commit"] == upstream["y"]
    assert not (weave / "central/upstream").exists()


def test_the_central_skills_location_is_read_from_the_config_file_given(weave, upstream, tmp_path):
    other = tmp_path / "other.yaml"
    write(other, f"central_skills:\n  location: {tmp_path / 'abs-skills'}\n")

    assert sync(weave, upstream, upstream["y"], "--config", str(other)).returncode == 0

    assert (tmp_path / "abs-skills/upstream/UPSTREAM.json").is_file()


def test_sync_refuses_to_run_when_no_central_skills_location_is_configured(weave, upstream):
    write(weave / "weave.yaml", "something_else: 1\n")

    result = sync(weave, upstream, upstream["y"])

    assert result.returncode != 0
    assert "central_skills.location" in result.stderr


def listing(weave: Path) -> dict[str, str]:
    result = subprocess.run(
        [sys.executable, "-m", "workflow_weave.central_skills", "list", "--json"],
        cwd=weave, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    return {name: entry["source"] for name, entry in json.loads(result.stdout).items()}


def test_after_a_sync_our_skill_still_wins_on_the_same_name(weave, upstream):
    assert sync(weave, upstream, upstream["y"]).returncode == 0

    assert listing(weave) == {"tdd": "ours", "code-review": "upstream"}
