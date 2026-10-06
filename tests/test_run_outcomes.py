"""Seam A: classifying how a run ends, and refusing runs that need setup.

Each test is named after an acceptance criterion of #38.
"""

from __future__ import annotations

import json
import subprocess
import time
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import pytest
from conftest import (
    make_repo,
    probe_reports,
    probe_request,
    sandbox_processes,
    sandboxes_of,
    wait_for,
    wait_until_ended,
)

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


# --------------------------------------------------------------------------- the agent's error kind


def test_a_probe_emitting_an_authentication_failure_error_kind_is_needs_setup_and_the_reason_includes_the_error(
    worker,
):
    _, final = _run(
        worker,
        {"end": "error", "errorKind": "authentication_failed", "message": "401 Invalid bearer token"},
    )

    assert final.state is RunState.ENDED
    assert final.outcome is Outcome.NEEDS_SETUP
    assert "credential" in final.reason  # the problem
    assert "401 Invalid bearer token" in final.reason and "authentication_failed" in final.reason  # the error


def test_an_agent_answering_with_acps_authentication_required_code_is_needs_setup(worker):
    # claude-agent-acp's answer when Claude Code says "Please run /login": no errorKind.
    _, final = _run(worker, {"end": "error", "code": -32000, "errorKind": None, "message": "Authentication required"})

    assert final.outcome is Outcome.NEEDS_SETUP
    assert "credential" in final.reason and "Authentication required" in final.reason


@pytest.mark.parametrize("kind", ["rate_limit", "billing_error"])
def test_a_probe_emitting_a_rate_limit_or_billing_error_kind_is_quota_exhausted(worker, kind):
    _, final = _run(worker, {"end": "error", "errorKind": kind, "message": "You've hit your limit"})

    assert final.outcome is Outcome.QUOTA_EXHAUSTED
    assert "usage limit" in final.reason
    assert "You've hit your limit" in final.reason and kind in final.reason


def test_classification_uses_the_agents_error_kind_not_the_error_text(worker):
    # The text looks like an auth failure (OpenHands itself would call it ACPAuthRequired),
    # but the agent's own error kind says rate limit.
    _, final = _run(worker, {"end": "error", "errorKind": "rate_limit", "message": "401 Unauthorized: credential"})

    assert final.outcome is Outcome.QUOTA_EXHAUSTED


def test_an_agent_error_with_an_unknown_or_no_error_kind_is_infra_failure_with_the_error(worker):
    _, unknown = _run(worker, {"end": "error", "errorKind": "server_error", "message": "upstream 500"})
    _, none = _run(worker, {"end": "error", "errorKind": None, "message": "the agent crashed"})

    assert unknown.outcome is Outcome.INFRA_FAILURE
    assert "agent failed" in unknown.reason and "upstream 500" in unknown.reason and "server_error" in unknown.reason
    assert none.outcome is Outcome.INFRA_FAILURE
    assert "agent failed" in none.reason and "the agent crashed" in none.reason


def test_a_failing_credential_is_reported_without_the_sdks_default_multi_retry_delay(worker):
    started, final = _run(worker, {"end": "error", "errorKind": "authentication_failed", "message": "401"})
    record = worker.record(started.run_id)

    # Every prompt attempt makes the probe report; the SDK's default would make four
    # attempts, 5 s + 15 s + 30 s apart.
    assert final.outcome is Outcome.NEEDS_SETUP
    assert len(probe_reports(record.event_log)) == 1
    reported_at = _event_times(record.event_log, "ACPToolCallEvent")[0]
    errored_at = _event_times(record.event_log, "ConversationErrorEvent")[-1]
    assert (errored_at - reported_at).total_seconds() < 4


def _event_times(event_log: Path, kind: str):
    times = []
    for line in event_log.read_text().splitlines():
        ev = json.loads(line)
        if ev.get("kind") == kind:
            times.append(datetime.fromisoformat(ev["timestamp"]))
    assert times, f"no {kind} in the event log"
    return times


# --------------------------------------------------------------------------- the sandbox


def test_a_sandbox_that_fails_to_come_up_is_infra_failure(worker, probe_profile):
    worker.settings.agent_profiles["probe"] = replace(probe_profile, image="weave/no-such-image:t38")
    _, final = _run(worker, {"end": "succeed"})

    assert final.outcome is Outcome.INFRA_FAILURE
    assert "sandbox" in final.reason and "weave/no-such-image:t38" in final.reason


def test_a_sandbox_that_dies_mid_run_is_infra_failure(worker):
    started = worker.start(probe_request({"end": "hang", "command": "sleep 600"}, product=PRODUCT))
    [sandbox] = wait_for(lambda: sandboxes_of(started.run_id), what="the run's sandbox")
    wait_for(lambda: "sleep 600" in sandbox_processes(sandbox), what="the probe's hanging command")

    subprocess.run(["docker", "kill", sandbox], check=True, capture_output=True)
    final = wait_until_ended(worker, started.run_id, timeout=120)

    assert final.state is RunState.ENDED
    assert final.outcome is Outcome.INFRA_FAILURE
    assert "sandbox" in final.reason
