"""Seam A: each Repo's Coding standards and the always-on file (#41).

Each test is named after an acceptance criterion of #41. The probe reports the
always-on file it finds at the agent's user level and the content of every file
that file points at, plus each working copy's `git status` and content digest.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from conftest import (
    PRODUCT, PROBE_SUBSCRIPTION, commit_on_code_host, make_code_host, make_repo, probe_reports, probe_request,
    wait_until_ended,
)
from test_skill_staging import tree_digest

from workflow_weave.agent_worker import (
    NeedsSetup,
    Outcome,
    ProductConfigError,
    Started,
    load_product_config,
)


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


# --------------------------------------------------------------------------- the always-on file (Seam A, real sandboxes)

PRODUCT_STANDARDS = "# Coding standards: probe-product\n\n- No swallowed errors.\n"
CONTEXT = "# Context: app\n\nGlossary only.\n"
REPO_CLAUDE_MD = "# app\n\nAlways run the type checker.\n"
REPO_RULE = "# Rule: naming\n\nUse the glossary's words.\n"


@pytest.fixture
def central(tmp_path: Path) -> Path:
    """A Central skills location with one skill and the Product's coding-standards file."""
    loc = tmp_path / "central" / "skills"
    files = {
        "ours/work/SKILL.md": "---\nname: work\ndescription: our work\n---\n\nDo the work.\n",
        f"products/{PRODUCT}/coding-standards.md": PRODUCT_STANDARDS,
    }
    for rel, text in files.items():
        (loc / rel).parent.mkdir(parents=True, exist_ok=True)
        (loc / rel).write_text(text)
    return loc


@pytest.fixture
def ruled_repo(tmp_path: Path) -> Path:
    """An onboarded Repo with its own root CLAUDE.md and a rules folder."""
    return make_repo(tmp_path / "repos" / "ruled", {
        "CONTEXT.md": CONTEXT,
        "CLAUDE.md": REPO_CLAUDE_MD,
        ".claude/rules/naming.md": REPO_RULE,
        "README.md": "# not a standard\n",
    })


def standards_yaml(source: Path, mode: str | None = None, rules: list[str] = ()) -> str:
    lines = [f"product: {PRODUCT}", "repos:", "  app:", f"    source: {source}"]
    if mode:
        lines.append(f"    coding_standards: {mode}")
    if rules:
        lines.append(f"    rules_files: [{', '.join(rules)}]")
    return "\n".join(lines) + "\n"


def run_probe(worker, **script):
    started = worker.start(probe_request({"end": "succeed", **script}, skill="work"))
    assert isinstance(started, Started), started
    final = wait_until_ended(worker, started.run_id)
    assert final.outcome is Outcome.SUCCEEDED, final.reason
    [report] = probe_reports(worker.record(started.run_id).event_log)
    return report


def pointed_contents(report) -> list[str]:
    """What the always-on file points the agent at, as file contents (order kept)."""
    always_on = report["always_on"]
    assert always_on is not None, "no always-on file at the agent's user level"
    assert None not in always_on["pointed"].values(), always_on
    return list(always_on["pointed"].values())


def test_central_the_always_on_file_lists_only_the_products_coding_standards_file(make_worker, central, ruled_repo):
    worker = make_worker(central_skills_location=central, product_yaml=standards_yaml(ruled_repo))

    assert sorted(pointed_contents(run_probe(worker))) == sorted([CONTEXT, PRODUCT_STANDARDS])


def test_central_plus_repo_it_lists_the_product_file_and_the_repos_named_rules_files(make_worker, central, ruled_repo):
    worker = make_worker(
        central_skills_location=central,
        product_yaml=standards_yaml(ruled_repo, "central+repo", ["CLAUDE.md", ".claude/rules"]),
    )

    assert sorted(pointed_contents(run_probe(worker))) == sorted([CONTEXT, PRODUCT_STANDARDS, REPO_CLAUDE_MD, REPO_RULE])


def test_repo_it_lists_only_the_repos_named_rules_files(make_worker, central, ruled_repo):
    worker = make_worker(
        central_skills_location=central, product_yaml=standards_yaml(ruled_repo, "repo", [".claude/rules/naming.md"])
    )

    assert sorted(pointed_contents(run_probe(worker))) == sorted([CONTEXT, REPO_RULE])


