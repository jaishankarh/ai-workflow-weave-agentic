"""Seam A: several Repos run side by side in one Environment (#50).

Each test is named after an acceptance criterion of #50. Every test here runs a real sandbox on
the sysbox runtime, pulls python:3.12-slim from the internet inside it, and is SKIPPED on a host
that does not list `sysbox-runc` in `docker info`. (Dependency order, branch choice and the exact
Docker commands are covered without Docker in test_environment_several_repos.py.)

Every fixture Repo here has a service called `web`, so the same name is defined by several Repos.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from conftest import (
    PRODUCT, needs_sysbox, probe_reports, probe_request, product_yaml_for, repo_with_recipe, wait_until_ended,
)

from workflow_weave.agent_worker import Outcome, Started

pytestmark = needs_sysbox


def web_recipe(body: str, depends_on: list[str] | None = None) -> str:
    """A recipe whose `web` service answers `body` on port 8000 (read from the committed file `body.txt`)."""
    deps = f"  depends_on: [{', '.join(depends_on)}]\n" if depends_on else ""
    return f"""\
services:
  web:
    image: python:3.12-slim
    volumes: ["./..:/srv:ro"]
    command: ["python", "-m", "http.server", "8000", "-d", "/srv"]
x-weave:
{deps}  readiness:
    web:
      command: python -c "import urllib.request as u; u.urlopen('http://localhost:8000/body.txt')"
      timeout: 120
"""



def make(tmp_path: Path, name: str, body: str, depends_on: list[str] | None = None) -> Path:
    return repo_with_recipe(tmp_path / "repos", name, web_recipe(body, depends_on), {"body.txt": body})


def story_branch(source: Path, branch: str, body: str) -> None:
    """Commit `body` to `branch` of the clone, as the Story's work already merged there."""
    def git(*args):
        subprocess.run(["git", "-C", str(source), "-c", "user.email=t@weave.invalid", "-c", "user.name=t", *args],
                       check=True, capture_output=True)
    git("checkout", "-q", "-b", branch)
    (source / "body.txt").write_text(body)
    git("commit", "-qam", f"work on {branch}")
    git("checkout", "-q", "main")


def reach(repo: str) -> dict:
    return {"repo": repo, "service": "web", "port": 8000, "path": "/body.txt"}


def run(worker, script, repos, branch="story-1", timeout=900):
    started = worker.start(probe_request(script, repos=repos, branch=branch))
    assert isinstance(started, Started)
    final = wait_until_ended(worker, started.run_id, timeout=timeout)
    assert final.outcome is Outcome.SUCCEEDED, final.reason
    (report,) = probe_reports(worker.record(started.run_id).event_log)
    return worker.record(started.run_id), report["environment"]


def test_two_repos_that_each_define_a_service_of_the_same_name_do_not_collide_and_are_reached_as_service_dot_repo(
    make_worker, tmp_path
):
    one, two = make(tmp_path, "one", "from-one"), make(tmp_path, "two", "from-two")
    worker = make_worker(product_yaml=product_yaml_for({"one": one, "two": two}, PRODUCT))
    script = {
        "end": "succeed", "reach": [reach("one"), reach("two")], "resolve": ["web.one", "web.two"],
        # From inside one Repo's container, the other Repo's service by its address.
        "exec_in_service": [{"repo": "one", "service": "web", "command": "getent hosts web.two"}],
    }
    try:
        record, env = run(worker, script, ["one", "two"])
    finally:
        worker.shutdown()
    by_repo = {r["repo"]: r for r in env["reach"]}
    assert by_repo["one"]["body"].strip() == "from-one" and by_repo["two"]["body"].strip() == "from-two"
    assert not any(r["bare_alias"] for r in env["reach"]), "a bare `web` alias would clash across Repos"
    assert all(r["alias_ok"] for r in env["reach"])
    assert env["resolve"]["web.one"] != env["resolve"]["web.two"]
    assert env["exec"][0]["exit"] == 0 and env["resolve"]["web.two"] in env["exec"][0]["output"]
    assert {s["address"] for s in record.environment_services} == {"web.one", "web.two"}


