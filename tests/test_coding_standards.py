"""Seam A: each Repo's Coding standards and the always-on file (#41).

Each test is named after an acceptance criterion of #41. The probe reports the
always-on file it finds at the agent's user level and the content of every file
that file points at, plus each working copy's `git status` and content digest.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from workflow_weave.agent_worker import ProductConfigError, load_product_config


def write_config(tmp_path: Path, repo_lines: str) -> Path:
    config = tmp_path / "product.yaml"
    config.write_text(f"product: p\nrepos:\n  app:\n    source: {tmp_path}\n{repo_lines}")
    return config


@pytest.mark.parametrize("mode", ["central+repo", "repo"])
@pytest.mark.parametrize("rules", ["", "    rules_files: []\n"], ids=["absent", "empty"])
def test_central_plus_repo_or_repo_without_named_rules_files_is_rejected_when_the_config_is_loaded(
    tmp_path, mode, rules
):
    config = write_config(tmp_path, f"    coding_standards: {mode}\n{rules}")

    with pytest.raises(ProductConfigError, match="rules_files"):
        load_product_config(config)


def test_coding_standards_default_to_central_and_name_no_rules_files(tmp_path):
    repo = load_product_config(write_config(tmp_path, "")).repos["app"]

    assert repo.coding_standards == "central"
    assert repo.rules_files == ()


def test_an_unknown_coding_standards_mode_is_rejected_when_the_config_is_loaded(tmp_path):
    with pytest.raises(ProductConfigError, match="coding_standards"):
        load_product_config(write_config(tmp_path, "    coding_standards: strict\n"))


def test_a_repo_mode_config_keeps_its_named_rules_files_in_order(tmp_path):
    repo = load_product_config(
        write_config(tmp_path, "    coding_standards: repo\n    rules_files: [CLAUDE.md, .claude/rules]\n")
    ).repos["app"]

    assert repo.coding_standards == "repo"
    assert repo.rules_files == ("CLAUDE.md", ".claude/rules")
