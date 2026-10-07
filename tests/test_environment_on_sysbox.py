"""Seam A: a Repo's Run recipe becomes an Environment inside the run's sandbox (#49).

Each test is named after an acceptance criterion of #49. Every test here runs a real sandbox on
the sysbox runtime, pulls public images (alpine, python:3.12-slim) from the internet inside it,
and is SKIPPED on a host that does not list `sysbox-runc` in `docker info`. (The recipe, readiness
and log-saving logic is covered without Docker in test_run_recipe.py, test_environment_bring_up.py
and test_environment_record.py.)

The fixture Repo `svc` has a tiny HTTP service that only starts listening five seconds after its
container starts, so an agent that started too early would find nothing to reach.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import (
    PRODUCT, needs_sysbox, probe_reports, probe_request, product_yaml_for, repo_with_recipe, sandboxes_of,
    wait_until_ended,
)

from workflow_weave.agent_worker import Outcome, Started

pytestmark = needs_sysbox

SERVICE_RECIPE = """\
services:
  web:
    image: python:3.12-slim
    command:
      - sh
      - -c
      - "sleep 5 && echo hello-from-web > /tmp/index.html && exec python -m http.server 8000 -d /tmp"
x-weave:
  readiness:
    web:
      command: python -c "import urllib.request as u; u.urlopen('http://localhost:8000/index.html')"
      timeout: 120
