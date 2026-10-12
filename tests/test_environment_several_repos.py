"""Several Repos in one Environment (#50).

Pure logic with a fake sandbox and no Docker: which Repos are in the Environment and in what order,
which branch each runs from, and the commands that give every service its `<service>.<repo>`
address and no other. (The same behaviour on a real sysbox sandbox is in
test_environment_several_repos_on_sysbox.py.) Each test is named after an acceptance criterion of #50.
"""

from __future__ import annotations

import json
import shlex

import pytest
import yaml
from test_environment_bring_up import FakeSandbox, environment

from workflow_weave.agent_worker import RepoTarget, RunRecord, RunState
from workflow_weave.agent_worker.environment import (
    EnvironmentBringUpError,
    RecipeError,
    parse_recipe,
    resolve_environment,
)

DB_RECIPE = """\
services:
  db: {image: postgres:16-alpine}
x-weave:
  readiness:
    db: {command: [pg_isready]}
"""


def recipe(repo, *, depends_on=(), services=()):
    doc = {"x-weave": {"readiness": {s: {"command": ["true"]} for s in services}}}
    if depends_on:
        doc["x-weave"]["depends_on"] = list(depends_on)
    doc["services"] = {s: {"image": "alpine"} for s in services}
    return parse_recipe(repo, yaml.safe_dump(doc))


def target(name, integration=None):
    return RepoTarget(name, integration or f"story-{name}")


def resolve(touched, recipes, working_on=None, base_branches=None):
    """Resolve with `recipes` (repo -> Recipe) standing in for what the checkouts hold."""
    loaded = []

    def load(placement):
        loaded.append(placement.repo)
        return recipes.get(placement.repo)

    placed = resolve_environment(
        {t.name: t for t in touched}, working_on or touched[0].name, lambda r: (base_branches or {}).get(r, "main"), load
    )
    return placed, loaded


# ---- the recipe names the Repos it depends on

def test_a_recipe_names_the_repos_it_depends_on_and_may_have_no_services_of_its_own():
    r = parse_recipe("client", "x-weave:\n  depends_on: [svc, auth]\n")
    assert r.depends_on == ("svc", "auth") and r.services == ()
    assert parse_recipe("svc", DB_RECIPE).depends_on == ()


@pytest.mark.parametrize("value", ["svc", [1], [""], {"a": 1}])
def test_a_depends_on_that_is_not_a_list_of_repo_names_is_refused_naming_the_repo(value):
    with pytest.raises(RecipeError) as e:
        parse_recipe("client", yaml.safe_dump({"x-weave": {"depends_on": value}}))
    assert "client" in str(e.value) and "depends_on" in str(e.value)


# ---- a Repo named in depends_on is brought up, dependencies first

def test_a_repo_named_in_depends_on_is_brought_up_at_its_base_branch_even_though_the_run_does_not_touch_it():
    placed, _ = resolve(
        [target("client")],
        {"client": recipe("client", depends_on=["svc"]), "svc": recipe("svc", services=["web"])},
        base_branches={"svc": "develop"},
    )
    assert [(p.repo, p.source, p.branch) for p, _ in placed] == [
        ("svc", "Base branch", "develop"),
        ("client", "working copy", "story-client"),
    ]


def test_dependencies_come_up_before_the_repos_that_need_them_and_a_shared_one_only_once():
    recipes = {
        "app": recipe("app", depends_on=["api", "auth"]),
        "api": recipe("api", depends_on=["auth"], services=["web"]),
        "auth": recipe("auth", depends_on=["db"], services=["login"]),
        "db": recipe("db", services=["pg"]),
    }
    placed, _ = resolve([target("app")], recipes)
    assert [p.repo for p, _ in placed] == ["db", "auth", "api", "app"]


def test_a_dependency_cycle_is_refused_naming_the_repos_in_it():
    recipes = {"a": recipe("a", depends_on=["b"]), "b": recipe("b", depends_on=["c"]), "c": recipe("c", depends_on=["a"])}
    with pytest.raises(RecipeError) as e:
        resolve([target("a")], recipes)
    assert "a -> b -> c -> a" in str(e.value)


