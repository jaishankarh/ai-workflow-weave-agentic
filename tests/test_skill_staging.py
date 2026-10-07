"""Seam A: staging the Central skills at the agent's user level, with Skill overrides (#40).

Each test is named after an acceptance criterion of #40. The probe reports what the
agent sees: the skills at user level, which skill it loads for each name, and each
working copy's `git status` and content digest.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest
from conftest import PRODUCT, make_repo, probe_reports, probe_request, wait_until_ended

from workflow_weave.agent_worker import PROTECTED_SKILLS, Outcome, ProductConfigError, load_product_config


def run_probe(worker, **kw):
    started = worker.start(probe_request({"end": "succeed"}, **kw))
    final = wait_until_ended(worker, started.run_id)
    record = worker.record(started.run_id)
    reports = probe_reports(record.event_log) if record.event_log and record.event_log.exists() else []
    return final, record, (reports[0] if reports else None)


def test_the_probe_sees_the_requested_skill_and_its_called_skills_at_user_level(make_worker):
    # This repo's real upstream copy: implement-spec calls the Skill tool with `tdd`
    # and `code-review`; tdd in turn calls it with "codebase-design".
    worker = make_worker()
    final, _, report = run_probe(worker, skill="implement-spec")

    assert final.outcome is Outcome.SUCCEEDED, final.reason
    assert set(report["user_skills"]) == {"implement-spec", "tdd", "code-review", "codebase-design"}


# --------------------------------------------------------------------------- fixture Central skills


def skill_md(name: str, description: str, body: str = "") -> str:
    return f"---\nname: {name}\ndescription: {description}\n---\n\n{body}\n"


def write_files(root: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        f = root / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)
    return root


@pytest.fixture
def central(tmp_path: Path) -> Path:
    """A fixture Central skills location in git, with an upstream copy and our own skills."""
    loc = write_files(tmp_path / "central" / "skills", {
        "upstream/UPSTREAM.json": json.dumps({"repository": "https://example.invalid/skills.git",
                                              "commit": "a" * 40}),
        "upstream/engineering/work/SKILL.md": skill_md(
            "work", "upstream work", "Call the Skill tool with `helper`, then call the Skill tool with \"shared\"."),
        "upstream/engineering/helper/SKILL.md": skill_md("helper", "upstream helper", "Call the Skill tool with `old`."),
        "upstream/engineering/shared/SKILL.md": skill_md("shared", "upstream shared"),
        "upstream/productivity/lonely/SKILL.md": skill_md("lonely", "never called"),
        "upstream/deprecated/old/SKILL.md": skill_md("old", "deprecated upstream skill"),
        "upstream/in-progress/wip/SKILL.md": skill_md("wip", "unfinished upstream skill"),
        "ours/shared/SKILL.md": skill_md("shared", "our shared"),
        "ours/review/SKILL.md": skill_md("review", "our review"),
        # Every Repo here uses the default `central` Coding standards (#41).
        f"products/{PRODUCT}/coding-standards.md": "# Coding standards\n",
    })
    make_repo_from(loc.parent)
    return loc


def make_repo_from(path: Path) -> None:
    git = lambda *a: subprocess.run(["git", "-C", str(path), *a], check=True, capture_output=True)  # noqa: E731
    git("init", "-q", "-b", "main")
    git("-c", "user.email=f@f", "-c", "user.name=f", "add", "-A")
    git("-c", "user.email=f@f", "-c", "user.name=f", "commit", "-qm", "central")


def product_yaml(repos: dict[str, tuple[Path, list[str]]]) -> str:
    lines = [f"product: {PRODUCT}", "repos:"]
    for name, (source, overrides) in repos.items():
        lines += [f"  {name}:", f"    source: {source}"]
        if overrides:
            lines.append(f"    skill_overrides: {json.dumps(overrides)}")
    return "\n".join(lines) + "\n"


def own_skill_repo(tmp_path: Path, name: str, own: dict[str, str]) -> Path:
    files = {"CONTEXT.md": f"# Context: {name}\n", "README.md": f"# {name}\n"}
    files |= {f".claude/skills/{s}/SKILL.md": skill_md(s, d) for s, d in own.items()}
    return make_repo(tmp_path / "own-repos" / name, files)


def test_no_upstream_deprecated_or_in_progress_skill_is_staged(make_worker, central):
    # `helper` calls the deprecated `old`; it must still not reach the run.
    worker = make_worker(central_skills_location=central)
    final, _, report = run_probe(worker, skill="work")

    assert final.outcome is Outcome.SUCCEEDED, final.reason
    assert set(report["user_skills"]) == {"work", "helper", "shared"}
    assert not {"old", "wip"} & set(report["user_skills"])


def test_with_the_same_name_in_both_our_skill_is_staged_not_upstreams(make_worker, central):
    worker = make_worker(central_skills_location=central)
    _, _, report = run_probe(worker, skill="work")

    assert report["user_skills"]["shared"] == "our shared"


def test_the_working_copy_is_byte_for_byte_unchanged_and_git_status_is_clean_after_staging(
    make_worker, central, onboarded_repo
):
    worker = make_worker(central_skills_location=central)
    _, _, report = run_probe(worker, skill="work")

    copy = report["working_copies"]["app"]
    assert copy["status"] == ""
    assert copy["digest"] == tree_digest(onboarded_repo)
    assert report["user_skills"]  # skills were staged, just not here


def tree_digest(root: Path) -> str:
    """The probe's digest, computed on the Repo as committed (independent of the worker)."""
    h = hashlib.sha256()
    files = subprocess.run(["git", "-C", str(root), "ls-files"], check=True, capture_output=True, text=True)
    for rel in sorted(files.stdout.split()):
        h.update(rel.encode() + b"\0" + (root / rel).read_bytes() + b"\0")
    return h.hexdigest()


