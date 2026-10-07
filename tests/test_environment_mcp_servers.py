"""Environment MCP servers (#55): which databases get one, how each is wired, and what is recorded.

Pure logic with fakes and no Docker. (The same behaviour on a real sysbox sandbox, with the probe
reading seeded data through the servers, is in test_environment_mcp_on_sysbox.py.) Each test is named
after an acceptance criterion of #55.

A recipe names its databases in `x-weave.databases` (service, kind, credentials); the Product lists
the kinds it enables; the catalog pairs each kind with a pinned server and the rule turning a
database's host, port, credentials and name into that server's settings.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import yaml
import pytest

from workflow_weave.agent_worker import Outcome, RunRecord, RunState
from workflow_weave.agent_worker.config import ProductConfig, ProductConfigError, WorkerSettings, load_product_config
from workflow_weave.agent_worker.environment import EnvironmentBringUpError, RecipeError, parse_recipe
from workflow_weave.agent_worker.worker import AgentWorker, _Run
from workflow_weave.agent_worker.mcp import CATALOG, Connection, McpPlanError, plan_environment_servers
from workflow_weave.agent_worker.sandbox import stage_mcp_servers


def recipe_text(databases=None, services=("db", "graph"), extra=None):
    block = {"readiness": {s: {"command": ["true"]} for s in services}}
    if databases is not None:
        block["databases"] = databases
    block.update(extra or {})
    return yaml.safe_dump({"services": {s: {"image": "alpine"} for s in services}, "x-weave": block})


PG = {"service": "db", "kind": "postgres", "credentials": {"user": "app", "password": "pw", "database": "chat"}}
NEO = {"service": "graph", "kind": "neo4j", "port": 7688, "credentials": {"user": "neo4j", "password": "n-pw"}}


# ---- the recipe names its databases: service, kind, credentials

def test_a_recipe_names_its_databases_with_service_kind_port_and_credentials():
    r = parse_recipe("svc", recipe_text([PG, NEO]))
    pg, neo = r.databases
    assert (pg.service, pg.kind, pg.port, pg.user, pg.password, pg.database) == ("db", "postgres", 5432, "app", "pw", "chat")
    # The port defaults to the kind's own; Neo4j's database defaults to its default database.
    assert (neo.service, neo.kind, neo.port, neo.database) == ("graph", "neo4j", 7688, "neo4j")


def test_a_recipe_that_names_no_databases_has_none_whatever_its_images_are():
    r = parse_recipe("svc", recipe_text())
    assert r.databases == ()


def test_credentials_can_come_from_the_services_own_environment_in_the_same_recipe():
    doc = {
        "services": {"db": {"image": "postgres:16", "environment": {"POSTGRES_USER": "app", "POSTGRES_DB": "chat"}}},
        "x-weave": {
            "readiness": {"db": {"command": ["true"]}},
            "databases": [{"service": "db", "kind": "postgres",
                           "credentials": {"user": {"env": "POSTGRES_USER"}, "password": "pw",
                                           "database": {"env": "POSTGRES_DB"}}}],
        },
    }
    (db,) = parse_recipe("svc", yaml.safe_dump(doc)).databases
    assert (db.user, db.database) == ("app", "chat")


@pytest.mark.parametrize("entry, problem", [
    ("db", "mapping"),
    ({"kind": "postgres", "credentials": PG["credentials"]}, "service"),
    ({**PG, "service": "ghost"}, "ghost"),
    ({**PG, "kind": "oracle"}, "oracle"),
    ({**PG, "port": 0}, "port"),
    ({**PG, "port": "5432"}, "port"),
    ({"service": "db", "kind": "postgres"}, "credentials"),
    ({"service": "db", "kind": "postgres", "credentials": {"user": "app", "password": "pw"}}, "database"),
    ({"service": "db", "kind": "postgres", "credentials": {"password": "pw", "database": "chat"}}, "user"),
    ({"service": "db", "kind": "mysql", "credentials": {"user": "u", "database": "d"}}, "password"),
    ({"service": "db", "kind": "postgres", "credentials": {"user": {"env": "NOPE"}, "password": "p", "database": "d"}}, "NOPE"),
    ({"service": "db", "kind": "postgres", "credentials": {"user": ["a"], "password": "p", "database": "d"}}, "user"),
])
def test_a_database_entry_that_cannot_be_wired_is_refused_naming_the_repo_and_what_to_fix(entry, problem):
    with pytest.raises(RecipeError) as e:
        parse_recipe("svc", recipe_text([entry]))
    assert "svc" in str(e.value) and "databases" in str(e.value) and problem in str(e.value)


def test_two_databases_in_one_service_are_refused_because_a_server_is_named_for_its_repo_and_service():
    with pytest.raises(RecipeError) as e:
        parse_recipe("svc", recipe_text([PG, {**PG, "kind": "mysql"}]))
    assert "db" in str(e.value) and "twice" in str(e.value)


def test_redis_may_be_named_but_needs_no_credentials():
    (redis,) = parse_recipe("svc", recipe_text([{"service": "db", "kind": "redis"}])).databases
    assert (redis.kind, redis.port, redis.user, redis.password, redis.database) == ("redis", 6379, None, None, None)


# ---- a Product lists which kinds it enables

def product_file(tmp_path, extra=""):
    path = tmp_path / "p.yaml"
    path.write_text(f"product: chat\n{extra}repos:\n  svc:\n    source: /srv/svc\n")
    return path


def test_a_product_lists_the_database_kinds_it_enables_and_by_default_enables_none(tmp_path):
    assert load_product_config(product_file(tmp_path)).database_mcp_kinds == frozenset()
    enabled = load_product_config(product_file(tmp_path, "database_mcp_kinds: [postgres, neo4j]\n"))
    assert enabled.database_mcp_kinds == frozenset({"postgres", "neo4j"})


@pytest.mark.parametrize("value, problem", [
    ("[oracle]", "oracle"),
    ("[redis]", "redis"),       # Redis is in the Environment but has no server to enable
    ("postgres", "list"),
    ("[postgres, postgres]", "twice"),
    ("[1]", "list"),
])
def test_a_kind_the_catalog_does_not_have_is_refused_naming_the_product_file(tmp_path, value, problem):
    path = product_file(tmp_path, f"database_mcp_kinds: {value}\n")
    with pytest.raises(ProductConfigError) as e:
        load_product_config(path)
    assert str(path) in str(e.value) and "database_mcp_kinds" in str(e.value) and problem in str(e.value)


# ---- a server for each named, enabled database; none for any other

MYSQL = {"service": "shop", "kind": "mysql", "credentials": {"user": "root", "password": "", "database": "shop"}}
REDIS = {"service": "cache", "kind": "redis"}


def plan_for(repo, databases, enabled, services=("db", "graph", "shop", "cache", "web")):
    recipe = parse_recipe(repo, recipe_text(databases, services))
    return plan_environment_servers([(repo, recipe.databases)], frozenset(enabled))


def test_a_server_is_planned_for_each_named_database_of_an_enabled_kind_named_for_its_repo_and_service():
    plan = plan_for("svc", [PG, NEO, MYSQL], {"postgres", "neo4j", "mysql"})
    assert [s.name for s in plan.started] == ["svc-db", "svc-graph", "svc-shop"]
    assert [(s.repo, s.service, s.kind, s.address) for s in plan.started] == [
        ("svc", "db", "postgres", "db.svc"), ("svc", "graph", "neo4j", "graph.svc"), ("svc", "shop", "mysql", "shop.svc"),
    ]
    assert plan.omitted == []


def test_each_catalog_entry_turns_host_port_credentials_and_name_into_its_servers_settings():
    plan = plan_for("svc", [PG, NEO, MYSQL], {"postgres", "neo4j", "mysql"})
    pg, neo, my = (s.settings.as_claude_entry() for s in plan.started)
    assert pg == {
        "type": "stdio", "command": "/opt/weave-mcp/postgres/bin/postgres-mcp",
        "args": ["--access-mode=unrestricted"],
        "env": {"DATABASE_URI": "postgresql://app:pw@db.svc:5432/chat"},
    }
    assert neo == {
        "type": "stdio", "command": "/opt/weave-mcp/neo4j/bin/mcp-neo4j-cypher", "args": [],
        "env": {"NEO4J_URI": "bolt://graph.svc:7688", "NEO4J_USERNAME": "neo4j", "NEO4J_PASSWORD": "n-pw",
                "NEO4J_DATABASE": "neo4j"},
    }
    assert my == {
        "type": "stdio", "command": "/opt/weave-mcp/mysql/bin/mysql_mcp_server", "args": [],
        "env": {"MYSQL_HOST": "shop.svc", "MYSQL_PORT": "3306", "MYSQL_USER": "root", "MYSQL_PASSWORD": "",
                "MYSQL_DATABASE": "shop"},
    }


def test_a_postgres_password_with_url_characters_is_encoded_into_the_connection_uri():
    odd = {**PG, "credentials": {"user": "a@b", "password": "p/w:x?#", "database": "d b"}}
    (server,) = plan_for("svc", [odd], {"postgres"}).started
    assert server.settings.env["DATABASE_URI"] == "postgresql://a%40b:p%2Fw%3Ax%3F%23@db.svc:5432/d%20b"


def test_servers_are_read_write_the_postgres_one_unrestricted_and_the_neo4j_one_without_read_only():
    plan = plan_for("svc", [PG, NEO], {"postgres", "neo4j"})
    pg, neo = plan.started
    assert "--access-mode=unrestricted" in pg.settings.args
    assert "--read-only" not in neo.settings.args


def test_a_server_gets_the_one_databases_address_and_nothing_from_the_environment_around_it():
    plan = plan_for("svc", [PG, NEO, MYSQL], {"postgres", "neo4j", "mysql"})
    for server in plan.started:
        entry = server.settings.as_claude_entry()
        hosts = {m for m in re.findall(r"[a-z0-9-]+\.svc", " ".join(entry["env"].values()))}
        assert hosts == {server.address}, (server.name, hosts)
        assert not any(k.startswith(("GH_", "GITHUB", "CLAUDE", "ANTHROPIC", "WEAVE")) for k in entry["env"])


def test_a_named_database_of_a_kind_the_product_has_not_enabled_gets_no_server_and_is_listed_as_omitted_with_the_reason():
    plan = plan_for("svc", [PG, NEO], {"postgres"})
    assert [s.name for s in plan.started] == ["svc-db"]
    (omitted,) = plan.omitted
    assert (omitted.repo, omitted.service, omitted.kind) == ("svc", "graph", "neo4j")
    assert "neo4j" in omitted.reason and "not enabled" in omitted.reason


def test_a_product_that_enables_nothing_gets_no_servers():
    plan = plan_for("svc", [PG, NEO, MYSQL], set())
    assert plan.started == [] and [o.service for o in plan.omitted] == ["db", "graph", "shop"]


def test_redis_is_named_in_the_environment_but_has_no_server_whatever_the_product_enables():
    plan = plan_for("svc", [REDIS], {"postgres", "neo4j", "mysql"})
    assert plan.started == []
    (omitted,) = plan.omitted
    assert (omitted.service, omitted.kind) == ("cache", "redis") and "no MCP server" in omitted.reason


def test_a_database_the_recipe_does_not_name_gets_no_server_and_is_not_listed_whatever_its_image():
    recipe = parse_recipe("svc", yaml.safe_dump({
        "services": {"db": {"image": "postgres:16"}, "graph": {"image": "neo4j:5"}},
        "x-weave": {"readiness": {"db": {"command": ["true"]}, "graph": {"command": ["true"]}}},
    }))
    plan = plan_environment_servers([("svc", recipe.databases)], frozenset({"postgres", "neo4j"}))
    assert plan.started == [] and plan.omitted == []


def test_servers_of_several_repos_are_named_for_each_repo_and_service_in_the_order_given():
    plan = plan_environment_servers(
        [("svc", parse_recipe("svc", recipe_text([PG])).databases),
         ("client", parse_recipe("client", recipe_text([PG])).databases)],
        frozenset({"postgres"}),
    )
    assert [s.name for s in plan.started] == ["svc-db", "client-db"]
    assert [s.address for s in plan.started] == ["db.svc", "db.client"]


def test_two_servers_that_would_share_a_name_are_refused_naming_both():
    a = parse_recipe("a-b", recipe_text([{**PG, "service": "c"}], services=("c",))).databases
    b = parse_recipe("a", recipe_text([{**PG, "service": "b-c"}], services=("b-c",))).databases
    with pytest.raises(McpPlanError) as e:
        plan_environment_servers([("a-b", a), ("a", b)], frozenset({"postgres"}))
    assert "a-b-c" in str(e.value) and "a-b" in str(e.value) and "b-c" in str(e.value)


class LocalSandbox:
    """Plays the sandbox's shell and file upload on this machine, with HOME inside `root`."""

    def __init__(self, root):
        self.root, self.home = root, root / "home"
        self.home.mkdir()
        self.commands = []
        self.workspace = self

    def _local(self, text):
        return text.replace("/tmp/weave-staging", str(self.root / "staging"))

    def file_upload(self, source, destination):
        target = Path(self._local(str(destination)))
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(source, target)

    def sh(self, command, timeout=120, cwd="/"):
        self.commands.append(command)
        done = subprocess.run(
            ["sh", "-c", self._local(command)], capture_output=True, text=True, env={**os.environ, "HOME": str(self.home)}
        )
        assert done.returncode == 0, done.stderr
        return done.stdout