def test_a_repo_that_depends_on_itself_is_a_cycle():
    with pytest.raises(RecipeError, match="a -> a"):
        resolve([target("a")], {"a": recipe("a", depends_on=["a"])})


def test_a_dependency_with_no_run_recipe_is_refused_naming_both_repos():
    with pytest.raises(RecipeError) as e:
        resolve([target("client")], {"client": recipe("client", depends_on=["svc"])})
    assert "svc" in str(e.value) and "client" in str(e.value) and "Run recipe" in str(e.value)


def test_a_touched_repo_with_no_run_recipe_is_left_out_without_an_error():
    placed, _ = resolve([target("docs"), target("svc")], {"svc": recipe("svc", services=["web"])}, working_on="docs")
    assert [p.repo for p, _ in placed] == ["svc"]


def test_a_recipe_with_no_services_of_its_own_and_only_dependencies_still_gives_an_environment():
    placed, _ = resolve(
        [target("client")], {"client": recipe("client", depends_on=["svc"]), "svc": recipe("svc", services=["web"])}
    )
    assert [p.repo for p, _ in placed] == ["svc", "client"]
    assert placed[-1][1].services == ()


# ---- which branch each Repo runs from

def test_the_repo_worked_on_runs_from_its_working_copy_another_touched_repo_from_its_task_branch_an_untouched_one_from_base():
    recipes = {
        "web": recipe("web", depends_on=["api", "billing"], services=["ui"]),
        "api": recipe("api", services=["srv"]),
        "billing": recipe("billing", services=["srv"]),
    }
    placed, _ = resolve([target("web"), target("api", "story-42-api")], recipes, working_on="web")
    by_repo = {p.repo: p for p, _ in placed}
    assert (by_repo["web"].source, by_repo["web"].branch, by_repo["web"].path) == (
        "working copy", "story-web", "/workspace/web")
    assert (by_repo["api"].source, by_repo["api"].branch) == ("Task branch", "story-42-api")
    assert (by_repo["billing"].source, by_repo["billing"].branch) == ("Base branch", "main")
    # Only the worked-on Repo runs from the agent's own checkout; the others run from a checkout the
    # agent does not edit.
    assert by_repo["api"].path != "/workspace/api" and by_repo["billing"].path != "/workspace/billing"


def test_a_touched_repo_that_is_also_a_dependency_stays_on_its_task_branch_not_base():
    placed, _ = resolve(
        [target("client"), target("svc", "story-svc")],
        {"client": recipe("client", depends_on=["svc"]), "svc": recipe("svc", services=["web"])},
        working_on="client",
    )
    assert {p.repo: p.source for p, _ in placed} == {"client": "working copy", "svc": "Task branch"}


def test_the_run_record_shows_which_branch_each_repo_ran_from():
    placed, _ = resolve(
        [target("web"), target("api", "story-api")],
        {"web": recipe("web", depends_on=["db"]), "api": recipe("api"), "db": recipe("db", services=["pg"])},
        working_on="web",
    )
    entries = [p.as_record() for p, _ in placed]
    rec = RunRecord(run_id="r", product="p", agent_profile="a", subscription="s", skill="k", repos=[],
                    state=RunState.ENDED, started_at="t", environment_repos=entries)
    back = RunRecord.from_json(rec.to_json()).environment_repos
    assert {e["repo"]: (e["source"], e["branch"]) for e in back} == {
        "db": ("Base branch", "main"), "web": ("working copy", "story-web"), "api": ("Task branch", "story-api"),
    }
    assert {e["repo"]: e["touched"] for e in back} == {"db": False, "web": True, "api": True}


def test_a_record_saved_before_several_repos_existed_loads_with_no_branches_listed():
    rec = RunRecord(run_id="r", product="p", agent_profile="a", subscription="s", skill="k", repos=[],
                    state=RunState.ENDED, started_at="t")
    d = json.loads(rec.to_json())
    del d["environment_repos"]
    assert RunRecord.from_json(json.dumps(d)).environment_repos is None


# ---- two Repos that each define a service of the same name do not collide

def containers_script(ids):
    """`compose ps -q <service>` answers with that service's container ids."""
    def script(command, sb):
        if " ps " in command and " -q " in command:
            return 0, ids.get(command.split()[-1], "") + "\n"
        return 0, ""
    return script


