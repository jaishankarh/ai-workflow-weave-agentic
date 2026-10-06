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


def branch_tip(repo: Path, branch: str) -> str | None:
    r = subprocess.run(["git", "-C", str(repo), "rev-parse", "-q", "--verify", f"refs/heads/{branch}"],
                       capture_output=True, text=True)
    return r.stdout.strip() or None


def test_the_probe_can_push_to_the_runs_integration_branch(worker, onboarded_repo):
    final, _, report = run(worker, {"end": "succeed", "push": ["story-1"]})

    assert final.outcome is Outcome.SUCCEEDED, final.reason
    [push] = [a for a in report["actions"] if a["action"] == "push"]
    assert push["ok"], push
    # The commit reached the Repo itself, on the Integration branch.
    assert branch_tip(onboarded_repo, "story-1") == push["commit"]


def test_pushing_to_any_other_branch_including_the_base_branch_is_refused(worker, onboarded_repo):
    main_before = branch_tip(onboarded_repo, "main")

    final, _, report = run(worker, {"end": "succeed", "push": ["main", "other-story"]})

    assert final.outcome is Outcome.SUCCEEDED, final.reason
    pushes = {a["branch"]: a for a in report["actions"] if a["action"] == "push"}
    assert not pushes["main"]["ok"], pushes["main"]
    assert not pushes["other-story"]["ok"], pushes["other-story"]
    assert "refused" in pushes["main"]["output"]
    assert branch_tip(onboarded_repo, "main") == main_before
    assert branch_tip(onboarded_repo, "other-story") is None


# Env names a Tracker or Code host token goes by (gh, glab, GitHub Actions, git hosts).
TRACKER_TOKEN_NAMES = {"GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN",
                       "GITLAB_TOKEN", "GLAB_TOKEN", "CI_JOB_TOKEN", "GITHUB_PAT"}


def test_no_tracker_or_code_host_token_is_present_in_the_sandbox_environment(make_worker, tmp_path, monkeypatch):
    # The Sandbox host itself holds one, and a Subscription's env tries to slip one in too.
    monkeypatch.setenv("GH_TOKEN", "host-token")
    store = tmp_path / "leaky-subscriptions.yaml"
    store.write_text(
        "subscriptions:\n  leaky:\n    agent: probe\n    cap: 2\n"
        "    env: {PROBE_TOKEN: probe-token, GITHUB_TOKEN: slipped-in, GITLAB_TOKEN: slipped-in}\n"
        f"products:\n  {PRODUCT}:\n    probe: [leaky]\n"
    )
    from workflow_weave.agent_worker import load_subscription_store

    worker = make_worker(subscriptions=load_subscription_store(store))
    try:
        final, _, report = run(worker, {"end": "succeed"})
    finally:
        worker.shutdown()

    assert final.outcome is Outcome.SUCCEEDED, final.reason
    assert "PROBE_TOKEN" in report["env_names"]  # the agent credential does go in
    assert TRACKER_TOKEN_NAMES.isdisjoint(report["env_names"])
    assert report["git_credentials"] == {"helpers": [], "files": []}


def test_an_attempt_to_create_an_issue_comment_label_or_open_a_pr_fails_for_lack_of_credentials(worker):
    final, _, report = run(worker, {"end": "succeed", "code_host_writes": ["issue", "comment", "label", "pr"]})

    assert final.outcome is Outcome.SUCCEEDED, final.reason
    writes = {a["write"]: a for a in report["actions"] if a["action"] == "code_host_write"}
    assert set(writes) == {"issue", "comment", "label", "pr"}
    for write in writes.values():
        assert not write["ok"], write
        assert write["credential_found"] is None, write
        assert "no credentials" in write["error"], write