def test_a_rules_file_changed_on_the_working_branch_is_still_read_in_its_base_branch_version(
    make_worker, central, ruled_repo
):
    # The rule is relaxed on the run's Integration branch in the Repo, and again by the
    # agent in its working copy before it reads its standards.
    git = lambda *a: subprocess.run(["git", "-C", str(ruled_repo), *a], check=True, capture_output=True)  # noqa: E731
    git("checkout", "-q", "-b", "story-1")
    (ruled_repo / ".claude/rules/naming.md").write_text("# Rule: naming\n\nAnything goes.\n")
    git("commit", "-qam", "relax the rule")
    worker = make_worker(
        central_skills_location=central, product_yaml=standards_yaml(ruled_repo, "repo", [".claude/rules"])
    )

    report = run_probe(worker, edit={".claude/rules/naming.md": "# Rule: naming\n\nThe agent says anything goes.\n"})
    contents = pointed_contents(report)

    assert [a["ok"] for a in report["actions"]] == [True]

    assert REPO_RULE in contents
    assert not [c for c in contents if "anything goes" in c.lower()]


def test_rules_files_are_read_from_the_code_hosts_base_branch_not_a_stale_clone(make_worker, central, tmp_path):
    code_host, clone = make_code_host(tmp_path / "hosts", {"CONTEXT.md": CONTEXT, "CLAUDE.md": REPO_CLAUDE_MD})
    # The rule changed on the Code host's Base branch after the Sandbox host's clone last fetched.
    newer = "# app\n\nAlways run the type checker and the linter.\n"
    commit_on_code_host(code_host, {"CLAUDE.md": newer, ".claude/rules/naming.md": REPO_RULE})
    worker = make_worker(
        central_skills_location=central, product_yaml=standards_yaml(clone, "repo", ["CLAUDE.md", ".claude/rules"])
    )

    assert sorted(pointed_contents(run_probe(worker))) == sorted([CONTEXT, newer, REPO_RULE])


def test_the_always_on_file_points_at_the_repos_context_md_and_the_repos_own_root_claude_md_is_untouched(
    make_worker, central, ruled_repo
):
    worker = make_worker(
        central_skills_location=central, product_yaml=standards_yaml(ruled_repo, "central+repo", ["CLAUDE.md"])
    )
    report = run_probe(worker)

    context_paths = [p for p, text in report["always_on"]["pointed"].items() if text == CONTEXT]
    assert context_paths == ["/workspace/app/CONTEXT.md"]  # the working copy's own glossary
    assert report["always_on"]["path"] != "/workspace/app/CLAUDE.md"
    copy = report["working_copies"]["app"]
    assert copy["status"] == ""
    assert copy["digest"] == tree_digest(ruled_repo)


# --------------------------------------------------------------------------- missing standards: needs-setup


def assert_refused_without_a_sandbox(worker) -> NeedsSetup:
    result = worker.start(probe_request({"end": "succeed"}, skill="work"))
    assert isinstance(result, NeedsSetup), result
    assert result.outcome is Outcome.NEEDS_SETUP
    assert not list(worker.settings.runs_dir.iterdir())  # no run, so no sandbox
    assert worker.settings.subscriptions.in_use(PROBE_SUBSCRIPTION) == 0
    return result


def test_a_missing_products_coding_standards_file_is_needs_setup_naming_it(make_worker, central, ruled_repo):
    (central / "products" / PRODUCT / "coding-standards.md").unlink()
    worker = make_worker(central_skills_location=central, product_yaml=standards_yaml(ruled_repo, "central+repo",
                                                                                     ["CLAUDE.md"]))

    refused = assert_refused_without_a_sandbox(worker)

    assert f"products/{PRODUCT}/coding-standards.md" in refused.reason


def test_a_missing_products_coding_standards_file_is_fine_when_the_repo_uses_only_its_own_rules(
    make_worker, central, ruled_repo
):
    (central / "products" / PRODUCT / "coding-standards.md").unlink()
    worker = make_worker(central_skills_location=central, product_yaml=standards_yaml(ruled_repo, "repo", ["CLAUDE.md"]))

    assert sorted(pointed_contents(run_probe(worker))) == sorted([CONTEXT, REPO_CLAUDE_MD])


def test_a_named_rules_file_missing_from_the_base_branch_is_needs_setup_naming_it(make_worker, central, ruled_repo):
    # It exists only on another branch, so the Base branch has no such file.
    git = lambda *a: subprocess.run(["git", "-C", str(ruled_repo), *a], check=True, capture_output=True)  # noqa: E731
    git("checkout", "-q", "-b", "story-1")
    (ruled_repo / "STYLE.md").write_text("# Style\n")
    git("add", "-A")
    git("commit", "-qm", "add a style file off the Base branch")
    worker = make_worker(
        central_skills_location=central, product_yaml=standards_yaml(ruled_repo, "central+repo", ["CLAUDE.md", "STYLE.md"])
    )

    refused = assert_refused_without_a_sandbox(worker)

    assert "STYLE.md" in refused.reason and "'app'" in refused.reason and "main" in refused.reason
    assert "CLAUDE.md" not in refused.reason.replace("STYLE.md", "")
