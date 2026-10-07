"""Seam A: a host without sysbox is an infra-failure, found before any sandbox starts (#48).

Needs no Docker daemon and no images: a stand-in `docker` on PATH plays a Sandbox host that has
only the default runtime, so this always runs. (The rest of #48's tests are in
test_sandbox_on_sysbox.py.)
"""

from __future__ import annotations

import json
import os
import stat
import sys

import pytest
from conftest import probe_request, wait_until_ended

from workflow_weave.agent_worker import AgentProfile, Outcome, RunState, Started
from workflow_weave.agent_worker.sandbox import SYSBOX_RUNTIME


@pytest.fixture
def probe_profile():
    """Replaces the image-building fixture: no sandbox is ever started here."""
    return AgentProfile(name="probe", image="never-started:test", acp_command=["true"])


@pytest.fixture
def stand_in_docker(tmp_path, monkeypatch):
    """A `docker` on PATH that is a Sandbox host without sysbox; records every call."""
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
        "sys.stderr.write('stand-in docker: not supported\\n')\n"
        "sys.exit(1)\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    return lambda: [json.loads(line) for line in calls.read_text().splitlines()] if calls.exists() else []


def test_host_without_sysbox_gives_infra_failure_naming_the_runtime_before_a_sandbox_is_started(
    make_worker, stand_in_docker
):
    worker = make_worker(sandbox_runtime=SYSBOX_RUNTIME)
    try:
        started = worker.start(probe_request({"end": "succeed"}))
        assert isinstance(started, Started)
        final = wait_until_ended(worker, started.run_id, timeout=30)
    finally:
        worker.shutdown()
    assert final.state is RunState.ENDED and final.outcome is Outcome.INFRA_FAILURE
    assert "sysbox-runc" in final.reason
    assert not [c for c in stand_in_docker() if c[:1] == ["run"]], "a sandbox was started"
    assert worker.record(started.run_id).outcome is Outcome.INFRA_FAILURE
