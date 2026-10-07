"""Seam A: Environment MCP servers on a real sysbox sandbox (#55).

Each test is named after an acceptance criterion of #55. Every run here is a real sandbox on the
sysbox runtime that pulls public images (postgres:16-alpine, neo4j:5, redis:7-alpine, alpine) from
the internet inside it, so these tests SKIP on a host that does not list `sysbox-runc` in `docker info`
(the first test needs only a Docker daemon). The probe stands in for the agent: it reads the agent's
managed MCP configuration (`/etc/claude-code/managed-mcp.json`), starts each MCP server as Claude Code would, and calls its tools (the
probe's `mcp` report). The same logic without Docker is in test_environment_mcp_servers.py.

NOT YET RUN: the workspace these were written in has no Docker daemon, let alone sysbox.
"""

from __future__ import annotations

import subprocess

import pytest
from conftest import (
    needs_sysbox, probe_reports, probe_request, product_yaml_for, repo_with_recipe, sandboxes_of, wait_until_ended,
)

from workflow_weave.agent_worker import Outcome, Started
from workflow_weave.agent_worker.mcp import CATALOG

POSTGRES_RECIPE = """\
services:
  db:
    image: postgres:16-alpine
    environment: {POSTGRES_USER: app, POSTGRES_PASSWORD: app-pw, POSTGRES_DB: chat}
  other:
    image: postgres:16-alpine
    environment: {POSTGRES_USER: app, POSTGRES_PASSWORD: app-pw, POSTGRES_DB: unnamed}
  graph:
    image: alpine:3.20
    command: sleep 3600
  cache:
    image: redis:7-alpine
x-weave:
  readiness:
    db: {command: pg_isready -U app -d chat, timeout: 120}
    other: {command: pg_isready -U app -d unnamed, timeout: 120}
    graph: {command: "true"}
    cache: {command: redis-cli ping, timeout: 60}
  seed:
    service: db
    command: psql -U app -d chat -c "create table notes (v text); insert into notes values ('seeded-row')"
  databases:
    - service: db
      kind: postgres
      credentials:
        user: {env: POSTGRES_USER}
        password: {env: POSTGRES_PASSWORD}
        database: {env: POSTGRES_DB}
    - service: graph
      kind: neo4j            # named, but the Product below does not enable neo4j
      credentials: {user: neo4j, password: x}
    - service: cache
      kind: redis
"""

NEO4J_RECIPE = """\
services:
  graph:
    image: neo4j:5
    environment: {NEO4J_AUTH: neo4j/graph-pw}
x-weave:
  readiness:
    graph: {command: cypher-shell -u neo4j -p graph-pw "RETURN 1", timeout: 240, interval: 3}
  seed:
    service: graph
    command: cypher-shell -u neo4j -p graph-pw "CREATE (:Note {v: 'seeded-node'})"
  databases:
    - service: graph
      kind: neo4j
      credentials: {user: neo4j, password: graph-pw}
"""

OWN_SKILL = {
    ".claude/skills/repo-own-skill/SKILL.md": "---\nname: repo-own-skill\ndescription: The Repo's own skill\n---\nUse me.\n"
}


def worker_for(make_worker, tmp_path, recipe=POSTGRES_RECIPE, kinds=("postgres",), extra_files=None):
    repo = repo_with_recipe(tmp_path / "recipe-repos", "svc", recipe, extra_files)
    return make_worker(product_yaml=product_yaml_for({"svc": repo}, database_mcp_kinds=kinds))


def run(worker, script, timeout=900):
    started = worker.start(probe_request(script, repos=["svc"]))
    assert isinstance(started, Started)
    return started, wait_until_ended(worker, started.run_id, timeout=timeout)


def mcp_report(worker, run_id):
    (report,) = probe_reports(worker.record(run_id).event_log)
    return report["mcp"]


def query(sql, server="svc-db"):
    return {"server": server, "tool": "execute_sql", "arguments": {"sql": sql}}


# ---- the pinned servers run in the sandbox image (Spec 2's "verify early")

def test_the_pinned_servers_run_in_the_sandbox_image_each_in_its_own_virtualenv(probe_image):
    for kind, module in (("postgres", "postgres_mcp"), ("neo4j", "mcp_neo4j_cypher"), ("mysql", "mysql_mcp_server")):
        out = subprocess.run(
            ["docker", "run", "--rm", "--entrypoint", f"/opt/weave-mcp/{kind}/bin/python", probe_image, "-c",
             f"import importlib.metadata as m, {module}; print(m.version({CATALOG[kind].package!r}))"],
            capture_output=True, text=True, timeout=120,
        )
        assert out.returncode == 0, out.stderr
        assert out.stdout.strip() == CATALOG[kind].version


