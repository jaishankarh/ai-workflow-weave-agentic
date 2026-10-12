"""Seam A: seeding in dependency order (#53).

Each test is named after an acceptance criterion of #53. Every test here runs a real sandbox on the
sysbox runtime, pulls python:3.12-slim from the internet inside it, and is SKIPPED on a host that
does not list `sysbox-runc` in `docker info`. (The generated commands, their order and the record
are covered without Docker in test_environment_seeding.py.)

Each fixture Repo's `web` service serves its `/data` volume over HTTP, the stand-in for a database
the probe can read through `reach` and `exec_in_service`; the seed command writes a file into it.
"""

from __future__ import annotations

import json
from pathlib import Path

from conftest import (
    PRODUCT, needs_sysbox, probe_reports, probe_request, product_yaml_for, repo_with_recipe, wait_until_ended,
)

from workflow_weave.agent_worker import Outcome, Started

pytestmark = needs_sysbox


def recipe(seed: str | None, depends_on: list[str] | None = None) -> str:
    deps = f"  depends_on: [{', '.join(depends_on)}]\n" if depends_on else ""
    seed_block = f"  seed:\n    service: web\n    command: {json.dumps(seed)}\n" if seed else ""
    return f"""\
services:
  web:
    image: python:3.12-slim
    volumes: ["data:/data"]
    command: ["python", "-m", "http.server", "8000", "-d", "/data"]
volumes:
  data: {{}}
x-weave:
{deps}{seed_block}  readiness:
    web:
      command: python -c "import urllib.request as u; u.urlopen('http://localhost:8000/')"
      timeout: 120
"""


def make(tmp_path: Path, name: str, seed: str | None, depends_on: list[str] | None = None) -> Path:
    return repo_with_recipe(tmp_path / "repos", name, recipe(seed, depends_on))


def run(worker, script, repos, timeout=900):
    started = worker.start(probe_request(script, repos=repos))
    assert isinstance(started, Started)
    final = wait_until_ended(worker, started.run_id, timeout=timeout)
    return final, worker.record(started.run_id)


def report(worker, record) -> dict:
    (probe,) = probe_reports(record.event_log)
    return probe["environment"]


def read(repo: str, path: str) -> dict:
    return {"repo": repo, "service": "web", "port": 8000, "path": path}


def test_after_bring_up_the_probe_finds_a_repos_seeded_data_in_its_database(make_worker, tmp_path):
    svc = make(tmp_path, "svc", "echo seeded-by-svc > /data/seeded.txt")
    worker = make_worker(product_yaml=product_yaml_for({"svc": svc}, PRODUCT))
    try:
        final, record = run(worker, {"end": "succeed", "reach": [read("svc", "/seeded.txt")]}, ["svc"])
        env = report(worker, record)
    finally:
        worker.shutdown()
    assert final.outcome is Outcome.SUCCEEDED, final.reason
    assert env["reach"][0]["body"].strip() == "seeded-by-svc"
    assert [(s["repo"], s["service"]) for s in record.environment_seeds] == [("svc", "web")]


def test_a_repo_that_depends_on_another_is_seeded_after_it_and_its_seed_command_sees_the_dependencys_data(
    make_worker, tmp_path
):
    fetch = ("python -c \"import urllib.request as u; "
             "open('/data/saw.txt','w').write(u.urlopen('http://web.svc:8000/seeded.txt').read().decode())\"")
    svc = make(tmp_path, "svc", "echo seeded-by-svc > /data/seeded.txt")
    client = make(tmp_path, "client", fetch, ["svc"])
    worker = make_worker(product_yaml=product_yaml_for({"svc": svc, "client": client}, PRODUCT))
    try:
        final, record = run(worker, {"end": "succeed", "reach": [read("client", "/saw.txt")]}, ["client"])
        env = report(worker, record)
    finally:
        worker.shutdown()
    assert final.outcome is Outcome.SUCCEEDED, final.reason
    assert env["reach"][0]["body"].strip() == "seeded-by-svc"
    assert [s["repo"] for s in record.environment_seeds] == ["svc", "client"]


def test_a_repos_seed_command_cannot_change_another_repos_databases(make_worker, tmp_path):
    # Its own volume is all its seed command is given: `svc`'s data lives in another project's
    # volume and container, and the command holds no credential for it.
    svc = make(tmp_path, "svc", "echo svc-data > /data/seeded.txt")
    client = make(tmp_path, "client", "ls /data > /data/listing.txt", ["svc"])
    worker = make_worker(product_yaml=product_yaml_for({"svc": svc, "client": client}, PRODUCT))
    script = {"end": "succeed", "reach": [read("client", "/listing.txt")]}
    try:
        final, record = run(worker, script, ["client"])
        env = report(worker, record)
    finally:
        worker.shutdown()
    assert final.outcome is Outcome.SUCCEEDED, final.reason
    assert "seeded.txt" not in env["reach"][0]["body"]


def test_a_second_run_starts_from_empty_databases_and_sees_nothing_from_the_first(make_worker, tmp_path):
    svc = make(tmp_path, "svc", None)
    worker = make_worker(product_yaml=product_yaml_for({"svc": svc}, PRODUCT))
    mark = {"repo": "svc", "service": "web", "command": "echo left-by-first-run > /data/marker.txt"}
    look = {"repo": "svc", "service": "web", "command": "ls /data"}
    try:
        _, first = run(worker, {"end": "succeed", "exec_in_service": [mark]}, ["svc"])
        _, second = run(worker, {"end": "succeed", "exec_in_service": [look]}, ["svc"])
        listing = report(worker, second)["exec"][0]
    finally:
        worker.shutdown()
    assert listing["exit"] == 0 and "marker.txt" not in listing["output"]


def test_a_repo_with_no_seed_command_is_brought_up_without_error(make_worker, tmp_path):
    svc = make(tmp_path, "svc", None)
    worker = make_worker(product_yaml=product_yaml_for({"svc": svc}, PRODUCT))
    try:
        final, record = run(worker, {"end": "succeed"}, ["svc"])
    finally:
        worker.shutdown()
    assert final.outcome is Outcome.SUCCEEDED, final.reason
    assert not record.environment_seeds