SERVERS = {"svc-db": {"type": "stdio", "command": "/opt/weave-mcp/postgres/bin/postgres-mcp", "args": [],
                      "env": {"DATABASE_URI": "postgresql://app:pw@db.svc:5432/chat"}}}


def test_servers_are_staged_in_the_agents_user_level_configuration_and_nowhere_in_a_working_copy(tmp_path):
    sandbox = LocalSandbox(tmp_path)
    stage_mcp_servers(sandbox, SERVERS)
    config = json.loads((sandbox.home / ".claude.json").read_text())
    assert config["mcpServers"] == SERVERS
    assert not (tmp_path / "staging" / "mcp-servers.json").exists(), "the staged copy of the credentials was left behind"
    assert (sandbox.home / ".claude.json").stat().st_mode & 0o077 == 0, "the configuration holds credentials"
    assert list(tmp_path.rglob(".mcp.json")) == []


def test_staging_keeps_what_the_user_level_configuration_already_holds_and_servers_staged_before(tmp_path):
    sandbox = LocalSandbox(tmp_path)
    (sandbox.home / ".claude.json").write_text(json.dumps({
        "theme": "dark", "mcpServers": {"earlier": {"type": "http", "url": "https://example.test/mcp"}},
    }))
    stage_mcp_servers(sandbox, SERVERS)
    # A second call (External MCP servers, #56) adds to the first and replaces a server of the same name.
    stage_mcp_servers(sandbox, {"ext-pay": {"type": "stdio", "command": "pay-mcp"},
                                "svc-db": {"type": "stdio", "command": "replaced"}})
    config = json.loads((sandbox.home / ".claude.json").read_text())
    assert config["theme"] == "dark"
    assert set(config["mcpServers"]) == {"earlier", "svc-db", "ext-pay"}
    assert config["mcpServers"]["svc-db"]["command"] == "replaced"


