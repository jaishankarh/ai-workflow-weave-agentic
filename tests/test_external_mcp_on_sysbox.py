"""Seam A: External MCP servers on a real sysbox sandbox (#56).

Each test is named after an acceptance criterion of #56. They SKIP on a host that does not list
`sysbox-runc` in `docker info`. The probe stands in for the agent: it reads the managed MCP
configuration, starts each server as Claude Code would, and calls its tools (the probe's `mcp`
report). The same logic without Docker, and the check against the real Claude Code CLI, is in
test_external_mcp_servers.py.

The External MCP server here is a tiny stdio server committed in the fixture Repo and run with the
sandbox image's own Python, so no outside service is needed.

NOT YET RUN: the workspace these were written in has no Docker daemon, let alone sysbox.
"""

from __future__ import annotations

import json

import pytest
from conftest import (
    PRODUCT, needs_sysbox, probe_reports, probe_request, product_yaml_for, repo_with_recipe, sandboxes_of,
    wait_until_ended,
)
from test_test_secrets import write_secrets

from workflow_weave.agent_worker import Outcome, Started
from workflow_weave.agent_worker.secret_store import SecretStore

PAY_KEY = "pay-test-key-3d9f61aa"

FAKE_SERVER = '''\
import os, pathlib
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("fake-external")

@mcp.tool()
def environment_names() -> str:
    return ",".join(sorted(os.environ))

@mcp.tool()
def secret(name: str) -> str:
    return os.environ.get(name, "<unset>")

@mcp.tool()
def touch(name: str) -> str:
    pathlib.Path("/tmp", name).write_text("here")
    return "touched"

@mcp.tool()
def listing() -> str:
    return ",".join(sorted(p.name for p in pathlib.Path("/tmp").glob("ext-*")))

mcp.run()
'''

RECIPE = """\
x-weave:
  external_mcp:
    - name: pay
      command: /opt/oh/bin/python
      args: [/workspace/svc/.weave/fake_external_mcp.py]
      secrets: [PAY_TEST_KEY]
"""

FILES = {".weave/fake_external_mcp.py": FAKE_SERVER}


def worker_for(make_worker, tmp_path, extra_files=None, secrets=None):
    repo = repo_with_recipe(tmp_path / "recipe-repos", "svc", RECIPE, {**FILES, **(extra_files or {})})
    worker = make_worker(product_yaml=product_yaml_for({"svc": repo}))
    store = SecretStore(
        write_secrets(tmp_path / "s", PRODUCT, secrets or {"PAY_TEST_KEY": PAY_KEY, "UNNAMED": "never-given"}).parent
    )
    worker.settings = worker.settings.__class__(**{**vars(worker.settings), "test_secrets": store})
    return worker


def run(worker, script, timeout=900):
    started = worker.start(probe_request(script, repos=["svc"]))
    assert isinstance(started, Started)
    return started, wait_until_ended(worker, started.run_id, timeout=timeout)


def mcp_report(worker, run_id):
    (report,) = probe_reports(worker.record(run_id).event_log)
    return report["mcp"]


def call(tool, **arguments):
    return {"server": "svc-pay", "tool": tool, "arguments": arguments}


@needs_sysbox
def test_a_declared_external_server_is_started_for_the_run_and_the_probe_can_use_it(make_worker, tmp_path):
    worker = worker_for(make_worker, tmp_path)
    try:
        started, final = run(worker, {"end": "succeed", "mcp": {"tools": True, "calls": [call("listing")]}})
        report, record = mcp_report(worker, started.run_id), worker.record(started.run_id)
    finally:
        worker.shutdown()
    assert final.outcome is Outcome.SUCCEEDED, final.reason
    assert "touch" in report["tools"]["svc-pay"]
    assert report["calls"][0]["ok"], report["calls"]
    assert [s["name"] for s in record.external_mcp_servers] == ["svc-pay"]
    assert sandboxes_of(started.run_id) == [], "the server is gone with the sandbox"


