"""What the run record says about an Environment, and which runs get one (#49).

No Docker daemon: a stand-in `docker` on PATH plays the Sandbox host. Each test is named after an
acceptance criterion of #49.
"""

from __future__ import annotations

import json
import os
import stat
import sys

import pytest
from conftest import PRODUCT, probe_request, product_yaml_for, repo_with_recipe, wait_until_ended

from workflow_weave.agent_worker import AgentProfile, Outcome, RunRecord, RunState, Started
from workflow_weave.agent_worker.sandbox import SYSBOX_RUNTIME


def test_the_run_record_names_the_services_their_readiness_times_and_their_saved_logs():
    text = RunRecord(
        run_id="r", product="p", agent_profile="a", subscription="s", skill="k", repos=[],
        state=RunState.ENDED, started_at="t",
        environment_services=[{"repo": "svc", "service": "web", "address": "web.svc",
                               "ready_at": "2026-10-08T10:00:03+00:00", "seconds_to_ready": 3.2}],
        environment_logs={"svc/web": "/runs/r/environment/svc/web.log"},
    ).to_json()
    back = RunRecord.from_json(text)
    assert back.environment_services[0]["service"] == "web"
    assert back.environment_services[0]["ready_at"] == "2026-10-08T10:00:03+00:00"
    assert back.environment_logs == {"svc/web": "/runs/r/environment/svc/web.log"}


def test_a_record_saved_before_environments_existed_still_loads_with_no_environment():
    old = RunRecord(
        run_id="r", product="p", agent_profile="a", subscription="s", skill="k", repos=[],
        state=RunState.ENDED, started_at="t",
    )
    d = json.loads(old.to_json())
    del d["environment_services"], d["environment_logs"]
    back = RunRecord.from_json(json.dumps(d))
    assert back.environment_services is None and back.environment_logs is None


@pytest.fixture
def probe_profile():
    """Replaces the image-building fixture: the stand-in host never really starts a sandbox."""
    return AgentProfile(name="probe", image="never-started:test", acp_command=["true"])


@pytest.fixture
def stand_in_docker(tmp_path, monkeypatch):
    """A `docker` on PATH for a host that has only the default runtime; records every call."""
    bin_dir = tmp_path / "fake-bin"
    bin_dir.mkdir()
    calls = tmp_path / "docker-calls.jsonl"
    script = bin_dir / "docker"
    script.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\n"
        f"open({str(calls)!r}, 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "if sys.argv[1:2] == ['info']:\n"
        "    print(json.dumps({'runc': {'path': 'runc'}}))\n"
        "    sys.exit(0)\n"
        "if sys.argv[1:3] == ['network', 'inspect']:\n"
        "    print('127.0.0.1')\n"
        "    sys.exit(0)\n"
        "sys.stderr.write('stand-in docker: not supported\\n')\n"
        "sys.exit(1)\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    return lambda: [json.loads(line) for line in calls.read_text().splitlines()] if calls.exists() else []


def _run(make_worker, product_yaml=None):
    worker = make_worker(sandbox_runtime=SYSBOX_RUNTIME, product_yaml=product_yaml)
    try:
        started = worker.start(probe_request({"end": "succeed"}))
        assert isinstance(started, Started)
        return worker.record(started.run_id), wait_until_ended(worker, started.run_id, timeout=30)
    finally:
        worker.shutdown()


def test_a_repo_with_no_recipe_runs_with_no_environment_on_the_default_runtime(make_worker, stand_in_docker):
    # The stand-in host cannot start any sandbox, so the run ends in an infra-failure: what matters
    # is that sysbox was neither required nor asked for, though the worker is set to use it.
    _run(make_worker)
    calls = stand_in_docker()
    assert not [c for c in calls if c[:1] == ["info"]], "the host was checked for sysbox for a run that needs none"
    (run_call,) = [c for c in calls if c[:1] == ["run"]]
    assert "--runtime" not in run_call and not any("WEAVE_START_DOCKERD" in a for a in run_call)


def test_a_repo_with_a_recipe_needs_sysbox_and_a_host_without_it_is_an_infra_failure_before_a_sandbox_starts(
    make_worker, stand_in_docker, tmp_path
):
    repo = repo_with_recipe(tmp_path / "recipe-repos")
    record, final = _run(make_worker, product_yaml_for({"app": repo}, PRODUCT))
    assert final.state is RunState.ENDED and final.outcome is Outcome.INFRA_FAILURE
    assert "sysbox-runc" in final.reason
    assert not [c for c in stand_in_docker() if c[:1] == ["run"]], "a sandbox was started"


def test_a_worker_with_no_sandbox_runtime_cannot_give_a_recipe_its_environment(make_worker, stand_in_docker, tmp_path):
    repo = repo_with_recipe(tmp_path / "recipe-repos")
    worker = make_worker(sandbox_runtime=None, product_yaml=product_yaml_for({"app": repo}, PRODUCT))
    try:
        started = worker.start(probe_request({"end": "succeed"}))
        final = wait_until_ended(worker, started.run_id, timeout=30)
    finally:
        worker.shutdown()
    assert final.outcome is Outcome.INFRA_FAILURE and "sandbox_runtime" in final.reason
    assert not [c for c in stand_in_docker() if c[:1] == ["run"]]
