"""`weave-env rebuild` and `weave-env reset` (#54), with the same fakes as test_weave_env.py.

Each test is named after an acceptance criterion of #54.
"""

from __future__ import annotations

import shlex

from test_weave_env import CLIENT, SVC, Shell, manifest_for, weave


def argv_of(command):
    """The `docker ...` argv of a command, without any NAME=value prefix for secrets."""
    parts = shlex.split(command)
    while parts and "=" in parts[0] and not parts[0].startswith("-"):
        parts.pop(0)
    return parts


def compose_commands(shell, verb, project="svc"):
    return [c for c in shell.commands
            if argv_of(c)[:4] == ["docker", "compose", "-p", project] and verb in argv_of(c)]


# ---- rebuild: that Repo's project from the working copy, its data kept

def test_rebuild_rebuilds_that_repos_project_from_its_working_copy_and_touches_no_other(tmp_path):
    code, out, shell, _ = weave(tmp_path, "rebuild", "svc")
    assert code == 0, out
    (create,) = [c for c in compose_commands(shell, "up") if "--no-start" in c]
    argv = argv_of(create)
    assert "--build" in argv and argv[argv.index("-f") + 1] == "/workspace/svc/.weave/compose.yaml"
    assert [c for c in compose_commands(shell, "up") if "up -d" in c]
    assert not any(argv_of(c)[:4] == ["docker", "compose", "-p", "client"] for c in shell.commands)


def test_rebuild_keeps_the_repos_data_it_never_removes_a_container_volume_or_the_project(tmp_path):
    _, _, shell, _ = weave(tmp_path, "rebuild", "svc")
    joined = "\n".join(shell.commands)
    for destructive in (" down", " rm ", "volume", " -v", "--force-recreate", "--renew-anon-volumes"):
        assert destructive not in joined
    # And it does not seed again: the Repo's data is as it was.
    assert not any("psql" in c for c in shell.commands)


def test_rebuild_joins_the_environment_network_again_and_waits_until_every_service_of_the_repo_is_ready(tmp_path):
    attempts = {"n": 0}

    def script(command):
        if "pg_isready" in command and " exec " in command:
            attempts["n"] += 1
            return (0, "") if attempts["n"] >= 3 else (1, "starting")
        return None

    code, out, shell, _ = weave(tmp_path, "rebuild", "svc", shell=Shell(script))
    assert code == 0, out
    connects = [argv_of(c) for c in shell.commands if " network connect " in c]
    assert sorted(a[a.index("--alias") + 1] for a in connects) == ["db.svc", "web.svc"]
    assert attempts["n"] == 3
    assert "ready" in out


def test_rebuild_names_the_services_in_the_sandbox_again_without_duplicating_the_old_names(tmp_path):
    _, _, _, hosts = weave(
        tmp_path, "rebuild", "svc",
        hosts="127.0.0.1 localhost\n10.0.0.2 web.svc\n10.0.0.3 db.svc\n10.0.0.4 api.other\n",
    )
    lines = hosts.read_text().splitlines()
    assert lines.count("10.0.0.5 web.svc") == 1 and lines.count("10.0.0.5 db.svc") == 1
    assert not any(line.startswith(("10.0.0.2", "10.0.0.3")) for line in lines)
    assert "10.0.0.4 api.other" in lines and "127.0.0.1 localhost" in lines


def test_rebuild_starts_containers_with_the_recipes_named_secrets_or_they_would_be_dropped(tmp_path):
    _, _, shell, _ = weave(tmp_path, "rebuild", "svc")
    ups = compose_commands(shell, "up")
    assert ups
    for c in ups:
        assert c.startswith("DB_PASSWORD=hunter2 docker compose"), c
        # Only the named secret, never one a container merely happens to have in its environment.
        assert "PATH=" not in c


def test_rebuild_that_cannot_start_a_service_says_which_repo_and_shows_the_output_without_secret_values(tmp_path):
    def script(command):
        if " up " in command and "--no-start" in command:
            return 1, "service web: build failed (password hunter2)\n"
        return None

    code, out, _, _ = weave(tmp_path, "rebuild", "svc", shell=Shell(script))
    assert code != 0 and "svc" in out and "build failed" in out and "hunter2" not in out