def bring_up(repo, ids, services=("db",)):
    sb = FakeSandbox(containers_script(ids))
    environment(sb, repo).bring_up(recipe(repo, services=list(services)))
    return sb


def connects(sb):
    return [shlex.split(c) for c in sb.commands if c.startswith("docker network connect")]


def test_two_repos_that_each_define_a_service_of_the_same_name_do_not_collide():
    one = bring_up("billing", {"db": "c1"})
    two = bring_up("orders", {"db": "c2"})
    # Each is its own Compose project, so the containers are separate...
    assert any("compose -p billing " in c for c in one.commands) and any("compose -p orders " in c for c in two.commands)
    # ...and on the shared network each is attached with exactly its own `<service>.<repo>` alias.
    assert connects(one) == [["docker", "network", "connect", "--alias", "db.billing", "weave-env", "c1"]]
    assert connects(two) == [["docker", "network", "connect", "--alias", "db.orders", "weave-env", "c2"]]


def test_no_service_is_given_its_bare_name_on_the_shared_network_where_two_repos_would_clash_on_it():
    sb = bring_up("billing", {"db": "c1"})
    # Compose adds a service's bare name as an alias on every network it attaches it to, so the
    # shared network is never named to Compose: it is joined only by `docker network connect --alias`.
    for cmd in sb.commands:
        assert not cmd.startswith("docker compose") or "weave-env" not in cmd, cmd
    for override in sb.files.values():
        assert "weave-env" not in override
    (connect,) = connects(sb)
    assert [connect[i + 1] for i, a in enumerate(connect) if a == "--alias"] == ["db.billing"]


def test_services_are_attached_to_the_shared_network_before_they_start_so_a_service_can_reach_a_sibling_repo_at_start_up():
    cmds = bring_up("billing", {"db": "c1"}).commands
    create = next(i for i, c in enumerate(cmds) if " up " in c and "--no-start" in c and "--build" in c)
    connect = next(i for i, c in enumerate(cmds) if c.startswith("docker network connect"))
    start = next(i for i, c in enumerate(cmds) if " up " in c and "--no-recreate" in c and " -d" in c)
    ready = next(i for i, c in enumerate(cmds) if " exec -T db " in c)
    assert create < connect < start < ready


def test_every_container_of_a_service_gets_the_address_and_a_service_with_no_container_is_a_bring_up_failure():
    assert [c[-1] for c in connects(bring_up("svc", {"db": "c1\nc2"}))] == ["c1", "c2"]
    with pytest.raises(EnvironmentBringUpError) as e:
        bring_up("svc", {})
    assert "svc" in str(e.value) and "db" in str(e.value)


def test_a_failed_attach_names_the_repo_and_service():
    def script(command, sb):
        if " ps " in command:
            return 0, "c1\n"
        if command.startswith("docker network connect"):
            return 1, "network weave-env not found"
        return 0, ""

    with pytest.raises(EnvironmentBringUpError) as e:
        environment(FakeSandbox(script), "svc").bring_up(recipe("svc", services=["db"]))
    assert "svc" in str(e.value) and "db" in str(e.value) and "weave-env not found" in str(e.value)


# ---- the same address in every run, reachable from the sandbox too

def test_the_address_of_a_service_is_the_same_whatever_the_run():
    first = bring_up("svc", {"db": "abc123"})
    second = bring_up("svc", {"db": "ffff99"})
    assert [c[4] for c in connects(first)] == [c[4] for c in connects(second)] == ["db.svc"]


def test_the_sandbox_shell_can_resolve_each_service_address_too():
    def script(command, sb):
        if " ps " in command:
            return 0, "c1\n"
        if command.startswith("docker inspect"):
            return 0, "172.20.0.5\n"
        return 0, ""

    sb = FakeSandbox(script)
    environment(sb, "svc").bring_up(recipe("svc", services=["db"]))
    hosts = [c for c in sb.commands if "/etc/hosts" in c]
    assert len(hosts) == 1 and "172.20.0.5" in hosts[0] and "db.svc" in hosts[0]