# --------------------------------------------------------------------------- Repo's own skills and Skill overrides


def run_log(runs_dir: Path, run_id: str) -> str:
    log = runs_dir / run_id / "run.log"
    return log.read_text() if log.exists() else ""


def test_with_no_override_a_repos_own_same_named_skill_is_shadowed_by_central_and_the_clash_appears_in_the_run_log(
    make_worker, central, tmp_path, runs_dir
):
    app = own_skill_repo(tmp_path, "app", {"helper": "app's own helper", "mine": "app-only skill"})
    worker = make_worker(central_skills_location=central, product_yaml=product_yaml({"app": (app, [])}))
    final, record, report = run_probe(worker, skill="work")

    assert final.outcome is Outcome.SUCCEEDED, final.reason
    assert report["skills_loaded"]["helper"] == {"level": "user", "description": "upstream helper"}
    # The Repo's own skills still load as they normally would.
    assert report["skills_loaded"]["mine"] == {"level": "project", "repo": "app", "description": "app-only skill"}
    log = run_log(runs_dir, record.run_id)
    assert "clash" in log and "`helper`" in log and "app" in log
    assert "`mine`" not in log


def test_with_an_override_the_central_skill_is_not_staged_and_the_repos_own_skill_is_the_one_the_agent_loads(
    make_worker, central, tmp_path
):
    app = own_skill_repo(tmp_path, "app", {"helper": "app's own helper"})
    worker = make_worker(central_skills_location=central, product_yaml=product_yaml({"app": (app, ["helper"])}))
    final, record, report = run_probe(worker, skill="work")

    assert final.outcome is Outcome.SUCCEEDED, final.reason
    assert "helper" not in report["user_skills"]
    assert report["skills_loaded"]["helper"] == {"level": "project", "repo": "app", "description": "app's own helper"}
    assert {"work", "shared"} <= set(report["user_skills"])


def test_an_override_naming_a_protected_skill_is_rejected_when_the_product_config_is_loaded(tmp_path):
    [protected, *_] = sorted(PROTECTED_SKILLS)
    config = tmp_path / "product.yaml"
    config.write_text(product_yaml({"app": (tmp_path, ["tdd", protected])}))

    with pytest.raises(ProductConfigError, match=protected):
        load_product_config(config)
    for name in PROTECTED_SKILLS:  # the fix skill, the review skill, the test rules
        config.write_text(product_yaml({"app": (tmp_path, [name])}))
        with pytest.raises(ProductConfigError, match="protected"):
            load_product_config(config)
    config.write_text(product_yaml({"app": (tmp_path, ["tdd"])}))
    assert load_product_config(config).repos["app"].skill_overrides == {"tdd"}


def two_repo_run(make_worker, central, tmp_path, own_b: str, overrides_b: list[str]):
    a = own_skill_repo(tmp_path, "a", {"helper": "the Repos' own helper"})
    b = own_skill_repo(tmp_path, "b", {"helper": own_b})
    worker = make_worker(
        central_skills_location=central,
        product_yaml=product_yaml({"a": (a, ["helper"]), "b": (b, overrides_b)}),
    )
    return run_probe(worker, skill="work", repos=["a", "b"])


def test_two_repos_in_one_run_the_override_applies_when_both_have_it_with_identical_skills(
    make_worker, central, tmp_path
):
    final, _, report = two_repo_run(make_worker, central, tmp_path, "the Repos' own helper", ["helper"])

    assert final.outcome is Outcome.SUCCEEDED, final.reason
    assert "helper" not in report["user_skills"]
    assert report["skills_loaded"]["helper"]["level"] == "project"


@pytest.mark.parametrize(
    "own_b, overrides_b, why",
    [("the Repos' own helper", [], "no override in b"), ("b's different helper", ["helper"], "differ")],
    ids=["only-one-repo-has-the-override", "own-skills-differ"],
)
def test_two_repos_in_one_run_otherwise_central_is_staged_and_the_disagreement_is_reported(
    make_worker, central, tmp_path, runs_dir, own_b, overrides_b, why
):
    final, record, report = two_repo_run(make_worker, central, tmp_path, own_b, overrides_b)

    assert final.outcome is Outcome.SUCCEEDED, final.reason
    assert report["user_skills"]["helper"] == "upstream helper"
    assert report["skills_loaded"]["helper"]["level"] == "user"
    [disagreement] = [c for c in record.skill_clashes if "disagreement" in c]
    assert "`helper`" in disagreement and why in disagreement
    assert disagreement in run_log(runs_dir, record.run_id)


# --------------------------------------------------------------------------- run record and failures


def test_the_run_record_holds_the_central_skills_version_and_upstream_commit(make_worker, central):
    head = subprocess.run(["git", "-C", str(central), "rev-parse", "HEAD"], check=True,
                          capture_output=True, text=True).stdout.strip()
    worker = make_worker(central_skills_location=central)
    _, record, _ = run_probe(worker, skill="work")

    # Read back from disk, as a restarted worker would.
    saved = make_worker(central_skills_location=central).record(record.run_id)
    assert saved.central_skills["version"] == head
    assert saved.central_skills["upstream_commit"] == "a" * 40


def test_an_unreachable_central_skills_location_is_an_infra_failure(make_worker, tmp_path):
    worker = make_worker(central_skills_location=tmp_path / "nowhere" / "skills")
    started = worker.start(probe_request({"end": "succeed"}, skill="work"))
    final = wait_until_ended(worker, started.run_id)

    assert final.outcome is Outcome.INFRA_FAILURE
    assert "Central skills" in final.reason and "unreachable" in final.reason