def test_staging_no_servers_writes_nothing(tmp_path):
    sandbox = LocalSandbox(tmp_path)
    stage_mcp_servers(sandbox, {})
    assert sandbox.commands == [] and not (sandbox.home / ".claude.json").exists()


def test_the_pins_in_the_catalog_are_the_ones_the_sandbox_image_installs_each_in_its_own_virtualenv():
    dockerfile = (Path(__file__).parent.parent / "sandbox" / "Dockerfile").read_text()
    args = dict(re.findall(r"^ARG (\w+_VERSION)=(\S+)$", dockerfile, re.MULTILINE))
    assert args["POSTGRES_MCP_VERSION"] == CATALOG["postgres"].version
    assert args["NEO4J_MCP_VERSION"] == CATALOG["neo4j"].version
    assert args["MYSQL_MCP_VERSION"] == CATALOG["mysql"].version
    assert f"mcp[cli]=={args['POSTGRES_MCP_SDK_VERSION']}" in CATALOG["postgres"].extra_pins
    for kind, entry in CATALOG.items():
        assert f"python3 -m venv /opt/weave-mcp/{kind}" in dockerfile
        assert f"/opt/weave-mcp/{kind}/bin/pip install" in dockerfile and entry.package in dockerfile
        assert entry.settings_for(Connection("h.r", 1, "u", "p", "d")).command == f"/opt/weave-mcp/{kind}/bin/{entry.executable}"


