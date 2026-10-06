"""Seam A: push-only runs with the spec and Tasks as local tickets (#42, ADR 0009).

Each test is named after an acceptance criterion of #42. The probe finds the
local-files tracker the way an upstream skill does (the tracker description the
run points it at), reports the tickets it sees, tries to change the originals,
marks tickets done, pushes branches and tries Tracker / Code host writes.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from conftest import probe_reports, wait_until_ended, PRODUCT

from workflow_weave.agent_worker import Outcome, RepoTarget, RunInputs, RunRequest

import json

SPEC = "# Spec: greet people\n\nThe app greets people by name.\n"
TASKS = ["# Add a greet function\n\nAdd greet(name).\n", "# Print the greeting\n\nCall greet from main.\n"]


def request(script: dict, *, branch: str = "story-1") -> RunRequest:
    return RunRequest(
        product=PRODUCT,
        agent_profile="probe",
        repos=[RepoTarget(name="app", integration_branch=branch, base_branch="main")],
        skill="implement-spec",
        inputs=RunInputs(spec=SPEC + f"\nprobe: {json.dumps(script)}\n", tasks=TASKS),
    )


def run(worker, script: dict, **kw):
    started = worker.start(request(script, **kw))
    final = wait_until_ended(worker, started.run_id)
    record = worker.record(started.run_id)
    [report] = probe_reports(record.event_log)
    return final, record, report


def test_the_probe_finds_the_spec_and_each_task_as_local_tickets_and_cannot_modify_the_originals(worker):
    final, _, report = run(worker, {"end": "succeed"})

    assert final.outcome is Outcome.SUCCEEDED, final.reason
    tickets = report["local_tickets"]
    assert tickets["tracker"] == "local-files", tickets
    found = tickets["tickets"]
    assert [t["kind"] for t in found] == ["spec", "task", "task"]
    assert SPEC.strip() in found[0]["body"]
    assert TASKS[0].strip() in found[1]["body"]
    assert TASKS[1].strip() in found[2]["body"]
    # Every original refused every change the probe tried (write, delete, chmod).
    assert tickets["originals"], "the probe found no originals to try"
    assert tickets["originals_changed"] == []


def test_tickets_the_probe_marks_done_are_listed_in_the_run_result(make_worker):
    worker = make_worker()
    final, record, _ = run(worker, {"end": "succeed", "mark_done": ["spec", "02"]})

    assert final.outcome is Outcome.SUCCEEDED, final.reason
    assert record.tickets_done == ["spec", "02"]
    # It is part of the saved result, read back without the sandbox.
    assert make_worker().record(record.run_id).tickets_done == ["spec", "02"]