def test_claude_code_lists_a_server_staged_in_the_managed_configuration_as_connected(claude_code_image):
    """Claude Code itself (not the probe) reads managed-mcp.json `mcpServers` and starts the pinned server."""
    script = (
        'mkdir -p /etc/claude-code && cat > /etc/claude-code/managed-mcp.json <<EOF\n'
        '{"mcpServers": {"svc-graph": {"type": "stdio", "command": "/opt/weave-mcp/neo4j/bin/mcp-neo4j-cypher", '
        '"args": [], "env": {"NEO4J_URI": "bolt://graph.svc:7687", "NEO4J_USERNAME": "neo4j", '
        '"NEO4J_PASSWORD": "x", "NEO4J_DATABASE": "neo4j"}}}}\nEOF\n'
        'cd /tmp && claude mcp list'
    )
    out = subprocess.run(
        ["docker", "run", "--rm", "--entrypoint", "bash", claude_code_image, "-lc", script],
        capture_output=True, text=True, timeout=180,
    )
    assert "svc-graph" in out.stdout and "Connected" in out.stdout, out.stdout + out.stderr


# ---- a server for each named, enabled database; reads seeded data; read-write; gone with the run

@needs_sysbox
def test_the_probe_is_given_a_server_for_each_named_enabled_database_and_reads_seeded_data_through_it(
    make_worker, tmp_path
):
    worker = worker_for(make_worker, tmp_path)
    try:
        started, final = run(worker, {"end": "succeed", "mcp": {"tools": True, "calls": [query("select v from notes")]}})
        report, record = mcp_report(worker, started.run_id), worker.record(started.run_id)
    finally:
        worker.shutdown()
    assert final.outcome is Outcome.SUCCEEDED, final.reason
    assert list(report["configured"]) == ["svc-db"]
    assert "execute_sql" in report["tools"]["svc-db"]
    (call,) = report["calls"]
    assert call["ok"] and "seeded-row" in call["text"], call
    assert [s["name"] for s in record.environment_mcp_servers] == ["svc-db"]


@needs_sysbox
def test_a_server_can_write_and_the_written_data_is_gone_in_the_next_run(make_worker, tmp_path):
    worker = worker_for(make_worker, tmp_path)
    try:
        first, first_final = run(worker, {"end": "succeed", "mcp": {"calls": [
            query("insert into notes values ('written-by-agent')"), query("select v from notes order by v"),
        ]}})
        assert first_final.outcome is Outcome.SUCCEEDED, first_final.reason
        _, written = mcp_report(worker, first.run_id)["calls"]
        assert written["ok"] and "written-by-agent" in written["text"], written
        second, second_final = run(worker, {"end": "succeed", "mcp": {"calls": [query("select v from notes")]}})
        (read,) = mcp_report(worker, second.run_id)["calls"]
    finally:
        worker.shutdown()
    assert second_final.outcome is Outcome.SUCCEEDED, second_final.reason
    assert read["ok"] and "seeded-row" in read["text"] and "written-by-agent" not in read["text"], read


@needs_sysbox
def test_each_server_connects_only_to_its_own_runs_database_two_runs_at_once_see_only_their_own_writes(
    make_worker, tmp_path
):
    worker = worker_for(make_worker, tmp_path)
    ids = {}
    try:
        for marker in ("run-a", "run-b"):
            started = worker.start(probe_request({"end": "succeed", "mcp": {"calls": [
                query(f"insert into notes values ('{marker}')"), query("select v from notes order by v"),
            ]}}, repos=["svc"]))
            assert isinstance(started, Started)
            ids[marker] = started.run_id
        finals = {m: wait_until_ended(worker, rid, timeout=900) for m, rid in ids.items()}
        reports = {m: mcp_report(worker, rid) for m, rid in ids.items()}
    finally:
        worker.shutdown()
    for marker, other in (("run-a", "run-b"), ("run-b", "run-a")):
        assert finals[marker].outcome is Outcome.SUCCEEDED, finals[marker].reason
        text = reports[marker]["calls"][1]["text"]
        assert marker in text and other not in text, f"{marker} saw {other}'s data: {text}"
        # The one host the server is configured with is the database's address in its own Environment.
        configured = reports[marker]["configured"]["svc-db"]
        assert configured["env_names"] == ["DATABASE_URI"]