class EnvironmentStub:
    """What the worker holds of a Repo brought up: its name and the recipe it ran."""

    def __init__(self, repo, databases, services=("db", "graph", "shop", "cache")):
        self.repo = repo
        self.recipe = parse_recipe(repo, recipe_text(databases, services))


def wired(tmp_path, environments, enabled):
    """Run the worker's step that starts the servers on a local stand-in for the sandbox."""
    product = ProductConfig(name="chat", repos={}, database_mcp_kinds=frozenset(enabled))
    settings = WorkerSettings(
        runs_dir=tmp_path / "runs", products={"chat": product}, agent_profiles={}, subscriptions=None,
        push_gateway=object(),
    )
    worker = AgentWorker(settings)
    record = RunRecord(run_id="r1", product="chat", agent_profile="probe", subscription="s", skill="k", repos=[],
                       state=RunState.RUNNING, started_at="t")
    worker._run_dir("r1").mkdir(parents=True)
    run = _Run(record, request=None, lease=None)
    worker._runs["r1"] = run
    sandbox = LocalSandbox(tmp_path / "sandbox")
    worker._start_environment_mcp_servers(run, product, sandbox, environments)
    log = (worker._run_dir("r1") / "run.log").read_text() if (worker._run_dir("r1") / "run.log").exists() else ""
    return record, log, sandbox, worker