@needs_sysbox
def test_the_external_server_receives_only_the_test_secrets_its_recipe_names(make_worker, tmp_path, monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "ghp_host_forge_token_0123456789")  # the worker's own Code host token
    worker = worker_for(make_worker, tmp_path)
    try:
        started, final = run(worker, {"end": "succeed", "mcp": {"calls": [
            call("environment_names"), call("secret", name="PAY_TEST_KEY"), call("secret", name="UNNAMED"),
        ]}})
        report = mcp_report(worker, started.run_id)
    finally:
        worker.shutdown()
    assert final.outcome is Outcome.SUCCEEDED, final.reason
    names, named, unnamed = report["calls"]
    seen = set(names["text"].split(","))
    # The probe's own environment has the Subscription's PROBE_TOKEN; the server's has only its secret
    # (and the PATH and HOME it needs; Python adds LC_CTYPE in a C locale).
    assert "PAY_TEST_KEY" in seen and not seen & {"PROBE_TOKEN", "GH_TOKEN", "GITHUB_TOKEN", "UNNAMED"}, seen
    assert seen <= {"PATH", "HOME", "PAY_TEST_KEY", "LC_CTYPE"}, seen
    assert named["text"] == PAY_KEY and unnamed["text"] == "<unset>"


@needs_sysbox
def test_no_tracker_or_code_host_credential_is_in_an_external_servers_configuration_or_the_record(
    make_worker, tmp_path, monkeypatch
):
    monkeypatch.setenv("GH_TOKEN", "ghp_host_forge_token_0123456789")
    worker = worker_for(make_worker, tmp_path)
    try:
        started, final = run(worker, {"end": "succeed", "mcp": {}})
        report = mcp_report(worker, started.run_id)
        record = worker.record(started.run_id)
        written = "".join(p.read_text() for p in worker._run_dir(started.run_id).rglob("*") if p.is_file())
    finally:
        worker.shutdown()
    assert final.outcome is Outcome.SUCCEEDED, final.reason
    assert report["configured"]["svc-pay"]["env_names"] == ["PAY_TEST_KEY"]
    assert "weave-git" not in json.dumps(report["configured"]["svc-pay"])
    assert "ghp_host_forge_token" not in written and PAY_KEY not in written
    assert PAY_KEY not in record.to_json()


@needs_sysbox
def test_each_run_gets_its_own_copy_and_one_runs_server_is_not_reachable_from_another(make_worker, tmp_path):
    worker = worker_for(make_worker, tmp_path)
    ids = {}
    try:
        for marker in ("ext-a", "ext-b"):
            started = worker.start(probe_request(
                {"end": "succeed", "mcp": {"calls": [call("touch", name=marker), call("listing")]}}, repos=["svc"]
            ))
            assert isinstance(started, Started)
            ids[marker] = started.run_id
        finals = {m: wait_until_ended(worker, rid, timeout=900) for m, rid in ids.items()}
        reports = {m: mcp_report(worker, rid) for m, rid in ids.items()}
    finally:
        worker.shutdown()
    for marker, other in (("ext-a", "ext-b"), ("ext-b", "ext-a")):
        assert finals[marker].outcome is Outcome.SUCCEEDED, finals[marker].reason
        listing = reports[marker]["calls"][1]["text"]
        assert marker in listing and other not in listing, f"{marker}'s server saw {other}: {listing}"


@needs_sysbox
def test_a_repos_committed_mcp_config_is_not_used_and_the_runs_log_says_it_was_ignored(make_worker, tmp_path):
    own = json.dumps({"mcpServers": {"repo-evil": {"type": "stdio", "command": "/opt/oh/bin/python",
                                                  "args": ["/workspace/svc/.weave/fake_external_mcp.py"]}}})
    worker = worker_for(make_worker, tmp_path, extra_files={".mcp.json": own})
    try:
        started, final = run(worker, {"end": "succeed", "mcp": {}})
        report, record = mcp_report(worker, started.run_id), worker.record(started.run_id)
        log = (worker._run_dir(started.run_id) / "run.log").read_text()
    finally:
        worker.shutdown()
    assert final.outcome is Outcome.SUCCEEDED, final.reason
    assert list(report["configured"]) == ["svc-pay"], "the Repo's own server was given to the agent"
    assert record.repo_mcp_config_ignored == ["svc"]
    assert "Repo 'svc' commits an MCP config (.mcp.json) that this run ignores" in log


@needs_sysbox
def test_a_recipe_naming_a_test_secret_the_product_lacks_stops_the_run_as_needs_setup(make_worker, tmp_path):
    from workflow_weave.agent_worker import NeedsSetup

    worker = worker_for(make_worker, tmp_path, secrets={"UNNAMED": "x"})
    try:
        result = worker.start(probe_request({"end": "succeed"}, repos=["svc"]))
    finally:
        worker.shutdown()
    assert isinstance(result, NeedsSetup) and "PAY_TEST_KEY" in result.reason
