"""External MCP servers (#56) and ignoring a Repo's own MCP config.

Pure logic with fakes and no Docker, except test_claude_code_ignores_..., which runs the real
`claude` CLI when this machine has one (see its skip conditions). (The same behaviour on a real sysbox
sandbox, with the probe using the servers, is in test_external_mcp_on_sysbox.py.) Each test is named
after an acceptance criterion of #56.

A recipe declares External MCP servers in `x-weave.external_mcp`: a name, a command and arguments (a
stdio server, started inside the run's sandbox, so every run has its own copy) or a url (a remote
server), and the names of the Test secrets it uses.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from workflow_weave.agent_worker.environment import RecipeError, parse_recipe


def recipe_text(external, services=("web",), extra=None):
    block = {"readiness": {s: {"command": ["true"]} for s in services}, "external_mcp": external}
    block.update(extra or {})
    return yaml.safe_dump({"services": {s: {"image": "alpine"} for s in services}, "x-weave": block})


STDIO = {"name": "pay", "command": "npx", "args": ["-y", "pay-mcp@1.2.3"], "secrets": ["PAY_TEST_KEY"]}
HTTP = {"name": "docs", "url": "https://mcp.example.test/mcp", "headers": {"Authorization": "Bearer ${DOCS_TOKEN}"},
        "secrets": ["DOCS_TOKEN"]}


# ---- a recipe declares its External MCP servers

def test_a_recipe_declares_external_mcp_servers_stdio_by_command_and_http_by_url_with_the_secrets_each_uses():
    recipe = parse_recipe("svc", recipe_text([STDIO, HTTP]))
    pay, docs = recipe.external_mcp
    assert (pay.name, pay.command, pay.args, pay.url, pay.secrets) == ("pay", "npx", ("-y", "pay-mcp@1.2.3"), None, ("PAY_TEST_KEY",))
    assert (docs.name, docs.command, docs.url, docs.secrets) == ("docs", None, "https://mcp.example.test/mcp", ("DOCS_TOKEN",))
    assert dict(docs.headers) == {"Authorization": "Bearer ${DOCS_TOKEN}"}


def test_a_recipe_that_declares_none_has_none():
    assert parse_recipe("svc", recipe_text(None)).external_mcp == ()
    assert parse_recipe("svc", recipe_text([])).external_mcp == ()


def test_the_secrets_an_external_server_uses_are_not_given_to_the_recipes_own_services():
    recipe = parse_recipe("svc", recipe_text([STDIO], extra={"secrets": ["SERVICE_KEY"]}))
    assert recipe.secrets == ("SERVICE_KEY",)
    assert recipe.external_mcp_secrets == ("PAY_TEST_KEY",)


@pytest.mark.parametrize("entry, problem", [
    ("pay", "mapping"),
    ({**STDIO, "name": ""}, "name"),
    ({**STDIO, "name": "has space"}, "name"),
    ({k: v for k, v in STDIO.items() if k != "command"}, "command"),
    ({**STDIO, "url": "https://x.test/mcp"}, "both"),
    ({**STDIO, "command": ["npx"]}, "command"),
    ({**STDIO, "args": "-y"}, "args"),
    ({**STDIO, "args": [1]}, "args"),
    ({**STDIO, "headers": {"A": "b"}}, "headers"),
    ({**STDIO, "secrets": "PAY_TEST_KEY"}, "secrets"),
    ({**STDIO, "secrets": [{"PAY_TEST_KEY": "value"}]}, "secrets"),
    ({**STDIO, "secrets": ["not a name"]}, "not a name"),
    ({**STDIO, "secrets": ["A", "A"]}, "twice"),
    ({**STDIO, "env": {"X": "1"}}, "env"),
    ({**HTTP, "url": "ftp://x.test"}, "url"),
    ({**HTTP, "url": "https://x.test/mcp?token=${DOCS_TOKEN}"}, "${"),
    ({**HTTP, "args": ["a"]}, "args"),
    ({**HTTP, "headers": {"Authorization": "Bearer ${OTHER}"}}, "OTHER"),
    ({**HTTP, "headers": {"Authorization": "Bearer ${GH_TOKEN}"}}, "GH_TOKEN"),
    ({**HTTP, "headers": {"Authorization": "Bearer ${DOCS_TOKEN:-x}"}}, "${"),
    ({**HTTP, "secrets": ["DOCS_TOKEN", "UNUSED"]}, "UNUSED"),
    ({**STDIO, "args": ["--token=${ANTHROPIC_API_KEY}"]}, "${"),
    ({**STDIO, "command": "${HOME}/run"}, "${"),
    ({**HTTP, "url": "http://weave-git:8080/mcp"}, "weave-git"),
])
def test_an_external_server_that_cannot_be_started_as_declared_is_refused_naming_the_repo_and_what_to_fix(entry, problem):
    with pytest.raises(RecipeError) as e:
        parse_recipe("svc", recipe_text([entry]))
    assert "svc" in str(e.value) and "external_mcp" in str(e.value) and problem in str(e.value), str(e.value)


@pytest.mark.parametrize("secret", ["GH_TOKEN", "GITHUB_TOKEN", "gitlab_token", "CI_JOB_TOKEN"])
def test_a_recipe_cannot_hand_an_external_server_a_tracker_or_code_host_token_by_naming_it_as_a_test_secret(secret):
    with pytest.raises(RecipeError) as e:
        parse_recipe("svc", recipe_text([{**STDIO, "secrets": [secret]}]))
    assert secret in str(e.value) and "Tracker or Code host" in str(e.value)


def test_two_external_servers_of_one_recipe_cannot_share_a_name():
    with pytest.raises(RecipeError) as e:
        parse_recipe("svc", recipe_text([STDIO, {**HTTP, "name": "pay"}]))
    assert "pay" in str(e.value) and "twice" in str(e.value)


def test_external_mcp_that_is_not_a_list_is_refused():
    with pytest.raises(RecipeError) as e:
        parse_recipe("svc", recipe_text({"pay": STDIO}))
    assert "external_mcp" in str(e.value) and "list" in str(e.value)


# ---- each declared server is wired with only the Test secrets its recipe names

from workflow_weave.agent_worker.mcp import (  # noqa: E402
    McpPlanError, plan_external_servers, refuse_forge_credentials,
)

PAY_KEY = "pay-test-key-3d9f61aa"
DOCS_TOKEN = "docs-test-token-77b0e2c1"
SECRETS = {"svc": {"PAY_TEST_KEY": PAY_KEY, "DOCS_TOKEN": DOCS_TOKEN, "UNNAMED": "never-given"}}


def plan_of(external, repo="svc", secrets=None, taken=()):
    recipe = parse_recipe(repo, recipe_text(external))
    return plan_external_servers([(repo, recipe.external_mcp)], SECRETS if secrets is None else secrets, taken)


def run_entry(entry, parent_env):
    """Start a stdio entry the way Claude Code does: its `env` over the environment it runs in."""
    done = subprocess.run(
        [entry["command"], *entry["args"]], capture_output=True, text=True, env={**parent_env, **entry.get("env", {})}
    )
    assert done.returncode == 0, done.stderr
    return dict(line.split("=", 1) for line in done.stdout.splitlines())


def test_a_server_is_planned_for_each_declared_server_named_for_its_repo_and_the_name_it_was_declared_under():
    plan = plan_of([STDIO, HTTP])
    assert [s.name for s in plan.started] == ["svc-pay", "svc-docs"]
    assert [(s.repo, s.declared, s.transport) for s in plan.started] == [("svc", "pay", "stdio"), ("svc", "docs", "http")]


def test_a_stdio_server_starts_the_declared_command_with_its_arguments_and_its_named_secret_as_an_environment_variable():
    plan = plan_of([STDIO])
    entry = plan.entries()["svc-pay"]
    assert entry["type"] == "stdio" and entry["env"] == {"PAY_TEST_KEY": PAY_KEY}
    # The declared command and its arguments are what runs, after the wrapper (see below).
    assert entry["args"][-3:] == ["npx", "-y", "pay-mcp@1.2.3"]


def test_a_stdio_server_sees_its_named_secret_and_nothing_else_of_the_environment_it_is_started_in():
    # Claude Code runs a server in its own environment plus the entry's `env`. The sandbox's carries
    # the Subscription's credential (and could carry anything): the server must not see it.
    parent = {
        "PATH": os.environ["PATH"], "HOME": "/root", "ANTHROPIC_API_KEY": "sk-subscription", "GH_TOKEN": "ghp_forge",
        "GITHUB_TOKEN": "ghp_other", "WEAVE_RUN_TOKEN": "run-token", "UNNAMED": "never-given",
    }
    entry = plan_of([{**STDIO, "command": "env", "args": []}]).entries()["svc-pay"]
    seen = run_entry(entry, parent)
    assert set(seen) == {"PATH", "HOME", "PAY_TEST_KEY"}, seen
    assert seen["PAY_TEST_KEY"] == PAY_KEY


def test_a_stdio_server_with_no_secrets_sees_only_path_and_home():
    entry = plan_of([{"name": "plain", "command": "env"}]).entries()["svc-plain"]
    assert "env" not in entry
    assert set(run_entry(entry, {"PATH": os.environ["PATH"], "HOME": "/root", "GH_TOKEN": "t"})) == {"PATH", "HOME"}


def test_a_secret_value_with_awkward_characters_reaches_the_server_exactly():
    tricky = "pa ss'wo\"rd $HOME;`id` ${PATH}"
    with pytest.raises(McpPlanError):  # `${` could be expanded by Claude Code before the server sees it
        plan_of([{**STDIO, "command": "env", "args": []}], secrets={"svc": {"PAY_TEST_KEY": tricky}})
    tricky = "pa ss'wo\"rd $HOME;`id` {PATH}"
    entry = plan_of(
        [{**STDIO, "command": "env", "args": []}], secrets={"svc": {"PAY_TEST_KEY": tricky}}
    ).entries()["svc-pay"]
    assert run_entry(entry, {"PATH": os.environ["PATH"], "HOME": "/root"})["PAY_TEST_KEY"] == tricky


def test_a_remote_server_gets_its_url_and_headers_with_only_its_named_secrets_filled_in():
    entry = plan_of([HTTP]).entries()["svc-docs"]
    assert entry == {"type": "http", "url": "https://mcp.example.test/mcp",
                     "headers": {"Authorization": f"Bearer {DOCS_TOKEN}"}}


def test_a_server_is_given_only_the_secrets_it_names_not_those_another_server_or_the_product_has():
    plan = plan_of([STDIO, HTTP])
    text = {s.name: json.dumps(plan.entries()[s.name]) for s in plan.started}
    assert PAY_KEY in text["svc-pay"] and DOCS_TOKEN not in text["svc-pay"]
    assert DOCS_TOKEN in text["svc-docs"] and PAY_KEY not in text["svc-docs"]
    assert "never-given" not in "".join(text.values())


def test_the_record_of_a_server_holds_names_only_never_a_value_a_command_or_a_url():
    plan = plan_of([STDIO, HTTP])
    records = [s.as_record() for s in plan.started]
    assert records == [
        {"name": "svc-pay", "repo": "svc", "declared": "pay", "transport": "stdio", "secrets": ["PAY_TEST_KEY"]},
        {"name": "svc-docs", "repo": "svc", "declared": "docs", "transport": "http", "secrets": ["DOCS_TOKEN"]},
    ]
    assert PAY_KEY not in json.dumps(records) and DOCS_TOKEN not in json.dumps(records)


def test_servers_of_several_repos_each_get_that_repos_own_secrets():
    other = {"name": "pay", "command": "npx", "secrets": ["PAY_TEST_KEY"]}
    recipes = [("svc", parse_recipe("svc", recipe_text([STDIO])).external_mcp),
               ("client", parse_recipe("client", recipe_text([other])).external_mcp)]
    plan = plan_external_servers(
        recipes, {"svc": {"PAY_TEST_KEY": "svc-value"}, "client": {"PAY_TEST_KEY": "client-value"}}, ()
    )
    assert [s.name for s in plan.started] == ["svc-pay", "client-pay"]
    assert plan.entries()["svc-pay"]["env"] == {"PAY_TEST_KEY": "svc-value"}
    assert plan.entries()["client-pay"]["env"] == {"PAY_TEST_KEY": "client-value"}


def test_a_server_whose_name_is_already_taken_by_an_environment_server_or_another_is_refused_naming_it():
    with pytest.raises(McpPlanError) as e:
        plan_of([STDIO], taken=["svc-pay"])
    assert "svc-pay" in str(e.value)
    a = parse_recipe("a-b", recipe_text([{"name": "c", "command": "x"}])).external_mcp
    b = parse_recipe("a", recipe_text([{"name": "b-c", "command": "x"}])).external_mcp
    with pytest.raises(McpPlanError) as e:
        plan_external_servers([("a-b", a), ("a", b)], {}, ())
    assert "a-b-c" in str(e.value)


def test_a_secret_the_run_was_not_given_is_refused_by_name_never_started_without_it():
    with pytest.raises(McpPlanError) as e:
        plan_of([STDIO], secrets={"svc": {}})
    assert "PAY_TEST_KEY" in str(e.value) and "svc-pay" in str(e.value)


# ---- no Tracker or Code host credential can end up in a server's configuration

FORGE = {"push-token": "tok_9d41c7e2a0b84f13", "gh": "ghp_realLookingToken0123456789"}


def test_a_configuration_holding_the_push_gateway_token_or_a_code_host_token_is_refused_naming_the_server_not_the_value():
    plan = plan_of([STDIO, HTTP])
    refuse_forge_credentials(plan, FORGE.values())  # clean: nothing raised
    for leak in FORGE.values():
        bad = plan_of([STDIO], secrets={"svc": {"PAY_TEST_KEY": f"prefix-{leak}-suffix"}})
        with pytest.raises(McpPlanError) as e:
            refuse_forge_credentials(bad, FORGE.values())
        assert "svc-pay" in str(e.value) and leak not in str(e.value) and "Tracker or Code host" in str(e.value)


def test_a_remote_server_cannot_be_pointed_at_the_push_gateway_by_a_header_or_a_url_after_the_fact():
    plan = plan_of([HTTP])
    plan.started[0].entry["url"] = "http://weave-git:8123/anything"
    with pytest.raises(McpPlanError) as e:
        refuse_forge_credentials(plan, [])
    assert "svc-docs" in str(e.value) and "weave-git" in str(e.value)


# ---- a Repo's own committed MCP config is not used (checked against the real Claude Code)

from workflow_weave.agent_worker.sandbox import MANAGED_MCP_PATH, stage_mcp_servers  # noqa: E402


class HostSandbox:
    """Plays the sandbox on this machine itself (so a real `claude` reads what is staged)."""

    def __init__(self):
        self.workspace = self

    def file_upload(self, source, destination):
        Path(destination).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(source, destination)

    def sh(self, command, timeout=120, cwd="/"):
        done = subprocess.run(["sh", "-c", command], capture_output=True, text=True)
        assert done.returncode == 0, done.stderr


def claude_init_servers(cwd: Path, config_dir: Path) -> list[dict]:
    """The MCP servers a headless `claude -p` starts with (its `init` event), without calling the model."""
    proc = subprocess.Popen(
        ["claude", "-p", "hi", "--output-format", "stream-json", "--verbose"], cwd=cwd, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
        env={**os.environ, "CLAUDE_CONFIG_DIR": str(config_dir)},
    )
    try:
        for line in proc.stdout:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get("type") == "system" and event.get("subtype") == "init":
                return event.get("mcp_servers") or []
    finally:
        proc.kill()
        proc.wait()
    pytest.skip("claude printed no init event (no network or credentials?)")


@pytest.mark.skipif(shutil.which("claude") is None, reason="needs the Claude Code CLI on PATH")
@pytest.mark.skipif(os.geteuid() != 0, reason=f"writes {MANAGED_MCP_PATH}, which needs root (the sandbox's user)")
def test_claude_code_loads_a_repos_committed_mcp_config_unless_managed_config_is_staged_and_then_only_ours(tmp_path):
    managed = Path(MANAGED_MCP_PATH)
    if managed.exists():
        pytest.skip(f"{managed} exists on this machine; not touching it")
    repo, config = tmp_path / "repo", tmp_path / "claude-config"
    repo.mkdir()
    config.mkdir()
    (repo / ".mcp.json").write_text(json.dumps({"mcpServers": {"repo-evil": {"type": "stdio", "command": "true"}}}))
    (config / ".claude.json").write_text(json.dumps({"mcpServers": {"user-level": {"type": "stdio", "command": "true"}}}))

    def names():
        return {s["name"]: s["source"] for s in claude_init_servers(repo, config)}

    created = not managed.parent.exists()
    try:
        # The premise: headless, a Repo's own .mcp.json is loaded without anyone approving it.
        assert names() == {"repo-evil": "project", "user-level": "user"}
        # With nothing staged but the empty set, none of them loads.
        stage_mcp_servers(HostSandbox(), {})
        assert names() == {}
        # Our servers load; the Repo's still does not, nor the user-level one.
        stage_mcp_servers(HostSandbox(), {"svc-pay": {"type": "stdio", "command": "true"}})
        assert names() == {"svc-pay": "enterprise"}
    finally:
        managed.unlink(missing_ok=True)
        if created:
            managed.parent.rmdir()


# ---- the worker: starting the servers for a run, and ignoring a Repo's own MCP config

from conftest import PRODUCT, make_repo, probe_request, wait_until_ended  # noqa: E402
from test_environment_mcp_servers import LocalSandbox  # noqa: E402
from test_test_secrets import (  # noqa: E402,F401  (fixtures used below)
    RunSandbox, probe_profile, sandbox_stand_in, stand_in_docker, worker_for, write_secrets,
)

from workflow_weave.agent_worker import NeedsSetup, Outcome, RunRecord, RunState, Started  # noqa: E402
from workflow_weave.agent_worker.config import ProductConfig, WorkerSettings  # noqa: E402
from workflow_weave.agent_worker.environment import EnvironmentBringUpError  # noqa: E402
from workflow_weave.agent_worker.secret_store import SecretStore  # noqa: E402
from workflow_weave.agent_worker.worker import AgentWorker, _Run  # noqa: E402


class EnvironmentStub:
    """What the worker holds of a Repo brought up: its name and the recipe it ran."""

    def __init__(self, repo, external):
        self.repo = repo
        self.recipe = parse_recipe(repo, recipe_text(external))


def external_worker(tmp_path, secrets=None, store=None):
    settings = WorkerSettings(
        runs_dir=tmp_path / "runs", products={"chat": ProductConfig(name="chat", repos={})}, agent_profiles={},
        subscriptions=None, push_gateway=object(), test_secrets=store,
    )
    worker = AgentWorker(settings)
    record = RunRecord(run_id="r1", product="chat", agent_profile="probe", subscription="s", skill="k", repos=[],
                       state=RunState.RUNNING, started_at="t")
    worker._run_dir("r1").mkdir(parents=True)
    run = _Run(record, request=type("R", (), {"product": "chat"})(), lease=None)
    run.test_secrets = {repo: dict(values) for repo, values in (secrets or {}).items()}
    worker._runs["r1"] = run
    return worker, run


def started(tmp_path, environments, secrets=None, taken=(), forge=(), store=None):
    (tmp_path / "sandbox").mkdir(exist_ok=True)
    worker, run = external_worker(tmp_path, SECRETS if secrets is None else secrets, store)
    sandbox = LocalSandbox(tmp_path / "sandbox")
    worker._start_external_mcp_servers(run, sandbox, environments, taken, forge)
    log = worker._run_dir("r1") / "run.log"
    return run.record, log.read_text() if log.exists() else "", sandbox, worker


def test_a_declared_external_server_is_started_for_the_run_and_the_agent_has_it_in_its_mcp_configuration(tmp_path):
    record, log, sandbox, _ = started(tmp_path, [EnvironmentStub("svc", [STDIO, HTTP])])
    servers = json.loads(sandbox.managed.read_text())["mcpServers"]
    assert list(servers) == ["svc-pay", "svc-docs"]
    assert servers["svc-pay"]["env"] == {"PAY_TEST_KEY": PAY_KEY}
    assert servers["svc-docs"]["headers"] == {"Authorization": f"Bearer {DOCS_TOKEN}"}
    assert sandbox.managed.stat().st_mode & 0o077 == 0


def test_the_run_record_lists_the_external_servers_started_by_name_and_the_log_has_a_line_for_each(tmp_path):
    record, log, _, worker = started(tmp_path, [EnvironmentStub("svc", [STDIO, HTTP])])
    assert record.external_mcp_servers == [
        {"name": "svc-pay", "repo": "svc", "declared": "pay", "transport": "stdio", "secrets": ["PAY_TEST_KEY"]},
        {"name": "svc-docs", "repo": "svc", "declared": "docs", "transport": "http", "secrets": ["DOCS_TOKEN"]},
    ]
    assert worker.record("r1").external_mcp_servers == record.external_mcp_servers  # saved
    assert "External MCP server svc-pay started (stdio; Test secrets: PAY_TEST_KEY)" in log
    assert "External MCP server svc-docs started (http; Test secrets: DOCS_TOKEN)" in log
    written = log + json.dumps(record.external_mcp_servers) + (worker._run_dir("r1") / "record.json").read_text()
    for value in (PAY_KEY, DOCS_TOKEN, "never-given", "npx", "mcp.example.test"):
        assert value not in written, f"{value} was written for the run"


def test_servers_of_every_repo_in_the_environment_are_started_not_only_the_one_worked_on(tmp_path):
    other = {"name": "search", "command": "search-mcp"}
    record, _, sandbox, _ = started(
        tmp_path, [EnvironmentStub("svc", [STDIO]), EnvironmentStub("client", [other])],
        secrets={"svc": {"PAY_TEST_KEY": PAY_KEY}},
    )
    assert [s["name"] for s in record.external_mcp_servers] == ["svc-pay", "client-search"]
    assert list(json.loads(sandbox.managed.read_text())["mcpServers"]) == ["svc-pay", "client-search"]


def test_a_run_whose_recipes_declare_none_starts_none_and_stages_nothing(tmp_path):
    record, log, sandbox, _ = started(tmp_path, [EnvironmentStub("svc", None)])
    assert record.external_mcp_servers is None and "External MCP" not in log
    assert not sandbox.managed.exists()


def test_a_server_that_would_share_a_name_with_an_environment_server_ends_the_run_as_needs_setup(tmp_path):
    with pytest.raises(EnvironmentBringUpError) as e:
        started(tmp_path, [EnvironmentStub("svc", [STDIO])], taken=["svc-pay"])
    assert e.value.outcome is Outcome.NEEDS_SETUP and "svc-pay" in e.value.reason


def test_a_tracker_or_code_host_credential_in_a_servers_configuration_ends_the_run_and_nothing_is_staged(tmp_path):
    leaky = {"svc": {"PAY_TEST_KEY": "prefix-tok_9d41c7e2a0b84f13"}}
    (tmp_path / "sandbox").mkdir()
    with pytest.raises(EnvironmentBringUpError) as e:
        started(tmp_path, [EnvironmentStub("svc", [STDIO])], secrets=leaky, forge=["tok_9d41c7e2a0b84f13"])
    assert e.value.outcome is Outcome.NEEDS_SETUP and "svc-pay" in e.value.reason
    assert "tok_9d41c7e2a0b84f13" not in e.value.reason and "Tracker or Code host" in e.value.reason
    assert not (tmp_path / "sandbox" / "etc").exists(), "a configuration holding the credential was written"


def test_a_secret_named_for_a_dependency_repos_server_that_the_product_lacks_is_needs_setup_naming_it(tmp_path):
    store = SecretStore(write_secrets(tmp_path / "s", PRODUCT, {"OTHER": "o"}).parent)
    (tmp_path / "sandbox").mkdir()
    worker, run = external_worker(tmp_path, {}, store)
    run.request = type("R", (), {"product": PRODUCT})()
    with pytest.raises(EnvironmentBringUpError) as e:
        worker._start_external_mcp_servers(run, LocalSandbox(tmp_path / "sandbox"), [EnvironmentStub("dep", [STDIO])], (), ())
    assert e.value.outcome is Outcome.NEEDS_SETUP and "dep" in e.value.reason and "PAY_TEST_KEY" in e.value.reason
    assert PRODUCT in e.value.reason


def test_a_missing_secret_of_a_dependency_repos_server_stops_the_run_before_any_repos_services_start(tmp_path):
    from types import SimpleNamespace

    store = SecretStore(write_secrets(tmp_path / "s", PRODUCT, {"OTHER": "o"}).parent)
    worker, run = external_worker(tmp_path, {}, store)
    run.request = type("R", (), {"product": PRODUCT})()

    class UntouchedSandbox:
        def __getattr__(self, name):
            raise AssertionError(f"the sandbox was used ({name}) before the missing secret was found")

    placed = [
        (SimpleNamespace(repo="client", path="/x"), parse_recipe("client", recipe_text(None))),
        (SimpleNamespace(repo="dep", path="/y"), parse_recipe("dep", recipe_text([STDIO]))),
    ]
    with pytest.raises(EnvironmentBringUpError) as e:
        worker._bring_up_placed(run, UntouchedSandbox(), placed, [])
    assert e.value.outcome is Outcome.NEEDS_SETUP and "dep" in e.value.reason and "PAY_TEST_KEY" in e.value.reason


def test_a_servers_secret_is_read_from_the_products_own_file_and_scrubbed_from_everything_the_run_writes(tmp_path):
    store = SecretStore(write_secrets(tmp_path / "s", PRODUCT, {"PAY_TEST_KEY": PAY_KEY, "UNNAMED": "never-given"}).parent)
    (tmp_path / "sandbox").mkdir()
    worker, run = external_worker(tmp_path, {}, store)
    run.request = type("R", (), {"product": PRODUCT})()
    sandbox = LocalSandbox(tmp_path / "sandbox")
    worker._start_external_mcp_servers(run, sandbox, [EnvironmentStub("svc", [STDIO])], (), ())
    assert json.loads(sandbox.managed.read_text())["mcpServers"]["svc-pay"]["env"] == {"PAY_TEST_KEY": PAY_KEY}
    assert run.test_secrets == {"svc": {"PAY_TEST_KEY": PAY_KEY}}
    worker._note(run.record, f"a failure printed {PAY_KEY}")
    assert PAY_KEY not in (worker._run_dir("r1") / "run.log").read_text()


def test_the_secrets_an_external_server_uses_do_not_reach_the_recipes_own_services(tmp_path):
    from workflow_weave.agent_worker.environment import compose_override

    text = recipe_text([STDIO], extra={"secrets": ["SERVICE_KEY"]})
    recipe = parse_recipe("svc", text)
    worker, run = external_worker(tmp_path, {"svc": {"SERVICE_KEY": "s", "PAY_TEST_KEY": PAY_KEY}})
    assert worker._secrets_for_recipe(run, "svc", recipe) == {"SERVICE_KEY": "s"}
    assert PAY_KEY not in compose_override("svc", recipe) and "PAY_TEST_KEY" not in compose_override("svc", recipe)
    assert worker.record("r1").test_secrets_given == {"svc": ["SERVICE_KEY"]}


def test_a_record_saved_before_external_servers_existed_loads_with_none_listed():
    rec = RunRecord(run_id="r", product="p", agent_profile="a", subscription="s", skill="k", repos=[],
                    state=RunState.ENDED, started_at="t",
                    external_mcp_servers=[{"name": "svc-pay"}], repo_mcp_config_ignored=["svc"])
    assert RunRecord.from_json(rec.to_json()).external_mcp_servers == [{"name": "svc-pay"}]
    d = json.loads(rec.to_json())
    del d["external_mcp_servers"], d["repo_mcp_config_ignored"]
    loaded = RunRecord.from_json(json.dumps(d))
    assert loaded.external_mcp_servers is None and loaded.repo_mcp_config_ignored is None


# ---- a recipe naming a Test secret the Product lacks stops the run as needs-setup, as for any secret

EXTERNAL_RECIPE = """\
x-weave:
  secrets: [SERVICE_KEY]
  external_mcp:
    - name: pay
      command: npx
      args: [pay-mcp]
      secrets: [PAY_TEST_KEY]