@needs_sysbox
def test_a_database_the_recipe_does_not_name_gets_no_server_whatever_its_image(make_worker, tmp_path):
    worker = worker_for(make_worker, tmp_path)  # `other` is a Postgres image the recipe does not name
    try:
        started, final = run(worker, {"end": "succeed", "mcp": {}})
        report, record = mcp_report(worker, started.run_id), worker.record(started.run_id)
    finally:
        worker.shutdown()
    assert final.outcome is Outcome.SUCCEEDED, final.reason
    assert "svc-other" not in report["configured"]
    assert "other" not in {s["service"] for s in record.environment_mcp_servers + record.environment_mcp_omitted}


@needs_sysbox
def test_a_named_database_of_a_kind_the_product_has_not_enabled_gets_none_and_the_runs_log_says_so(
    make_worker, tmp_path
):
    worker = worker_for(make_worker, tmp_path)  # `graph` is named as neo4j; the Product enables only postgres
    try:
        started, final = run(worker, {"end": "succeed", "mcp": {}})
        report, record = mcp_report(worker, started.run_id), worker.record(started.run_id)
        log = (worker._run_dir(started.run_id) / "run.log").read_text()
    finally:
        worker.shutdown()
    assert final.outcome is Outcome.SUCCEEDED, final.reason
    assert "svc-graph" not in report["configured"]
    assert any(o["service"] == "graph" and o["kind"] == "neo4j" and "not enabled" in o["reason"]
               for o in record.environment_mcp_omitted)
    assert "no Environment MCP server for Repo 'svc' service 'graph'" in log and "not enabled" in log


@needs_sysbox
def test_redis_is_in_the_environment_but_has_no_server_and_the_record_says_why(make_worker, tmp_path):
    worker = worker_for(make_worker, tmp_path, kinds=("postgres", "neo4j", "mysql"))
    try:
        started, final = run(worker, {"end": "succeed", "mcp": {}, "exec_in_service": [
            {"repo": "svc", "service": "cache", "command": "redis-cli ping"}]})
        report, record = mcp_report(worker, started.run_id), worker.record(started.run_id)
    finally:
        worker.shutdown()
    assert final.outcome is Outcome.SUCCEEDED, final.reason
    assert "svc-cache" not in report["configured"]
    assert any(o["service"] == "cache" and "no MCP server" in o["reason"] for o in record.environment_mcp_omitted)


@needs_sysbox
def test_servers_are_named_for_their_repo_and_service_and_the_record_lists_those_started_and_omitted(
    make_worker, tmp_path
):
    worker = worker_for(make_worker, tmp_path)
    try:
        started, final = run(worker, {"end": "succeed", "mcp": {}})
        record = worker.record(started.run_id)
        assert sandboxes_of(started.run_id) == [], "the servers are gone with the sandbox"
    finally:
        worker.shutdown()
    assert final.outcome is Outcome.SUCCEEDED, final.reason
    (server,) = record.environment_mcp_servers
    assert (server["name"], server["repo"], server["service"], server["address"]) == ("svc-db", "svc", "db", "db.svc")
    assert server["server"] == CATALOG["postgres"].pin
    assert sorted(o["service"] for o in record.environment_mcp_omitted) == ["cache", "graph"]
    assert "app-pw" not in record.to_json(), "a database credential reached the run record"


@needs_sysbox
def test_a_repos_own_skills_still_load_alongside_the_servers(make_worker, tmp_path):
    worker = worker_for(make_worker, tmp_path, extra_files=OWN_SKILL)
    try:
        started, final = run(worker, {"end": "succeed", "mcp": {}})
        (report,) = probe_reports(worker.record(started.run_id).event_log)
    finally:
        worker.shutdown()
    assert final.outcome is Outcome.SUCCEEDED, final.reason
    assert report["skills_loaded"]["repo-own-skill"]["level"] == "project"
    assert list(report["mcp"]["configured"]) == ["svc-db"]


@needs_sysbox
def test_a_neo4j_server_reads_seeded_data_and_writes_through_the_same_wiring(make_worker, tmp_path):
    worker = worker_for(make_worker, tmp_path, NEO4J_RECIPE, kinds=("neo4j",))
    read = {"server": "svc-graph", "tool": "read_neo4j_cypher", "arguments": {"query": "MATCH (n:Note) RETURN n.v AS v"}}
    write = {"server": "svc-graph", "tool": "write_neo4j_cypher", "arguments": {"query": "CREATE (:Note {v: 'written'})"}}
    try:
        started, final = run(worker, {"end": "succeed", "mcp": {"calls": [read, write, read]}})
        before, wrote, after = mcp_report(worker, started.run_id)["calls"]
    finally:
        worker.shutdown()
    assert final.outcome is Outcome.SUCCEEDED, final.reason
    assert before["ok"] and "seeded-node" in before["text"] and "written" not in before["text"], before
    assert wrote["ok"], wrote
    assert after["ok"] and "written" in after["text"], after