def test_the_servers_are_wired_into_the_agents_configuration_and_the_run_record_lists_those_started_and_those_omitted(tmp_path):
    (tmp_path / "sandbox").mkdir()
    record, log, sandbox, worker = wired(
        tmp_path, [EnvironmentStub("svc", [PG, NEO, REDIS])], {"postgres"},
    )
    config = json.loads((sandbox.home / ".claude.json").read_text())
    assert list(config["mcpServers"]) == ["svc-db"]
    assert record.environment_mcp_servers == [
        {"name": "svc-db", "repo": "svc", "service": "db", "kind": "postgres", "address": "db.svc",
         "server": "postgres-mcp==0.3.0"},
    ]
    assert [(o["service"], o["kind"]) for o in record.environment_mcp_omitted] == [("graph", "neo4j"), ("cache", "redis")]
    # Saved, so a caller who reads the record later sees it too.
    saved = worker.record("r1")
    assert saved.environment_mcp_servers == record.environment_mcp_servers
    assert saved.environment_mcp_omitted == record.environment_mcp_omitted


def test_the_run_log_says_which_servers_started_and_that_a_kind_the_product_has_not_enabled_got_none(tmp_path):
    (tmp_path / "sandbox").mkdir()
    _, log, _, _ = wired(tmp_path, [EnvironmentStub("svc", [PG, NEO, REDIS])], {"postgres"})
    assert "Environment MCP server svc-db started" in log and "postgres-mcp==0.3.0" in log
    assert "no Environment MCP server for Repo 'svc' service 'graph'" in log and "neo4j" in log and "not enabled" in log
    assert "service 'cache'" in log and "redis has no MCP server" in log
    assert "pw" not in log and "n-pw" not in log, "a credential reached the run log"


def test_a_run_whose_recipes_name_no_database_has_no_servers_and_a_record_that_says_so_by_having_none(tmp_path):
    (tmp_path / "sandbox").mkdir()
    record, log, sandbox, _ = wired(tmp_path, [EnvironmentStub("svc", None)], {"postgres", "neo4j", "mysql"})
    assert record.environment_mcp_servers is None and record.environment_mcp_omitted is None
    assert not (sandbox.home / ".claude.json").exists()


def test_servers_of_every_repo_in_the_environment_are_wired_dependencies_first(tmp_path):
    (tmp_path / "sandbox").mkdir()
    record, _, sandbox, _ = wired(
        tmp_path, [EnvironmentStub("svc", [PG]), EnvironmentStub("client", [PG])], {"postgres"},
    )
    assert [s["name"] for s in record.environment_mcp_servers] == ["svc-db", "client-db"]
    assert list(json.loads((sandbox.home / ".claude.json").read_text())["mcpServers"]) == ["svc-db", "client-db"]


def test_two_servers_that_would_share_a_name_end_the_run_as_needs_setup_before_any_agent_starts(tmp_path):
    (tmp_path / "sandbox").mkdir()
    a = EnvironmentStub("a-b", [{**PG, "service": "c"}], services=("c",))
    b = EnvironmentStub("a", [{**PG, "service": "b-c"}], services=("b-c",))
    with pytest.raises(EnvironmentBringUpError) as e:
        wired(tmp_path, [a, b], {"postgres"})
    assert e.value.outcome is Outcome.NEEDS_SETUP and "a-b-c" in e.value.reason


def test_a_record_saved_before_mcp_servers_existed_loads_with_none_listed():
    rec = RunRecord(run_id="r", product="p", agent_profile="a", subscription="s", skill="k", repos=[],
                    state=RunState.ENDED, started_at="t")
    d = json.loads(rec.to_json())
    del d["environment_mcp_servers"], d["environment_mcp_omitted"]
    loaded = RunRecord.from_json(json.dumps(d))
    assert loaded.environment_mcp_servers is None and loaded.environment_mcp_omitted is None


def test_the_record_entries_name_the_server_and_its_pin_and_never_a_credential():
    plan = plan_for("svc", [PG, NEO], {"postgres"})
    (started,) = plan.started
    assert started.as_record() == {
        "name": "svc-db", "repo": "svc", "service": "db", "kind": "postgres", "address": "db.svc",
        "server": "postgres-mcp==0.3.0",
    }
    (omitted,) = plan.omitted
    assert set(omitted.as_record()) == {"repo", "service", "kind", "reason"}
    assert "pw" not in json.dumps([started.as_record(), omitted.as_record()])
