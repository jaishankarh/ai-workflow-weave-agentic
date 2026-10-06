"""The Central skills this repo ships (ticket #39), found through its own configuration."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def test_the_upstream_copy_is_present_with_its_upstream_commit_recorded_next_to_it():
    result = subprocess.run(
        [sys.executable, "-m", "workflow_weave.central_skills", "list", "--json"],
        cwd=REPO, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    skills = json.loads(result.stdout)
    assert {"implement-spec", "tdd", "code-review"} <= skills.keys()

    copy = Path(skills["tdd"]["path"]).parents[1]
    record = json.loads((copy / "UPSTREAM.json").read_text())
    assert record["repository"] == "https://github.com/mattpocock/skills.git"
    assert re.fullmatch(r"[0-9a-f]{40}", record["commit"])
    assert (copy / "LICENSE").read_text().startswith("MIT License")