"""


def test_an_external_servers_secret_the_product_lacks_gives_needs_setup_naming_the_repo_and_the_secret_and_no_sandbox_starts(
    make_worker, tmp_path, stand_in_docker
):
    # SERVICE_KEY is there; only the External MCP server's secret is missing.
    store = SecretStore(write_secrets(tmp_path / "s", PRODUCT, {"SERVICE_KEY": "s", "OTHER": "o"}).parent)
    worker = worker_for(make_worker, tmp_path, store, recipe=EXTERNAL_RECIPE)
    try:
        result = worker.start(probe_request({"end": "succeed"}, repos=["svc"]))
    finally:
        worker.shutdown()
    assert isinstance(result, NeedsSetup) and result.outcome is Outcome.NEEDS_SETUP
    assert "svc" in result.reason and "PAY_TEST_KEY" in result.reason and PRODUCT in result.reason
    assert "SERVICE_KEY" not in result.reason
    assert not [c for c in stand_in_docker() if c[:1] in (["run"], ["create"])], "a sandbox was started"


def test_an_external_servers_secret_the_product_has_lets_the_run_start(make_worker, tmp_path, stand_in_docker):
    store = SecretStore(write_secrets(tmp_path / "s", PRODUCT, {"SERVICE_KEY": "s", "PAY_TEST_KEY": PAY_KEY}).parent)
    worker = worker_for(make_worker, tmp_path, store, recipe=EXTERNAL_RECIPE)
    try:
        result = worker.start(probe_request({"end": "succeed"}, repos=["svc"]))
        assert isinstance(result, Started)
        wait_until_ended(worker, result.run_id, timeout=30)
    finally:
        worker.shutdown()


# ---- a Repo's own committed MCP config is ignored, and the run's log notes it


def mcp_config_run(make_worker, tmp_path, files):
    from conftest import product_yaml_for, repo_with_recipe

    repo = repo_with_recipe(tmp_path / "recipe-repos", "svc", "x-weave: {}\n", extra_files=files)
    worker = make_worker(product_yaml=product_yaml_for({"svc": repo}, PRODUCT))
    started_run = worker.start(probe_request({"end": "succeed"}, repos=["svc"]))
    assert isinstance(started_run, Started)
    wait_until_ended(worker, started_run.run_id, timeout=30)
    return worker, started_run.run_id


@pytest.fixture
def staged_mcp(monkeypatch, sandbox_stand_in):
    from workflow_weave.agent_worker import worker as worker_module

    calls: list[dict] = []
    monkeypatch.setattr(worker_module, "stage_mcp_servers", lambda sandbox, servers: calls.append(dict(servers)))
    return calls


REPO_MCP = json.dumps({"mcpServers": {"repo-evil": {"type": "http", "url": "https://evil.example.test/mcp"}}})


def test_a_repos_committed_mcp_config_is_not_used_and_the_runs_log_notes_that_it_was_ignored(
    make_worker, tmp_path, stand_in_docker, staged_mcp
):
    worker, run_id = mcp_config_run(make_worker, tmp_path, {".mcp.json": REPO_MCP})
    try:
        record = worker.record(run_id)
        log = (worker._run_dir(run_id) / "run.log").read_text()
    finally:
        worker.shutdown()
    # Nothing the Repo committed was staged: the managed configuration is written (empty) before
    # anything else, and it is what stops Claude Code loading the Repo's own.
    assert staged_mcp and staged_mcp[0] == {}
    assert "evil.example.test" not in json.dumps(staged_mcp)
    assert "Repo 'svc' commits an MCP config (.mcp.json) that this run ignores" in log
    assert "only Environment MCP servers and External MCP servers" in log
    assert record.repo_mcp_config_ignored == ["svc"]


def test_a_repo_with_no_mcp_config_has_no_line_about_one_but_still_gets_the_exclusive_configuration(
    make_worker, tmp_path, stand_in_docker, staged_mcp
):
    worker, run_id = mcp_config_run(make_worker, tmp_path, {})
    try:
        record = worker.record(run_id)
        log = (worker._run_dir(run_id) / "run.log").read_text()
    finally:
        worker.shutdown()
    assert staged_mcp and staged_mcp[0] == {}, "a Repo could add a .mcp.json on its branch later"
    assert "ignores" not in log and record.repo_mcp_config_ignored is None
