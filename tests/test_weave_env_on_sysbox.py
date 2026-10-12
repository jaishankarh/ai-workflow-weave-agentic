"""Seam A: the agent's `weave-env` command (#54).

Each test is named after an acceptance criterion of #54. Every test here runs a real sandbox on the
sysbox runtime, pulls python:3.12-slim from the internet inside it, and is SKIPPED on a host that
does not list `sysbox-runc` in `docker info`. (The command's own logic is covered without Docker in
test_weave_env.py and test_weave_env_rebuild_reset.py.) The probe runs `weave-env` itself, from
inside its sandbox (`weave_env` in its script), and reports each command's exit status and output.

Each fixture Repo's `web` service is built from the Repo (a Dockerfile copying `index.txt`) and
serves `/srv`; `/srv/data` is a volume, the stand-in for a database, written by the seed command.
"""

from __future__ import annotations

import json
from pathlib import Path

from conftest import (
    PRODUCT, needs_sysbox, probe_reports, probe_request, product_yaml_for, repo_with_recipe, wait_until_ended,
)

from workflow_weave.agent_worker import Outcome, Started

pytestmark = needs_sysbox

DOCKERFILE = """\
FROM python:3.12-slim
COPY index.txt /srv/index.txt
CMD ["python", "-m", "http.server", "8000", "-d", "/srv"]
"""


def recipe(seed: str | None = None, depends_on: list[str] | None = None) -> str:
    deps = f"  depends_on: [{', '.join(depends_on)}]\n" if depends_on else ""
    seed_block = f"  seed:\n    service: web\n    command: {json.dumps(seed)}\n" if seed else ""
    return f"""\
services:
  web:
    build: ..
    volumes: ["data:/srv/data"]
volumes:
  data: {{}}
x-weave:
{deps}{seed_block}  readiness:
    web:
      command: python -c "import urllib.request as u; u.urlopen('http://localhost:8000/index.txt')"
      timeout: 120
"""


def make(tmp_path: Path, name: str, seed: str | None = None, depends_on: list[str] | None = None) -> Path:
    return repo_with_recipe(
        tmp_path / "repos", name, recipe(seed, depends_on),
        extra_files={"Dockerfile": DOCKERFILE, "index.txt": "v1\n"},
    )


def run(worker, script, repos, timeout=1200):
    started = worker.start(probe_request(script, repos=repos))
    assert isinstance(started, Started)
    final = wait_until_ended(worker, started.run_id, timeout=timeout)
    return final, worker.record(started.run_id)


def reports(record) -> dict:
    (probe,) = probe_reports(record.event_log)
    return probe


def read(repo: str, path: str) -> dict:
    return {"repo": repo, "service": "web", "port": 8000, "path": path}


def commands(probe) -> list[dict]:
    return probe["weave_env"]


def test_status_lists_every_service_and_whether_it_is_ready(make_worker, tmp_path):
    svc, client = make(tmp_path, "svc"), make(tmp_path, "client", depends_on=["svc"])
    worker = make_worker(product_yaml=product_yaml_for({"svc": svc, "client": client}, PRODUCT))
    try:
        final, record = run(worker, {"end": "succeed", "weave_env": [["status"]]}, ["client"])
        (status,) = commands(reports(record))
    finally:
        worker.shutdown()
    assert final.outcome is Outcome.SUCCEEDED, final.reason
    assert status["exit"] == 0, status
    for address in ("web.svc", "web.client"):
        line = next(line for line in status["output"].splitlines() if address in line)
        assert "running" in line and "not ready" not in line


def test_logs_returns_a_named_services_recent_output_and_a_clear_error_for_an_unknown_repo_or_service(
    make_worker, tmp_path
):
    svc = make(tmp_path, "svc")
    worker = make_worker(product_yaml=product_yaml_for({"svc": svc}, PRODUCT))
    script = {"end": "succeed", "weave_env": [["logs", "svc", "web"], ["logs", "ghost"], ["logs", "svc", "ghost"]]}
    try:
        final, record = run(worker, script, ["svc"])
        ok, no_repo, no_service = commands(reports(record))
    finally:
        worker.shutdown()
    assert final.outcome is Outcome.SUCCEEDED, final.reason
    assert ok["exit"] == 0 and ok["output"].strip()
    assert no_repo["exit"] != 0 and "ghost" in no_repo["output"] and "svc" in no_repo["output"]
    assert no_service["exit"] != 0 and "ghost" in no_service["output"] and "web" in no_service["output"]