def test_rebuild_that_never_becomes_ready_says_which_service_and_its_last_log_lines(tmp_path):
    def script(command):
        if " exec " in command and "curl" in command:
            return 1, ""
        if " logs " in command:
            return 0, "web | Traceback: boom\n"
        return None

    code, out, _, _ = weave(tmp_path, "rebuild", "svc", shell=Shell(script))
    assert code != 0 and "'web'" in out and "not ready" in out and "boom" in out


def test_rebuild_of_a_repo_whose_secrets_cannot_be_recovered_stops_before_changing_anything(tmp_path):
    def script(command):
        if command.startswith("docker inspect") and "Config.Env" in command:
            return 0, "PATH=/bin\n"
        return None

    code, out, shell, _ = weave(tmp_path, "rebuild", "svc", shell=Shell(script))
    assert code != 0 and "DB_PASSWORD" in out and "hunter2" not in out
    assert not compose_commands(shell, "up")


# ---- reset: tear down, fresh bring-up, re-seeded in dependency order

OTHER = """\
services:
  api: {image: python:3.12-slim}
x-weave:
  depends_on: [svc]
  readiness:
    api: {command: 'true'}
  seed: {service: api, command: 'load-data'}
"""
ALL = (("svc", SVC), ("other", OTHER), ("client", CLIENT))


def reset(tmp_path, script=None, hosts="127.0.0.1 localhost\n"):
    return weave(tmp_path, "reset", shell=Shell(script), manifest=manifest_for(tmp_path, ALL), hosts=hosts)


def test_reset_tears_every_project_down_with_its_data_dependents_first_then_brings_them_up_fresh(tmp_path):
    code, out, shell, _ = reset(tmp_path)
    assert code == 0, out
    downs = [(argv_of(c)[3], argv_of(c)[-3:]) for c in shell.commands if " down " in c]
    assert downs == [("other", ["down", "-v", "--remove-orphans"]), ("svc", ["down", "-v", "--remove-orphans"])]
    last_down = max(i for i, c in enumerate(shell.commands) if " down " in c)
    first_up = next(i for i, c in enumerate(shell.commands) if "--no-start" in c)
    assert first_up > last_down
    # Dependencies first; the Repo with no services starts nothing.
    assert [argv_of(c)[3] for c in shell.commands if "--no-start" in c] == ["svc", "other"]


def test_reset_re_seeds_in_dependency_order_only_after_every_repo_is_ready(tmp_path):
    code, out, shell, _ = reset(tmp_path)
    assert code == 0, out
    seeds = [i for i, c in enumerate(shell.commands) if "psql" in c or "load-data" in c]
    assert len(seeds) == 2
    assert "psql" in shell.commands[seeds[0]] and "load-data" in shell.commands[seeds[1]]
    checks = [i for i, c in enumerate(shell.commands) if " exec " in c and i not in seeds]
    assert checks and max(checks) < seeds[0]
    # A seed runs in its own Repo's project with the secrets its recipe names, and only those.
    first = shell.commands[seeds[0]]
    assert first.startswith("DB_PASSWORD=hunter2 docker compose -p svc") and "-e DB_PASSWORD" in first
    assert shell.commands[seeds[1]].startswith("docker compose -p other")


def test_reset_starts_containers_with_the_recipes_named_secrets_and_reads_them_before_tearing_down(tmp_path):
    _, _, shell, _ = reset(tmp_path)
    first_down = next(i for i, c in enumerate(shell.commands) if " down " in c)
    assert any("Config.Env" in c for c in shell.commands[:first_down])
    ups = [c for c in shell.commands if "docker compose -p svc" in c and " up " in c]
    assert ups and all(c.startswith("DB_PASSWORD=hunter2 ") for c in ups)


def test_reset_leaves_one_name_per_service_in_the_sandbox_hosts_file(tmp_path):
    _, _, _, hosts = reset(tmp_path, hosts="127.0.0.1 localhost\n10.0.0.2 web.svc\n10.0.0.2 api.other\n")
    lines = hosts.read_text().splitlines()
    for name in ("web.svc", "db.svc", "api.other"):
        assert sum(line.endswith(" " + name) for line in lines) == 1
    assert "127.0.0.1 localhost" in lines


def test_reset_that_fails_to_seed_says_which_repo_and_stops(tmp_path):
    def script(command):
        if "load-data" in command:
            return 3, "relation missing (hunter2)\n"
        return None

    code, out, _, _ = reset(tmp_path, script)
    assert code != 0 and "other" in out and "seed" in out and "relation missing" in out and "hunter2" not in out