def test_the_probe_reaches_a_repos_service_from_another_repo_with_the_same_address_in_every_run(make_worker, tmp_path):
    svc, client = make(tmp_path, "svc", "from-svc"), make(tmp_path, "client", "from-client", ["svc"])
    worker = make_worker(product_yaml=product_yaml_for({"svc": svc, "client": client}, PRODUCT))
    script = {
        "end": "succeed",
        "exec_in_service": [{"repo": "client", "service": "web", "command":
                             "python -c \"import urllib.request as u; print(u.urlopen('http://web.svc:8000/body.txt').read().decode())\""}],
    }
    try:
        outputs = []
        for _ in range(2):
            _, env = run(worker, script, ["client"])
            outputs.append(env["exec"][0])
    finally:
        worker.shutdown()
    assert all(o["exit"] == 0 and o["output"].strip() == "from-svc" for o in outputs), outputs


def test_a_repo_named_in_depends_on_is_brought_up_even_though_the_run_does_not_touch_it_at_its_base_branch(
    make_worker, tmp_path
):
    svc, client = make(tmp_path, "svc", "base-of-svc"), make(tmp_path, "client", "from-client", ["svc"])
    story_branch(svc, "story-1", "another-storys-unmerged-work")  # never part of this run's Environment
    worker = make_worker(product_yaml=product_yaml_for({"svc": svc, "client": client}, PRODUCT))
    try:
        record, env = run(worker, {"end": "succeed", "reach": [reach("svc")]}, ["client"])
    finally:
        worker.shutdown()
    assert env["reach"][0]["body"].strip() == "base-of-svc"
    sources = {e["repo"]: (e["source"], e["touched"]) for e in record.environment_repos}
    assert sources == {"svc": ("Base branch", False), "client": ("working copy", True)}


def test_the_repo_worked_on_runs_from_the_working_copy_another_touched_repo_from_its_task_branch_and_the_record_says_which(
    make_worker, tmp_path
):
    api, web = make(tmp_path, "api", "api-on-main"), make(tmp_path, "webapp", "webapp-on-main", ["api"])
    story_branch(api, "story-1", "api-on-task-branch")
    worker = make_worker(product_yaml=product_yaml_for({"api": api, "webapp": web}, PRODUCT))
    script = {"end": "succeed", "reach": [reach("api")]}
    try:
        record, env = run(worker, script, ["webapp", "api"])
    finally:
        worker.shutdown()
    assert env["reach"][0]["body"].strip() == "api-on-task-branch"
    entries = {e["repo"]: (e["source"], e["branch"]) for e in record.environment_repos}
    assert entries == {"api": ("Task branch", "story-1"), "webapp": ("working copy", "story-1")}


def test_a_recipe_with_no_services_of_its_own_and_only_dependencies_still_gives_an_environment(make_worker, tmp_path):
    svc = make(tmp_path, "svc", "from-svc")
    client = repo_with_recipe(tmp_path / "repos", "client", "x-weave:\n  depends_on: [svc]\n")
    worker = make_worker(product_yaml=product_yaml_for({"svc": svc, "client": client}, PRODUCT))
    try:
        record, env = run(worker, {"end": "succeed", "reach": [reach("svc")]}, ["client"])
    finally:
        worker.shutdown()
    assert env["reach"][0]["body"].strip() == "from-svc"
    assert [s["repo"] for s in record.environment_services] == ["svc"]


def test_two_runs_at_once_cannot_reach_each_others_services_and_do_not_clash_on_names(make_worker, tmp_path):
    svc = make(tmp_path, "svc", "from-svc")
    worker = make_worker(product_yaml=product_yaml_for({"svc": svc}, PRODUCT))
    script = {"end": "succeed", "reach": [reach("svc")], "list_containers": True}
    try:
        runs = [worker.start(probe_request(script, repos=["svc"])) for _ in range(2)]
        assert all(isinstance(r, Started) for r in runs)
        finals = [wait_until_ended(worker, r.run_id, timeout=900) for r in runs]
        reports = [probe_reports(worker.record(r.run_id).event_log)[0]["environment"] for r in runs]
    finally:
        worker.shutdown()
    assert all(f.outcome is Outcome.SUCCEEDED for f in finals), [f.reason for f in finals]
    # Each sandbox has its own engine: each sees only its own one `web` container, with the same name.
    assert all(len(r["containers"]) == 1 and r["reach"][0]["body"].strip() == "from-svc" for r in reports)