"""

# A developer's own compose file: it would start `devonly` if anything used it.
DEVELOPER_COMPOSE = "services:\n  devonly:\n    image: alpine:3.20\n    command: sleep 300\n    container_name: devonly\n"


def _worker(make_worker, tmp_path, recipe=SERVICE_RECIPE, extra_files=None):
    repo = repo_with_recipe(tmp_path / "recipe-repos", "svc", recipe, extra_files)
    return make_worker(product_yaml=product_yaml_for({"svc": repo}, PRODUCT))


def _run(worker, script, timeout=600):
    started = worker.start(probe_request(script, repos=["svc"]))
    assert isinstance(started, Started)
    return started, wait_until_ended(worker, started.run_id, timeout=timeout)


def _report(worker, run_id):
    (report,) = probe_reports(worker.record(run_id).event_log)
    return report


def test_a_fixture_repos_recipe_brings_up_an_http_service_that_the_probe_reaches_before_it_starts(
    make_worker, tmp_path
):
    worker = _worker(make_worker, tmp_path)
    try:
        started, final = _run(worker, {"end": "succeed", "reach": [{"repo": "svc", "service": "web", "port": 8000, "path": "/index.html"}]})
        assert final.outcome is Outcome.SUCCEEDED, final.reason
        (reached,) = _report(worker, started.run_id)["environment"]["reach"]
    finally:
        worker.shutdown()
    # One attempt, no retry: the service answered because the agent started only once it was ready.
    assert reached.get("error") is None, reached
    assert reached["status"] == 200 and reached["body"].strip() == "hello-from-web"
    assert reached["alias_ok"], "the service is not reachable as web.svc"


def test_the_agent_starts_only_after_every_service_is_ready_so_the_record_shows_readiness_before_the_agent_ran(
    make_worker, tmp_path
):
    worker = _worker(make_worker, tmp_path)
    try:
        started, final = _run(worker, {"end": "succeed"})
        record = worker.record(started.run_id)
    finally:
        worker.shutdown()
    (web,) = record.environment_services
    assert final.outcome is Outcome.SUCCEEDED, final.reason
    assert web["seconds_to_ready"] >= 5, "reported ready before the service could have been listening"


def test_a_service_in_the_environment_can_reach_an_outside_host(make_worker, tmp_path):
    worker = _worker(make_worker, tmp_path)
    cmd = "python -c \"import urllib.request as u; print(u.urlopen('https://example.com', timeout=20).status)\""
    try:
        started, final = _run(worker, {"end": "succeed", "exec_in_service": [{"repo": "svc", "service": "web", "command": cmd}]})
        (result,) = _report(worker, started.run_id)["environment"]["exec"]
    finally:
        worker.shutdown()
    assert result["exit"] == 0 and result["output"].strip() == "200", result


def test_a_developers_own_compose_file_in_the_repo_is_never_used(make_worker, tmp_path):
    worker = _worker(make_worker, tmp_path, extra_files={"docker-compose.yml": DEVELOPER_COMPOSE})
    try:
        started, final = _run(worker, {"end": "succeed", "list_containers": True})
        containers = _report(worker, started.run_id)["environment"]["containers"]
    finally:
        worker.shutdown()
    assert final.outcome is Outcome.SUCCEEDED, final.reason
    assert containers and not any("devonly" in c for c in containers), containers
    assert any(c.startswith("svc-web") for c in containers)


def test_the_environment_is_gone_with_the_sandbox_and_a_second_run_sees_nothing_from_the_first(make_worker, tmp_path):
    worker = _worker(make_worker, tmp_path)
    leave = "touch /tmp/left-by-first-run"
    look = "test -e /tmp/left-by-first-run"
    try:
        first, first_final = _run(worker, {"end": "succeed", "exec_in_service": [{"repo": "svc", "service": "web", "command": leave}]})
        assert first_final.outcome is Outcome.SUCCEEDED, first_final.reason
        assert sandboxes_of(first.run_id) == [], "the sandbox, and with it the Environment, is still on the host"
        second, second_final = _run(worker, {"end": "succeed", "list_containers": True,
                                             "exec_in_service": [{"repo": "svc", "service": "web", "command": look}]})
        assert second_final.outcome is Outcome.SUCCEEDED, second_final.reason
        env = _report(worker, second.run_id)["environment"]
    finally:
        worker.shutdown()
    assert env["exec"][0]["exit"] != 0, "the second run found a file the first run left in its service"
    assert [c for c in env["containers"] if not c.startswith("svc-")] == []


def test_a_repo_with_no_recipe_runs_with_no_environment_and_no_error(make_worker):
    worker = make_worker()  # the default fixture Repo has no Run recipe
    try:
        started = worker.start(probe_request({"end": "succeed"}))
        final = wait_until_ended(worker, started.run_id, timeout=180)
        record = worker.record(started.run_id)
    finally:
        worker.shutdown()
    assert final.outcome is Outcome.SUCCEEDED, final.reason
    assert record.environment_services is None and record.environment_logs is None


def test_the_run_record_names_the_services_their_readiness_times_and_their_saved_logs_which_outlive_the_sandbox(
    make_worker, tmp_path
):
    worker = _worker(make_worker, tmp_path)
    try:
        started, final = _run(worker, {"end": "succeed"})
        record = worker.record(started.run_id)
        assert sandboxes_of(started.run_id) == []
    finally:
        worker.shutdown()
    (web,) = record.environment_services
    assert (web["repo"], web["service"], web["address"]) == ("svc", "web", "web.svc")
    assert web["ready_at"] and web["seconds_to_ready"] > 0
    log = Path(record.environment_logs["svc/web"])
    assert log.exists() and log.parent.parent.parent == Path(record.event_log).parent  # beside the event log
    assert "GET /index.html" in log.read_text()  # the readiness check's own request, served by the service


def test_a_service_that_never_passes_its_check_ends_the_run_before_the_agent_starts_with_its_logs_saved(
    make_worker, tmp_path
):
    recipe = SERVICE_RECIPE.replace("timeout: 120", "timeout: 12").replace("sleep 5", "sleep 600")
    worker = _worker(make_worker, tmp_path, recipe)
    try:
        started, final = _run(worker, {"end": "succeed"})
        record = worker.record(started.run_id)
    finally:
        worker.shutdown()
    assert final.outcome is Outcome.NEEDS_SETUP  # provisional: Spec 2 #51 splits this by retrying at Base
    assert "svc" in final.reason and "web" in final.reason and "not ready" in final.reason
    assert record.environment_services in (None, [])
    log = Path(record.event_log)
    assert not (log.exists() and probe_reports(log)), "an agent started"
    assert Path(record.environment_logs["svc/web"]).exists()


def test_a_recipe_that_cannot_be_read_ends_the_run_naming_the_repo_before_the_agent_starts(make_worker, tmp_path):
    worker = _worker(make_worker, tmp_path, "services:\n  web: {image: alpine:3.20}\n")  # no x-weave block
    try:
        started, final = _run(worker, {"end": "succeed"})
    finally:
        worker.shutdown()
    assert final.outcome is Outcome.NEEDS_SETUP
    assert "svc" in final.reason and "x-weave" in final.reason
