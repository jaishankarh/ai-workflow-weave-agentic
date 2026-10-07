"""Seam A: leasing Subscriptions from the Sandbox host's Subscription store.

Each test is named after an acceptance criterion of #37.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest
from conftest import probe_reports, probe_request, wait_until_ended

from workflow_weave.agent_worker import AgentWorker, NoCapacity, Outcome, Started, load_subscription_store

STORE = """
subscriptions:
  cursor-a:
    agent: probe
    cap: 1
    env: {PROBE_TOKEN: token-of-cursor-a}
  cursor-b:
    agent: probe
    cap: 1
    env: {PROBE_TOKEN: token-of-cursor-b}
  shared:
    agent: probe
    cap: 1
    env: {PROBE_TOKEN: token-of-shared}
  shared-backup:
    agent: probe
    cap: 1
    env: {PROBE_TOKEN: token-of-shared-backup}
products:
  product-a:
    probe: [cursor-a]
  product-b:
    probe: [cursor-b]
  product-c:
    probe: [shared]
  product-d:
    probe: [shared, shared-backup]
  product-e:
    probe: [shared]
"""

HANG = {"end": "hang", "command": "sleep 600"}


@pytest.fixture
def store(tmp_path: Path):
    path = tmp_path / "subscriptions.yaml"
    path.write_text(STORE)
    return load_subscription_store(path)


@pytest.fixture
def worker(make_worker, store):
    w = make_worker(products=["product-a", "product-b", "product-c", "product-d", "product-e"], subscriptions=store)
    yield w
    w.shutdown()


def test_fallback_order_is_respected_second_subscription_leased_only_when_first_is_full(worker):
    first = worker.start(probe_request(HANG, product="product-d"))
    assert isinstance(first, Started)
    assert first.subscription == "shared"

    second = worker.start(probe_request(HANG, product="product-d"))
    assert isinstance(second, Started)
    assert second.subscription == "shared-backup"


def test_a_run_for_product_a_never_leases_a_subscription_associated_only_with_product_b(worker):
    first = worker.start(probe_request(HANG, product="product-a"))
    assert isinstance(first, Started) and first.subscription == "cursor-a"

    # product-a's only Subscription is full; product-b's cursor-b has room but is not product-a's.
    second = worker.start(probe_request(HANG, product="product-a"))
    assert isinstance(second, NoCapacity)
    assert second.subscriptions == ["cursor-a"]

    other = worker.start(probe_request(HANG, product="product-b"))
    assert isinstance(other, Started) and other.subscription == "cursor-b"


def test_two_products_sharing_a_subscription_with_cap_1_cannot_run_on_it_at_once(worker):
    first = worker.start(probe_request(HANG, product="product-c"))
    assert isinstance(first, Started) and first.subscription == "shared"

    second = worker.start(probe_request(HANG, product="product-e"))

    assert isinstance(second, NoCapacity)
    assert second.product == "product-e"
    assert second.subscriptions == ["shared"]


def _second_start_succeeds(worker, product: str = "product-a") -> None:
    """cursor-a has cap 1: a new run can lease it only if the last lease was released."""
    again = worker.start(probe_request(HANG, product=product))
    assert isinstance(again, Started), f"lease was not released: {again}"
    worker.cancel(again.run_id)


def test_a_lease_is_released_after_succeeded(worker):
    started = worker.start(probe_request({"end": "succeed"}, product="product-a"))
    assert wait_until_ended(worker, started.run_id).outcome is Outcome.SUCCEEDED
    _second_start_succeeds(worker)


def test_a_lease_is_released_after_agent_gave_up(worker):
    started = worker.start(probe_request({"end": "give-up", "reason": "no"}, product="product-a"))
    assert wait_until_ended(worker, started.run_id).outcome is Outcome.AGENT_GAVE_UP
    _second_start_succeeds(worker)


def test_a_lease_is_released_after_infra_failure(worker, probe_profile):
    broken = replace(probe_profile, name="broken", image="weave/no-such-image:test", agent="probe")
    broken_worker = AgentWorker(
        replace(worker.settings, agent_profiles={**worker.settings.agent_profiles, "broken": broken})
    )
    request = replace(probe_request({"end": "succeed"}, product="product-a"), agent_profile="broken")
    started = broken_worker.start(request)
    assert wait_until_ended(broken_worker, started.run_id).outcome is Outcome.INFRA_FAILURE
    _second_start_succeeds(worker)


def test_a_lease_is_released_after_cancel(worker):
    started = worker.start(probe_request(HANG, product="product-a"))
    assert isinstance(worker.start(probe_request(HANG, product="product-a")), NoCapacity)
    assert worker.cancel(started.run_id).is_cancelled
    _second_start_succeeds(worker)


def _fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def test_the_probe_sees_the_leased_credential_env_and_start_result_and_run_record_carry_only_the_name(
    worker, runs_dir
):
    started = worker.start(probe_request({"end": "succeed", "fingerprint_env": ["PROBE_TOKEN"]}, product="product-a"))
    wait_until_ended(worker, started.run_id)

    [report] = probe_reports(worker.record(started.run_id).event_log)
    assert report["env_fingerprints"] == {"PROBE_TOKEN": _fingerprint("token-of-cursor-a")}

    assert started.subscription == "cursor-a"
    assert "token-of-cursor-a" not in repr(started)
    record_text = (runs_dir / started.run_id / "record.json").read_text()
    assert json.loads(record_text)["subscription"] == "cursor-a"
    for saved in (runs_dir / started.run_id).rglob("*"):
        if saved.is_file():
            assert "token-of-cursor-a" not in saved.read_text(), f"credential leaked into {saved.name}"


def test_credentials_reach_the_sandbox_as_environment_not_as_openhands_conversation_secrets(worker):
    started = worker.start(probe_request({"end": "succeed"}, product="product-a"))
    wait_until_ended(worker, started.run_id)

    [report] = probe_reports(worker.record(started.run_id).event_log)
    assert "PROBE_TOKEN" in report["env_names"]
    # OpenHands advertises conversation secrets to the agent in a <CUSTOM_SECRETS> prompt block.
    assert report["prompt_has_secrets_block"] is False
