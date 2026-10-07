"""Seeding in dependency order (#53).

Pure logic with a fake sandbox and no Docker. (The same behaviour on a real sysbox sandbox is in
test_environment_seeding_on_sysbox.py.) Each test is named after an acceptance criterion of #53.

Every Environment starts from empty databases (ADR 0011's tracked Seed scripts are Spec 2b): a
recipe's `x-weave.seed` is a service and a command, run once per Repo after its services are ready.
"""

from __future__ import annotations

import json
import shlex

import pytest
import yaml
from test_environment_bring_up import FakeSandbox, environment

from workflow_weave.agent_worker import RunRecord, RunState
from workflow_weave.agent_worker.environment import (
    EnvironmentBringUpError,
    RecipeError,
    parse_recipe,
    seed_in_dependency_order,
)


def recipe_text(services=("db",), seed=None, depends_on=(), secrets=()):
    block = {"readiness": {s: {"command": ["true"]} for s in services}}
    if seed is not None:
        block["seed"] = seed
    if depends_on:
        block["depends_on"] = list(depends_on)
    if secrets:
        block["secrets"] = list(secrets)
    return yaml.safe_dump({"services": {s: {"image": "alpine"} for s in services}, "x-weave": block})


def up(repo, seed=None, script=None, services=("db",), depends_on=(), secrets=(), values=None):
    """A Repo brought up on a fake sandbox: (environment, recipe, sandbox)."""
    sb = FakeSandbox(script or (lambda c, s: (0, "")))
    env = environment(sb, repo)
    if values:
        env._secrets = dict(values)
    recipe = parse_recipe(repo, recipe_text(services, seed, depends_on, secrets))
    env.bring_up(recipe)
    return env, recipe, sb


SEED = {"service": "db", "command": "seed"}


def is_seed(command):
    return " exec -T " in command and command.endswith(" db sh -c seed")


# ---- the recipe names a service and a command; a Repo with nothing to seed omits the field

def test_a_recipe_names_a_seed_service_and_command_and_a_repo_with_nothing_to_seed_omits_it():
    r = parse_recipe("svc", recipe_text(seed={"service": "db", "command": "psql -f /seed.sql"}))
    assert r.seed.service == "db" and r.seed.command == ("sh", "-c", "psql -f /seed.sql")
    as_list = parse_recipe("svc", recipe_text(seed={"service": "db", "command": ["./seed", "--small"]}))
    assert as_list.seed.command == ("./seed", "--small")
    assert parse_recipe("svc", recipe_text()).seed is None


@pytest.mark.parametrize("seed, problem", [
    ("psql", "mapping"),
    ({"command": "x"}, "service"),
    ({"service": "ghost", "command": "x"}, "ghost"),
    ({"service": "db"}, "command"),
    ({"service": "db", "command": ""}, "command"),
    ({"service": "db", "command": "x", "timeout": 0}, "timeout"),
])
def test_a_seed_that_cannot_be_run_is_refused_naming_the_repo_and_what_to_fix(seed, problem):
    with pytest.raises(RecipeError) as e:
        parse_recipe("svc", recipe_text(seed=seed))
    assert "svc" in str(e.value) and "seed" in str(e.value) and problem in str(e.value)


# ---- a Repo with no seed command is brought up without error

def test_a_repo_with_no_seed_command_is_brought_up_and_passed_over_without_error():
    env, _, sb = up("svc")
    before = list(sb.commands)
    assert seed_in_dependency_order([env]) == []
    assert env.seed() is None
    assert sb.commands == before


def test_a_repo_with_no_services_and_only_dependencies_has_nothing_to_seed():
    sb = FakeSandbox(lambda c, s: (0, ""))
    env = environment(sb, "client")
    env.bring_up(parse_recipe("client", "x-weave:\n  depends_on: [svc]\n"))
    assert seed_in_dependency_order([env]) == [] and sb.commands == []


# ---- the seed command runs in the Repo's own service container, on its own project

def test_the_seed_command_runs_in_the_named_service_of_the_repos_own_project_and_nowhere_else():
    env, _, sb = up("svc", seed={"service": "db", "command": "psql -f /seed.sql"})
    n = len(sb.commands)
    run = env.seed()
    (cmd,) = sb.commands[n:]
    argv = shlex.split(cmd)
    assert argv[:5] == ["docker", "compose", "-p", "svc", "-f"]
    assert argv[argv.index("exec"):] == ["exec", "-T", "db", "sh", "-c", "psql -f /seed.sql"]
    assert run.repo == "svc" and run.service == "db"


def test_a_repos_seed_command_cannot_change_another_repos_databases_it_runs_in_its_own_container_with_only_its_own_credentials():
    svc, _, svc_sb = up("svc", SEED, secrets=["SVC_KEY"], values={"SVC_KEY": "svc-secret-value"})
    other, _, other_sb = up("other", SEED, secrets=["OTHER_KEY"], values={"OTHER_KEY": "other-secret-value"})
    n_svc, n_other = len(svc_sb.commands), len(other_sb.commands)
    seed_in_dependency_order([svc, other])
    (svc_cmd,), (other_cmd,) = svc_sb.commands[n_svc:], other_sb.commands[n_other:]
    # Each command targets its own project and carries its own Repo's secrets and no other's.
    assert " -p svc " in svc_cmd and "other" not in svc_cmd.lower()
    assert " -p other " in other_cmd and "svc" not in other_cmd.lower()
    assert "svc-secret-value" in svc_cmd and "other-secret-value" not in svc_cmd
    assert "other-secret-value" in other_cmd and "svc-secret-value" not in other_cmd