def test_after_the_probe_edits_a_repos_source_rebuild_makes_the_change_visible_while_the_data_is_unchanged(
    make_worker, tmp_path
):
    svc = make(tmp_path, "svc", seed="echo seeded > /srv/data/seeded.txt")
    worker = make_worker(product_yaml=product_yaml_for({"svc": svc}, PRODUCT))
    script = {
        "end": "succeed",
        "edit": {"index.txt": "v2\n"},
        # Data the probe adds itself, which only a kept volume still has after the rebuild.
        "weave_env": [{"exec": {"repo": "svc", "service": "web", "command": "echo mine > /srv/data/mine.txt"}},
                      ["rebuild", "svc"]],
        "reach": [read("svc", "/index.txt"), read("svc", "/data/seeded.txt"), read("svc", "/data/mine.txt")],
    }
    try:
        final, record = run(worker, script, ["svc"])
        probe = reports(record)
    finally:
        worker.shutdown()
    assert final.outcome is Outcome.SUCCEEDED, final.reason
    _, rebuild = commands(probe)
    assert rebuild["exit"] == 0, rebuild
    index, seeded, mine = probe["environment"]["reach"]
    assert index["body"].strip() == "v2"
    assert seeded["body"].strip() == "seeded" and mine["body"].strip() == "mine"


def test_reset_returns_the_environment_to_freshly_seeded_discarding_changes_the_probe_made_to_the_data(
    make_worker, tmp_path
):
    svc = make(tmp_path, "svc", seed="echo seeded > /srv/data/seeded.txt")
    fetch = ("python -c \"import urllib.request as u; "
             "open('/srv/data/saw.txt','w').write(u.urlopen('http://web.svc:8000/data/seeded.txt').read().decode())\"")
    client = make(tmp_path, "client", seed=fetch, depends_on=["svc"])
    worker = make_worker(product_yaml=product_yaml_for({"svc": svc, "client": client}, PRODUCT))
    script = {
        "end": "succeed",
        "weave_env": [
            {"exec": {"repo": "svc", "service": "web",
                      "command": "rm /srv/data/seeded.txt; echo mine > /srv/data/mine.txt"}},
            ["reset"],
        ],
        "reach": [read("svc", "/data/seeded.txt"), read("svc", "/data/mine.txt"), read("client", "/data/saw.txt")],
    }
    try:
        final, record = run(worker, script, ["client"])
        probe = reports(record)
    finally:
        worker.shutdown()
    assert final.outcome is Outcome.SUCCEEDED, final.reason
    _, reset = commands(probe)
    assert reset["exit"] == 0, reset
    seeded, mine, saw = probe["environment"]["reach"]
    assert seeded["body"].strip() == "seeded"  # seeded again
    assert "error" in mine or mine.get("status") != 200  # the probe's own change is gone
    assert saw["body"].strip() == "seeded"  # and `client` was seeded after `svc`, so it saw svc's data


def test_a_run_on_a_repo_with_no_environment_gets_a_clear_message_from_the_command_rather_than_a_crash(
    make_worker, onboarded_repo
):
    worker = make_worker(product_yaml=product_yaml_for({"app": onboarded_repo}, PRODUCT))
    try:
        final, record = run(worker, {"end": "succeed", "weave_env": [["status"], ["reset"]]}, ["app"])
        status, reset = commands(reports(record))
    finally:
        worker.shutdown()
    assert final.outcome is Outcome.SUCCEEDED, final.reason
    for result in (status, reset):
        assert result["exit"] != 0 and "no Environment" in result["output"] and "Traceback" not in result["output"]
