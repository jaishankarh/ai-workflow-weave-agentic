"""Fixes from the review of Spec 2's integration branch (#21), each through the agent worker's start.

Seam A with no Docker, as in test_bring_up_outcomes.py and test_test_secrets.py: a whole run is started
on a fixture Product while a scripted stand-in plays the sandbox. Each test is named after the
behaviour a story of the Spec asks for: a missing Test secret stops the run before any sandbox starts
(#52), and a Repo with a Run recipe on the Story's branch gets an Environment (#49).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from conftest import PRODUCT, make_repo, probe_request, product_yaml_for, repo_with_recipe, wait_until_ended
from test_bring_up_outcomes import (  # noqa: F401  (fixtures: engine, probe_profile)
    RECIPE, engine, probe_profile, ups,
)
from test_test_secrets import KOR_KEY, STRIPE_KEY, stand_in_docker, write_secrets  # noqa: F401

from workflow_weave.agent_worker import NeedsSetup, Outcome, Started
from workflow_weave.agent_worker.secret_store import SecretStore

NEEDS_STRIPE = "x-weave:\n  secrets: [STRIPE_KEY]\n"


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def commit_on_branch(repo: Path, branch: str, files: dict[str, str]) -> None:
    """Commit `files` on a new branch of the clone, leaving `main` checked out."""
    git(repo, "checkout", "-q", "-b", branch)
    for rel, body in files.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(body)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "story work")
    git(repo, "checkout", "-q", "main")


def with_secrets(worker, store):
    worker.settings = worker.settings.__class__(**{**vars(worker.settings), "test_secrets": store})
    return worker


def docker_started_a_sandbox(calls) -> bool:
    return any(c[:1] in (["run"], ["create"]) for c in calls())


# ---------------------------------------------------------------------------- #52: secrets of every recipe in play


def test_a_secret_named_by_a_repo_the_run_does_not_touch_but_a_recipe_depends_on_stops_the_run_as_needs_setup(
    make_worker, tmp_path, stand_in_docker
):
    app = repo_with_recipe(tmp_path / "r", "app", "x-weave:\n  depends_on: [dep]\n")
    dep = repo_with_recipe(tmp_path / "r", "dep", NEEDS_STRIPE)
    worker = with_secrets(
        make_worker(product_yaml=product_yaml_for({"app": app, "dep": dep}, PRODUCT)),
        SecretStore(write_secrets(tmp_path / "s", PRODUCT, {"KORONA_API_KEY": KOR_KEY}).parent),
    )
    try:
        result = worker.start(probe_request({"end": "succeed"}, repos=["app"]))
    finally:
        worker.shutdown()
    assert isinstance(result, NeedsSetup) and result.outcome is Outcome.NEEDS_SETUP
    assert "dep" in result.reason and "STRIPE_KEY" in result.reason
    assert not docker_started_a_sandbox(stand_in_docker), "a sandbox was started"


def test_a_secret_named_by_a_dependency_of_a_dependency_is_found_too(make_worker, tmp_path, stand_in_docker):
    app = repo_with_recipe(tmp_path / "r", "app", "x-weave:\n  depends_on: [mid]\n")
    mid = repo_with_recipe(tmp_path / "r", "mid", "x-weave:\n  depends_on: [dep]\n")
    dep = repo_with_recipe(tmp_path / "r", "dep", NEEDS_STRIPE)
    worker = with_secrets(
        make_worker(product_yaml=product_yaml_for({"app": app, "mid": mid, "dep": dep}, PRODUCT)),
        SecretStore(write_secrets(tmp_path / "s", PRODUCT, {"KORONA_API_KEY": KOR_KEY}).parent),
    )
    try:
        result = worker.start(probe_request({"end": "succeed"}, repos=["app"]))
    finally:
        worker.shutdown()
    assert isinstance(result, NeedsSetup) and "dep" in result.reason and "STRIPE_KEY" in result.reason


def test_a_secret_named_by_a_task_branchs_recipe_stops_the_run_as_needs_setup_before_any_sandbox(
    make_worker, tmp_path, stand_in_docker
):
    app = repo_with_recipe(tmp_path / "r", "app")
    other = repo_with_recipe(tmp_path / "r", "other")
    commit_on_branch(other, "story-1", {".weave/compose.yaml": NEEDS_STRIPE})
    worker = with_secrets(
        make_worker(product_yaml=product_yaml_for({"app": app, "other": other}, PRODUCT)),
        SecretStore(write_secrets(tmp_path / "s", PRODUCT, {"KORONA_API_KEY": KOR_KEY}).parent),
    )
    try:
        result = worker.start(probe_request({"end": "succeed"}, repos=["app", "other"]))
    finally:
        worker.shutdown()
    assert isinstance(result, NeedsSetup) and result.outcome is Outcome.NEEDS_SETUP
    assert "other" in result.reason and "STRIPE_KEY" in result.reason
    assert not docker_started_a_sandbox(stand_in_docker), "a sandbox was started"


def test_a_secret_a_dependency_names_and_the_product_has_does_not_stop_the_run(make_worker, tmp_path, stand_in_docker):
    app = repo_with_recipe(tmp_path / "r", "app", "x-weave:\n  depends_on: [dep]\n")
    dep = repo_with_recipe(tmp_path / "r", "dep", NEEDS_STRIPE)
    worker = with_secrets(
        make_worker(product_yaml=product_yaml_for({"app": app, "dep": dep}, PRODUCT)),
        SecretStore(write_secrets(tmp_path / "s", PRODUCT, {"STRIPE_KEY": STRIPE_KEY}).parent),
    )
    try:
        result = worker.start(probe_request({"end": "succeed"}, repos=["app"]))
        assert isinstance(result, Started)
        wait_until_ended(worker, result.run_id, timeout=30)
    finally:
        worker.shutdown()


# ---------------------------------------------------------------------------- #49: which runs get an Environment


def run_with(make_worker, tmp_path, repos: dict[str, Path], touched: list[str]):
    worker = make_worker(product_yaml=product_yaml_for(repos, PRODUCT))
    try:
        started = worker.start(probe_request({"end": "succeed"}, repos=touched))
        assert isinstance(started, Started)
        return worker, started, wait_until_ended(worker, started.run_id, timeout=30)
    finally:
        worker.shutdown()


def test_a_repo_whose_first_run_recipe_is_on_the_storys_task_branch_still_gets_an_environment(
    make_worker, tmp_path, engine
):
    app = make_repo(tmp_path / "r" / "app", {"CONTEXT.md": "# Context: app\n"})
    svc = make_repo(tmp_path / "r" / "svc", {"CONTEXT.md": "# Context: svc\n"})  # no recipe at Base
    commit_on_branch(svc, "story-1", {".weave/compose.yaml": RECIPE})
    run_with(make_worker, tmp_path, {"app": app, "svc": svc}, ["app", "svc"])
    assert len([c for c in ups(engine) if "-p svc" in c]) == 1, "the recipe on the Story's branch was never brought up"
    assert engine.agents_started == ["conversation"]


def test_a_repo_with_no_run_recipe_anywhere_still_runs_with_no_environment(make_worker, tmp_path, engine):
    app = make_repo(tmp_path / "r" / "app", {"CONTEXT.md": "# Context: app\n"})
    svc = make_repo(tmp_path / "r" / "svc", {"CONTEXT.md": "# Context: svc\n"})
    commit_on_branch(svc, "story-1", {"README.md": "no recipe here\n"})
    run_with(make_worker, tmp_path, {"app": app, "svc": svc}, ["app", "svc"])
    assert ups(engine) == [] and engine.agents_started == ["conversation"]
    assert not any("docker" in c for c in engine.commands), "a Repo with no recipe got Environment commands"


# ---------------------------------------------------------------------------- a recipe that cannot be read


def test_a_recipe_that_cannot_be_read_is_reported_as_a_fault_not_taken_for_no_recipe(
    make_worker, tmp_path, engine
):
    app = repo_with_recipe(tmp_path / "r", "app")
    # Lose the object holding `.weave/`: the Base branch is readable, but the recipe can't be told apart.
    tree = subprocess.run(["git", "-C", str(app), "rev-parse", "HEAD:.weave"], capture_output=True, text=True,
                          check=True).stdout.strip()
    (app / ".git" / "objects" / tree[:2] / tree[2:]).unlink()
    _, _, final = run_with(make_worker, tmp_path, {"app": app}, ["app"])
    assert final.outcome is Outcome.INFRA_FAILURE, final.reason
    assert "app" in final.reason
    assert engine.agents_started == [], "the run went on as if the Repo had no recipe"
    assert "AgentStarted" not in (final.reason or "")