def test_reset_that_cannot_bring_a_repo_up_says_so_and_does_not_seed(tmp_path):
    def script(command):
        if "--no-start" in command and "-p other" in command:
            return 1, "no such image\n"
        return None

    code, out, shell, _ = reset(tmp_path, script)
    assert code != 0 and "other" in out and "no such image" in out
    assert not any("psql" in c or "load-data" in c for c in shell.commands)


def test_reset_of_an_environment_whose_repos_have_no_services_succeeds_having_done_nothing(tmp_path):
    shell = Shell()
    path = manifest_for(tmp_path, (("client", "x-weave:\n  depends_on: [svc]\n"),))
    code, _, _, _ = weave(tmp_path, "reset", shell=shell, manifest=path)
    assert code == 0 and not any("docker compose" in c for c in shell.commands)


# ---- with the Environment MCP servers (#55): they address databases by `<service>.<repo>`

def test_rebuild_and_reset_rejoin_the_network_under_the_address_an_environment_mcp_server_uses(tmp_path):
    from workflow_weave.agent_worker.mcp import plan_environment_servers

    plan = plan_environment_servers(
        [("svc", [parse_db("db")])], {"postgres"},
    )
    (server,) = plan.started
    for verb in (["rebuild", "svc"], ["reset"]):
        _, _, shell, _ = weave(tmp_path, *verb)
        connects = [argv_of(c) for c in shell.commands if argv_of(c)[:3] == ["docker", "network", "connect"]]
        assert any(a[a.index("--alias") + 1] == server.address for a in connects), verb


def parse_db(service):
    from workflow_weave.agent_worker.mcp import DatabaseSpec

    return DatabaseSpec(service=service, kind="postgres", port=5432, user="u", password="p", database="d")


def test_a_readiness_timeout_counts_from_after_up_returns_not_from_before_it_built_the_image(tmp_path):
    attempts = {"n": 0}
    shell = Shell()

    def script(command):
        if " up " in command and "--no-start" in command:
            shell.now += 300  # a slow build must not use up the 10s readiness timeout
        if "pg_isready" in command and " exec " in command:
            attempts["n"] += 1
            return (0, "") if attempts["n"] >= 3 else (1, "starting")
        return None

    shell.script = script
    code, out, _, _ = weave(tmp_path, "rebuild", "svc", shell=shell)
    assert code == 0, out


def _manifest_as_the_worker_writes_it(tmp_path):
    """Three Repos: `dep` (not touched, pulled in by depends_on: Base checkout), `other` (touched,
    runs from its Task branch checkout) and `app` (the one being worked on: its working copy)."""
    import json

    from test_weave_env import FakeSandbox
    from workflow_weave.agent_worker.environment import Environment, Placement, environment_manifest, parse_recipe

    recipe = "services:\n  web: {image: alpine}\nx-weave:\n  readiness:\n    web: {command: 'true'}\n"
    places = [
        Placement("dep", "Base branch", "main", "/weave/env/src/dep", False),
        Placement("other", "Task branch", "story", "/weave/env/src/other", True),
        Placement("app", "working copy", "story", "/workspace/app", True),
    ]
    environments = []
    for place in places:
        env = Environment(FakeSandbox(), place.repo, now=lambda: "t", working_copy=place.path)
        env.bring_up(parse_recipe(place.repo, recipe))
        environments.append(env)
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(environment_manifest(places, environments)))
    return path


def test_rebuild_of_a_touched_repo_other_than_the_one_worked_on_builds_from_the_agents_working_copy(tmp_path):
    path = _manifest_as_the_worker_writes_it(tmp_path)
    code, out, shell, _ = weave(tmp_path, "rebuild", "other", manifest=path)
    assert code == 0, out
    (create,) = [c for c in compose_commands(shell, "up", "other") if "--no-start" in c]
    assert argv_of(create)[argv_of(create).index("-f") + 1] == "/workspace/other/.weave/compose.yaml"


def test_rebuild_of_a_repo_the_story_does_not_touch_has_no_working_copy_and_builds_from_where_it_runs(tmp_path):
    path = _manifest_as_the_worker_writes_it(tmp_path)
    code, out, shell, _ = weave(tmp_path, "rebuild", "dep", manifest=path)
    assert code == 0, out
    (create,) = [c for c in compose_commands(shell, "up", "dep") if "--no-start" in c]
    assert argv_of(create)[argv_of(create).index("-f") + 1] == "/weave/env/src/dep/.weave/compose.yaml"
