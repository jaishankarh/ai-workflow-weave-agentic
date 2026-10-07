"""Seam A: the sandbox runs nested Docker on sysbox.

Each test is named after an acceptance criterion of #48.

Two kinds of test live here:
  * host-independent ones, which need no Docker daemon: a stand-in `docker` on PATH (or a fake
    passed in) plays the Sandbox host, and they always run;
  * `needs_sysbox` ones, which run a real sandbox on the sysbox runtime and are SKIPPED on a host
    that does not list `sysbox-runc` in `docker info` (the skip message says so).
"""

from __future__ import annotations

import json
import subprocess

import pytest
from conftest import needs_sysbox, probe_reports, probe_request, sandboxes_of, wait_for, wait_until_ended

from workflow_weave.agent_worker import AgentProfile, Outcome, RunState, Started
from workflow_weave.agent_worker.sandbox import (
    SYSBOX_RUNTIME,
    MissingRuntimeError,
    SandboxError,
    remove_sandbox,
    require_runtime,
    sandbox_run_command,
)


def _proc(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


# ----------------------------------------------------------------- a host without sysbox


def test_host_without_sysbox_is_reported_naming_the_missing_runtime():
    def docker(*args, timeout=120):
        assert args[:1] == ("info",)
        return _proc(stdout=json.dumps({"runc": {"path": "runc"}, "io.containerd.runc.v2": {"path": "runc"}}))

    with pytest.raises(MissingRuntimeError) as e:
        require_runtime(SYSBOX_RUNTIME, docker=docker)
    assert "sysbox-runc" in str(e.value)
    assert isinstance(e.value, SandboxError)  # reported as an infra-failure like other host faults


def test_host_with_sysbox_passes_the_check():
    def docker(*args, timeout=120):
        return _proc(stdout=json.dumps({"runc": {}, "sysbox-runc": {"path": "/usr/bin/sysbox-runc"}}))

    require_runtime(SYSBOX_RUNTIME, docker=docker)  # does not raise


def test_host_whose_docker_cannot_be_asked_is_reported_with_the_runtime_and_the_cause():
    def docker(*args, timeout=120):
        return _proc(returncode=1, stderr="Cannot connect to the Docker daemon")

    with pytest.raises(MissingRuntimeError) as e:
        require_runtime(SYSBOX_RUNTIME, docker=docker)
    assert "sysbox-runc" in str(e.value) and "Cannot connect" in str(e.value)


# ----------------------------------------------- runs that need no Environment are unaffected


def _command(**kw):
    args = dict(
        image="img", run_id="r1", product="p", api_key="k", port=1234, env={"A": "b"},
        nofile_limit=65536, mounts=[("/h", "/s")], extra_hosts=["weave-git:host-gateway"], runtime=None,
    )
    return sandbox_run_command(**{**args, **kw})


def test_runs_that_need_no_environment_are_unaffected():
    cmd = _command(runtime=None)
    assert "--runtime" not in cmd
    assert not any("WEAVE_START_DOCKERD" in c for c in cmd)


def test_sandbox_on_sysbox_is_not_privileged_and_has_no_host_docker_socket():
    cmd = _command(runtime=SYSBOX_RUNTIME)
    assert cmd[cmd.index("--runtime") + 1] == "sysbox-runc"
    assert "WEAVE_START_DOCKERD=1" in cmd  # the nested Docker engine is started inside the sandbox
    joined = " ".join(cmd)
    assert "--privileged" not in cmd
    assert "docker.sock" not in joined
    assert "--cap-add" not in joined and "--pid=host" not in joined and "--network=host" not in joined


# ------------------------------------------------------------------ teardown awaits removal


class FakeHost:
    """A Sandbox host whose `docker rm -f` returns at once but whose container lingers for a while."""

    def __init__(self, lingers_for: float):
        self.now = 0.0
        self.removed_at = None
        self.lingers_for = lingers_for
        self.inspects = 0
        self.log = []

    def docker(self, *args, timeout=120):
        self.log.append(args[0])
        if args[0] == "rm":
            self.removed_at = self.now
            return _proc()
        if args[0] == "inspect":
            self.inspects += 1
            if self.now - self.removed_at < self.lingers_for:
                return _proc(stdout="[{}]")
            return _proc(returncode=1, stderr="Error: No such object: abc")
        raise AssertionError(args)

    def sleep(self, s):
        self.now += s

    def clock(self):
        return self.now


def _remove(host, timeout=30):
    remove_sandbox("abc", timeout=timeout, interval=1.0, docker=host.docker, sleep=host.sleep, clock=host.clock)


def test_teardown_is_reported_only_after_the_sandbox_is_gone_when_removal_is_immediate():
    host = FakeHost(lingers_for=0)
    _remove(host)
    assert host.log == ["rm", "inspect"]  # even then it is checked, not assumed


def test_teardown_is_reported_only_after_the_sandbox_is_gone_including_when_removal_is_slow():
    host = FakeHost(lingers_for=7.5)
    _remove(host)
    assert host.now >= 7.5, "returned while the sandbox was still on the Sandbox host"
    assert host.inspects >= 8


def test_teardown_that_never_sees_the_sandbox_go_fails_instead_of_reporting_done():
    host = FakeHost(lingers_for=10_000)
    with pytest.raises(SandboxError) as e:
        _remove(host, timeout=5)
    assert "abc" in str(e.value) and "still" in str(e.value)


def test_a_docker_that_cannot_answer_is_not_taken_for_a_removed_sandbox():
    def docker(*args, timeout=120):
        return _proc() if args[0] == "rm" else _proc(returncode=1, stderr="Cannot connect to the Docker daemon")

    host = FakeHost(0)
    with pytest.raises(SandboxError):
        remove_sandbox("abc", timeout=3, interval=1.0, docker=docker, sleep=host.sleep, clock=host.clock)


# ----------------------------------------------------- on a real host with sysbox (else skipped)


@needs_sysbox
def test_the_probe_can_start_and_stop_a_container_inside_its_sandbox_and_report_the_result(make_worker):
    worker = make_worker(sandbox_runtime=SYSBOX_RUNTIME)
    try:
        started = worker.start(probe_request({"end": "succeed", "run_container": {"image": "alpine:3.20"}}))
        final = wait_until_ended(worker, started.run_id, timeout=300)
        assert final.outcome is Outcome.SUCCEEDED, final.reason
        (report,) = probe_reports(worker.record(started.run_id).event_log)
    finally:
        worker.shutdown()
    result = report["container"]
    assert result["started"] and result["stopped"], result
    assert result["output"].strip() == "hello from inside the sandbox"


@needs_sysbox
def test_no_host_docker_socket_is_present_in_the_sandbox_and_the_sandbox_is_not_privileged(make_worker):
    worker = make_worker(sandbox_runtime=SYSBOX_RUNTIME)
    try:
        started = worker.start(probe_request({"end": "succeed", "run_container": {"image": "alpine:3.20"}}))
        final = wait_until_ended(worker, started.run_id, timeout=300)
        assert final.outcome is Outcome.SUCCEEDED, final.reason
        (report,) = probe_reports(worker.record(started.run_id).event_log)
    finally:
        worker.shutdown()
    # Seen from inside: no bind mount of a docker.sock (the nested engine's own socket is not a mount).
    assert report["docker_socket_mounts"] == []


@needs_sysbox
def test_the_sandbox_is_not_privileged_and_runs_on_sysbox(make_worker):
    worker = make_worker(sandbox_runtime=SYSBOX_RUNTIME)
    try:
        started = worker.start(probe_request({"end": "hang"}))
        wait_for(lambda: sandboxes_of(started.run_id), 60, 0.5, "the sandbox to exist")
        (cid,) = sandboxes_of(started.run_id)
        out = subprocess.run(["docker", "inspect", cid], capture_output=True, text=True, check=True).stdout
        inspect = json.loads(out)[0]
        worker.cancel(started.run_id)
    finally:
        worker.shutdown()
    assert inspect["HostConfig"]["Privileged"] is False
    assert inspect["HostConfig"]["Runtime"] == "sysbox-runc"
    assert not [m for m in inspect["Mounts"] if "docker.sock" in m["Source"] + m["Destination"]]


@needs_sysbox
def test_teardown_leaves_no_sandbox_on_the_sandbox_host_when_it_is_reported(make_worker):
    worker = make_worker(sandbox_runtime=SYSBOX_RUNTIME)
    try:
        script = {"end": "succeed", "run_container": {"image": "alpine:3.20", "leave_running": True}}
        started = worker.start(probe_request(script))
        wait_until_ended(worker, started.run_id, timeout=300)
    finally:
        worker.shutdown()
    assert sandboxes_of(started.run_id) == []  # a container left running inside goes with its sandbox


@needs_sysbox
def test_runs_that_need_no_environment_are_unaffected_on_a_sysbox_host(make_worker):
    worker = make_worker()  # no sandbox_runtime
    try:
        started = worker.start(probe_request({"end": "succeed"}))
        final = wait_until_ended(worker, started.run_id, timeout=180)
    finally:
        worker.shutdown()
    assert final.outcome is Outcome.SUCCEEDED
