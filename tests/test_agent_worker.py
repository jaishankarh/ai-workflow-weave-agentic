"""Seam A: the agent worker's start / status / cancel, against real sandboxes.

Each test is named after an acceptance criterion of #36.
"""

from __future__ import annotations

import time

from conftest import (
    PRODUCT,
    probe_reports,
    probe_request,
    sandboxes_of,
    wait_for,
    wait_until_ended,
    wait_until_hanging,
)

from workflow_weave.agent_worker import Outcome, RunState, Started


def test_start_returns_a_run_id_without_waiting_for_the_run_to_finish(worker):
    t0 = time.monotonic()
    started = worker.start(probe_request({"end": "succeed"}))
    elapsed = time.monotonic() - t0

    assert isinstance(started, Started)
    assert started.run_id
    # A probe run takes well over 5s (sandbox start alone does), and it is
    # still running when start has returned.
    assert worker.status(started.run_id).is_running
    assert elapsed < 5.0, f"start blocked for {elapsed:.1f}s"

    final = wait_until_ended(worker, started.run_id)
    assert final.outcome is Outcome.SUCCEEDED


def test_each_run_gets_a_fresh_sandbox_and_sees_nothing_from_an_earlier_run(worker, runs_dir):
    first = worker.start(probe_request({"end": "succeed"}))
    wait_until_ended(worker, first.run_id)
    second = worker.start(probe_request({"end": "succeed"}))
    wait_until_ended(worker, second.run_id)

    [first_report] = probe_reports(worker.record(first.run_id).event_log)
    [second_report] = probe_reports(worker.record(second.run_id).event_log)
    assert first_report["earlier_run_markers"] == []
    assert second_report["earlier_run_markers"] == []
    assert second_report["repos"] == [{"name": "app", "branch": "story-1", "clean": True}]


def test_status_reports_running_while_live_and_exactly_one_final_outcome_after(worker):
    started = worker.start(probe_request({"end": "succeed"}))
    seen = [worker.status(started.run_id)]
    while not seen[-1].is_final:
        time.sleep(0.2)
        seen.append(worker.status(started.run_id))

    assert seen[0].is_running and seen[0].outcome is None
    finals = {(s.state, s.outcome) for s in seen if s.is_final}
    assert finals == {(RunState.ENDED, Outcome.SUCCEEDED)}
    # It stays that one outcome afterwards.
    time.sleep(1)
    assert worker.status(started.run_id) == seen[-1]


def test_the_probe_can_end_as_agent_gave_up_with_its_reason(worker):
    started = worker.start(probe_request({"end": "give-up", "reason": "the spec contradicts itself"}))
    final = wait_until_ended(worker, started.run_id)

    assert final.state is RunState.ENDED
    assert final.outcome is Outcome.AGENT_GAVE_UP
    assert "the spec contradicts itself" in final.reason


def test_when_a_run_ends_its_sandbox_is_gone_from_the_sandbox_host(worker):
    started = worker.start(probe_request({"end": "succeed"}))
    wait_for(lambda: sandboxes_of(started.run_id), what="the run's sandbox to appear")

    wait_until_ended(worker, started.run_id)
    assert sandboxes_of(started.run_id) == []


def test_run_record_and_event_log_are_saved_outside_the_sandbox_and_outlive_it(make_worker, runs_dir):
    worker = make_worker()
    started = worker.start(probe_request({"end": "give-up", "reason": "no tests to run"}))
    wait_until_ended(worker, started.run_id)
    assert sandboxes_of(started.run_id) == []

    # A new worker (e.g. after a restart) reads the record back from disk alone.
    record = make_worker().record(started.run_id)
    assert record.run_id == started.run_id
    assert record.product == PRODUCT
    assert record.agent_profile == "probe"
    assert record.started_at and record.ended_at and record.started_at <= record.ended_at
    assert record.outcome is Outcome.AGENT_GAVE_UP
    assert "no tests to run" in record.reason

    assert record.event_log.is_relative_to(runs_dir)
    [report] = probe_reports(record.event_log)
    assert report["repos"][0]["name"] == "app"


def test_cancel_stops_a_hanging_command_and_the_agent_and_status_reports_cancelled(worker):
    started = worker.start(probe_request({"end": "hang", "command": "sleep 600"}))
    wait_until_hanging(worker, started.run_id, "sleep 600")

    status = worker.cancel(started.run_id, timeout=300)

    assert status.is_cancelled
    assert status.outcome is None
    assert status.state.value == "cancelled"
    assert "cancelled" not in {o.value for o in Outcome}
    assert worker.status(started.run_id) == status
    record = worker.record(started.run_id)
    assert record.state is RunState.CANCELLED and record.ended_at
    # Checked after closing the agent and before removing the sandbox:
    # neither the hanging command nor the agent survived.
    assert record.processes_left_after_close == []
    assert sandboxes_of(started.run_id) == []


def test_cancel_closes_the_conversation_rather_than_interrupting_it(worker):
    # The probe, like Claude Code, keeps a command running after an interrupt
    # (ACP session/cancel). Only closing the conversation stops it.
    started = worker.start(probe_request({"end": "hang", "command": "sleep 600"}))
    wait_until_hanging(worker, started.run_id, "sleep 600")

    assert worker.cancel(started.run_id, timeout=300).is_cancelled

    record = worker.record(started.run_id)
    assert record.processes_left_after_close == []
    [report] = probe_reports(record.event_log)  # the log is saved even for a cancelled run
    assert report["repos"][0]["branch"] == "story-1"
