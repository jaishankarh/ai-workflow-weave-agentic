"""Seam A: classifying how a run ends, and refusing runs that need setup.

Each test is named after an acceptance criterion of #38.
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest
from conftest import make_repo, probe_reports, probe_request, sandboxes_of, wait_for, wait_until_ended

from workflow_weave.agent_worker import NeedsSetup, Outcome, RunState, Started, load_subscription_store

# A Product name of its own, so "no sandbox started" can be checked on a shared Sandbox host.
PRODUCT = "outcomes-product"


def _store(tmp_path: Path, associations: str):
    path = tmp_path / "outcomes-subscriptions.yaml"
    path.write_text(
        "subscriptions:\n"
        "  probe-sub:\n    agent: probe\n    cap: 5\n    env: {PROBE_TOKEN: t}\n"
        "  other-agent-sub:\n    agent: some-other-agent\n    cap: 5\n    env: {OTHER_TOKEN: t}\n"
        f"products:\n  {PRODUCT}:\n{associations}"
    )
    return load_subscription_store(path)


@pytest.fixture
def worker(make_worker, tmp_path):
    w = make_worker(products=[PRODUCT], subscriptions=_store(tmp_path, "    probe: [probe-sub]\n"))
    yield w
    w.shutdown()


def _sandboxes_of_product() -> list[str]:
    out = subprocess.run(
        ["docker", "ps", "-aq", "--filter", f"label=weave.product={PRODUCT}"],
        check=True, capture_output=True, text=True,
    ).stdout
    return out.split()


def _assert_refused_without_a_sandbox(worker, request) -> NeedsSetup:
    t0 = time.monotonic()
    result = worker.start(request)
    elapsed = time.monotonic() - t0

    assert isinstance(result, NeedsSetup), result
    assert result.outcome is Outcome.NEEDS_SETUP
    # Starting a sandbox alone takes seconds; a refusal does not.
    assert elapsed < 2.0, f"refusal took {elapsed:.1f}s"
    assert _sandboxes_of_product() == []
    # It took no lease and left no run behind.
    assert worker.settings.subscriptions.in_use("probe-sub") == 0
    assert not list(worker.settings.runs_dir.iterdir())
    return result


def _run(worker, script: dict, **kw):
    started = worker.start(probe_request(script, product=PRODUCT, **kw))
    assert isinstance(started, Started), started
    return started, wait_until_ended(worker, started.run_id)


# --------------------------------------------------------------------------- pre-checks


def _product_yaml(**repos: Path) -> str:
    lines = [f"product: {PRODUCT}", "repos:"]
    for name, source in repos.items():
        lines += [f"  {name}:", f"    source: {source}"]
    return "\n".join(lines) + "\n"


def test_a_repo_without_context_md_is_needs_setup_naming_the_repo_and_no_sandbox_is_started(
    make_worker, tmp_path, onboarded_repo
):
    bare = make_repo(tmp_path / "repos" / "not-onboarded", {"README.md": "# no context here\n"})
    worker = make_worker(
        products=[PRODUCT],
        subscriptions=_store(tmp_path, "    probe: [probe-sub]\n"),
        product_yaml=_product_yaml(app=onboarded_repo, **{"not-onboarded": bare}),
    )

    refused = _assert_refused_without_a_sandbox(
        worker, probe_request({"end": "succeed"}, product=PRODUCT, repos=["app", "not-onboarded"])
    )

    assert "not-onboarded" in refused.reason
    assert "CONTEXT.md" in refused.reason
    assert "'app'" not in refused.reason  # the onboarded Repo is not blamed


def test_a_repo_whose_context_md_exists_only_off_its_base_branch_is_needs_setup(make_worker, tmp_path):
    repo = make_repo(tmp_path / "repos" / "late", {"README.md": "# late\n"})
    git = lambda *a: subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True)  # noqa: E731
    git("checkout", "-q", "-b", "onboarding")
    (repo / "CONTEXT.md").write_text("# Context\n")
    git("add", "-A")
    git("commit", "-qm", "onboard")
    worker = make_worker(
        products=[PRODUCT],
        subscriptions=_store(tmp_path, "    probe: [probe-sub]\n"),
        product_yaml=_product_yaml(late=repo),
    )

    refused = _assert_refused_without_a_sandbox(
        worker, probe_request({"end": "succeed"}, product=PRODUCT, repos=["late"])
    )
    assert "'late'" in refused.reason and "CONTEXT.md" in refused.reason and "main" in refused.reason


@pytest.mark.parametrize(
    "associations",
    ["    some-other-agent: [other-agent-sub]\n", "    probe: []\n"],
    ids=["associated-only-for-another-agent", "empty-association"],
)
def test_a_product_with_no_subscription_for_the_agent_is_needs_setup_and_no_sandbox_is_started(
    make_worker, tmp_path, associations
):
    worker = make_worker(products=[PRODUCT], subscriptions=_store(tmp_path, associations))

    refused = _assert_refused_without_a_sandbox(worker, probe_request({"end": "succeed"}, product=PRODUCT))

    assert PRODUCT in refused.reason
    assert "Subscription" in refused.reason and "probe" in refused.reason