def test_secrets_the_recipe_names_reach_the_seed_command_and_no_other_secret_does():
    env, _, sb = up("svc", SEED, secrets=["API_KEY"], values={"API_KEY": "k-123", "UNNAMED": "nope"})
    n = len(sb.commands)
    env.seed()
    (cmd,) = sb.commands[n:]
    assert cmd.startswith("API_KEY=k-123 ") and " -e API_KEY " in cmd
    assert "UNNAMED" not in cmd and "nope" not in cmd


# ---- run only once the Repo's services are ready, in dependency order

def test_a_seed_command_is_run_only_once_its_repos_services_are_ready():
    sb = FakeSandbox(lambda c, s: (0, ""))
    env = environment(sb, "svc")
    with pytest.raises(EnvironmentBringUpError) as e:
        env.seed()  # nothing brought up yet
    assert "svc" in str(e.value) and "ready" in str(e.value)

    # Brought up, but one of its two services never passed its readiness check.
    env.recipe = parse_recipe("svc", recipe_text(services=("web", "db"), seed=SEED))
    with pytest.raises(EnvironmentBringUpError):
        env.seed()
    assert sb.commands == []


def test_every_repo_is_ready_before_any_is_seeded():
    ready_env, _, sb = up("svc", SEED)
    n = len(sb.commands)
    never_brought_up = environment(FakeSandbox(lambda c, s: (0, "")), "client")
    with pytest.raises(EnvironmentBringUpError):
        seed_in_dependency_order([ready_env, never_brought_up])
    assert sb.commands[n:] == [] and ready_env.seeded is None, "a Repo was seeded while a sibling was not ready"


def script_noting(order, repo):
    def script(command, sb):
        if is_seed(command):
            order.append(repo)
        return 0, ""
    return script


def test_a_repo_that_depends_on_another_is_seeded_after_it_and_each_is_seeded_once():
    order = []
    svc, _, _ = up("svc", SEED, script_noting(order, "svc"))
    client, _, _ = up("client", SEED, script_noting(order, "client"), depends_on=["svc"])
    runs = seed_in_dependency_order([svc, client])  # the order resolve_environment gives
    assert order == ["svc", "client"]
    assert [r.repo for r in runs] == ["svc", "client"]


def test_seeding_can_be_run_again_through_the_same_function_for_a_reset():
    order = []
    envs = [up("a", SEED, script_noting(order, "a"))[0], up("b", SEED, script_noting(order, "b"), depends_on=["a"])[0]]
    seed_in_dependency_order(envs)
    seed_in_dependency_order(envs)  # what `reset` does once everything is up fresh
    assert order == ["a", "b", "a", "b"]


# ---- failures

def test_a_seed_command_that_fails_stops_the_run_naming_the_repo_service_and_output_without_secret_values():
    def script(command, sb):
        if is_seed(command):
            return 3, "ERROR: relation users does not exist (password=hunter2)\n"
        return 0, ""

    env, _, _ = up("svc", SEED, script, secrets=["DB_PASSWORD"], values={"DB_PASSWORD": "hunter2"})
    with pytest.raises(EnvironmentBringUpError) as e:
        env.seed()
    reason = str(e.value)
    assert "svc" in reason and "db" in reason and "relation users does not exist" in reason
    assert "hunter2" not in reason
    assert env.seeded is None


def test_a_seed_that_fails_leaves_the_later_repos_unseeded():
    first, _, _ = up("svc", SEED, lambda c, s: (1, "boom") if is_seed(c) else (0, ""))
    second, _, sb = up("client", SEED, depends_on=["svc"])
    n = len(sb.commands)
    with pytest.raises(EnvironmentBringUpError):
        seed_in_dependency_order([first, second])
    assert sb.commands[n:] == [] and second.seeded is None


# ---- the run record exposes the seed

def test_the_run_record_shows_each_repos_seed_without_secret_values():
    env, _, _ = up("svc", {"service": "db", "command": "psql -f /seed.sql"}, secrets=["K"], values={"K": "v-1"})
    (run,) = seed_in_dependency_order([env])
    entry = run.as_record()
    assert entry["repo"] == "svc" and entry["service"] == "db" and entry["command"] == "psql -f /seed.sql"
    assert entry["seeded_at"].startswith("2026-10-08T") and entry["seconds"] >= 0
    rec = RunRecord(run_id="r", product="p", agent_profile="a", subscription="s", skill="k", repos=[],
                    state=RunState.ENDED, started_at="t", environment_seeds=[entry])
    assert RunRecord.from_json(rec.to_json()).environment_seeds == [entry]
    assert "v-1" not in rec.to_json()


def test_a_record_saved_before_seeding_existed_loads_with_no_seeds_listed():
    rec = RunRecord(run_id="r", product="p", agent_profile="a", subscription="s", skill="k", repos=[],
                    state=RunState.ENDED, started_at="t")
    d = json.loads(rec.to_json())
    del d["environment_seeds"]
    assert RunRecord.from_json(json.dumps(d)).environment_seeds is None
