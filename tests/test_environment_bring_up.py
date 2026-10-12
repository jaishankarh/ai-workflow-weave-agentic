"""Bringing a Repo's Run recipe up inside a sandbox, and saving its logs (#49).

Pure logic with a fake sandbox, a fake clock and no Docker. (The same behaviour on a real sysbox
sandbox is in test_environment_on_sysbox.py.) Each test is named after an acceptance criterion
of #49.
"""

from __future__ import annotations

import yaml
import pytest

from workflow_weave.agent_worker.environment import (
    Environment,
    EnvironmentBringUpError,
    RECIPE_PATH,
    parse_recipe,
)
from workflow_weave.agent_worker.model import Outcome

RECIPE = """\
services:
  web: {image: python:3.12-slim}
  db: {image: postgres:16-alpine}
x-weave:
  readiness:
    web: {command: 'curl -f localhost:8000', timeout: 10, interval: 2}
    db: {command: [pg_isready], timeout: 10, interval: 2}
"""


class FakeSandbox:
    """Plays a sandbox's shell: records each command, answers from `script(command) -> (code, out)`."""

    def __init__(self, script):
        self.script = script
        self.commands: list[str] = []
        self.files: dict[str, str] = {}
        self.now = 0.0

    def run(self, command, timeout=120, cwd="/workspace"):
        self.commands.append(command)
        code, out = self.script(command, self)
        # A script that says nothing about a container lookup gets a running container with an address.
        if (code, out) == (0, ""):
            if " ps " in command and " -q " in command:
                return 0, f"c-{command.split()[-1]}\n"
            if command.startswith("docker inspect"):
                return 0, "10.0.0.2\n"
        return code, out

    def put_text(self, path, text):
        self.files[path] = text

    def sleep(self, s):
        self.now += s

    def clock(self):
        return self.now


def environment(sandbox, repo="svc", ticks=None):
    stamps = iter(ticks or (f"2026-10-08T10:00:{i:02d}+00:00" for i in range(60)))
    return Environment(sandbox, repo, now=lambda: next(stamps), clock=sandbox.clock, sleep=sandbox.sleep)


def healthy(command, sb):
    if "logs" in command:
        return 0, "web | listening\n"
    return 0, ""


def test_a_repos_recipe_is_brought_up_as_its_own_project_on_the_environment_network():
    sb = FakeSandbox(healthy)
    ready = environment(sb).bring_up(parse_recipe("svc", RECIPE))
    joined = "\n".join(sb.commands)
    assert "docker network create weave-env" in sb.commands[0]
    create, start = [c for c in sb.commands if " up " in c]  # created, joined to the network (#50), started
    assert "docker compose -p svc -f /workspace/svc/" + RECIPE_PATH in create
    assert "-f /weave/env/svc.override.yaml" in create and "--no-start" in create
    assert "up -d" in start
    assert yaml.safe_load(sb.files["/weave/env/svc.override.yaml"])["services"]["web"]["labels"]["weave.repo"] == "svc"
    assert "docker-compose.yml" not in joined
    assert [(r.service, r.address) for r in ready] == [("web", "web.svc"), ("db", "db.svc")]


def test_the_agent_starts_only_after_every_service_is_ready_and_a_service_is_reported_ready_only_when_it_passes():
    passes = {"web": 3, "db": 1}  # attempts needed before each check exits 0
    attempts = {"web": 0, "db": 0}

    def script(command, sb):
        for service in attempts:
            if f" exec -T {service} " in command:
                attempts[service] += 1
                return (0 if attempts[service] >= passes[service] else 1), ""
        return 0, ""

    sb = FakeSandbox(script)
    ready = environment(sb).bring_up(parse_recipe("svc", RECIPE))
    # bring_up returned only once both passed; web needed three attempts, two seconds apart.
    assert attempts == {"web": 3, "db": 1}
    by_service = {r.service: r for r in ready}
    assert by_service["web"].seconds_to_ready == 4.0  # polled at 0s, 2s, 4s: passed on the third
    assert by_service["db"].seconds_to_ready >= by_service["web"].seconds_to_ready  # checked after web
    assert all(r.ready_at.startswith("2026-10-08T10:00:") for r in ready)


def test_a_service_that_never_passes_its_check_is_never_ready_and_the_reason_names_the_repo_service_and_its_logs():
    def script(command, sb):
        if " exec -T web " in command:
            return 1, ""
        if " logs " in command and "web" in command:
            return 0, "web  | boot failed: no such table users\n"
        return 0, ""

    sb = FakeSandbox(script)
    env = environment(sb)
    with pytest.raises(EnvironmentBringUpError) as e:
        env.bring_up(parse_recipe("svc", RECIPE))
    reason = str(e.value)
    assert "svc" in reason and "web" in reason and "10s" in reason and "no such table users" in reason
    assert e.value.outcome is Outcome.NEEDS_SETUP  # Environment alone; the Base retry is the worker's (#51)
    assert env.ready == [], "a service that did not pass was reported ready"
    assert sb.now >= 10


def test_services_ready_before_a_later_one_fails_stay_on_record():
    def script(command, sb):
        return (1, "") if " exec -T db " in command else (0, "")

    env = environment(FakeSandbox(script))
    with pytest.raises(EnvironmentBringUpError):
        env.bring_up(parse_recipe("svc", RECIPE))
    assert [r.service for r in env.ready] == ["web"]


def test_a_recipe_whose_services_do_not_start_is_reported_with_the_compose_error():
    def script(command, sb):
        if " up " in command:
            return 1, "pull access denied for nosuchimage"
        return 0, ""

    with pytest.raises(EnvironmentBringUpError) as e:
        environment(FakeSandbox(script)).bring_up(parse_recipe("svc", RECIPE))
    assert "svc" in str(e.value) and "pull access denied" in str(e.value)


def test_a_repo_with_a_recipe_but_no_services_has_an_environment_with_nothing_to_start():
    sb = FakeSandbox(healthy)
    assert environment(sb, "client").bring_up(parse_recipe("client", "x-weave: {}\n")) == []
    assert sb.commands == []


def test_service_logs_are_saved_outside_the_sandbox_one_file_per_service(tmp_path):
    def script(command, sb):
        if " logs " in command:
            service = command.split()[-1]
            return 0, f"{service} | line one\n"
        return 0, ""

    sb = FakeSandbox(script)
    env = environment(sb)
    env.bring_up(parse_recipe("svc", RECIPE))
    saved = env.save_logs(tmp_path / "environment")
    assert saved == {
        "svc/web": str(tmp_path / "environment/svc/web.log"),
        "svc/db": str(tmp_path / "environment/svc/db.log"),
    }
    assert (tmp_path / "environment/svc/web.log").read_text() == "web | line one\n"


def test_logs_of_an_environment_that_failed_to_come_up_are_still_saved(tmp_path):
    def script(command, sb):
        if " up " in command:
            return 1, "boom"
        if " logs " in command:
            return 0, "partial output\n"
        return 0, ""

    env = environment(FakeSandbox(script))
    with pytest.raises(EnvironmentBringUpError):
        env.bring_up(parse_recipe("svc", RECIPE))
    assert set(env.save_logs(tmp_path)) == {"svc/web", "svc/db"}


def test_a_service_whose_logs_cannot_be_read_does_not_stop_the_others_being_saved(tmp_path):
    def script(command, sb):
        if " logs " in command and command.endswith(" web"):
            return 1, "no such container"
        return 0, "ok\n"

    env = environment(FakeSandbox(script))
    env.bring_up(parse_recipe("svc", RECIPE))
    with pytest.raises(RuntimeError) as e:
        env.save_logs(tmp_path)
    assert "web" in str(e.value)
    assert (tmp_path / "svc/db.log").exists()


def test_a_readiness_timeout_counts_from_after_up_returns_not_from_before_it_pulled_and_built():
    attempts = {"n": 0}

    def script(command, sb):
        if " up " in command and "--no-start" in command:
            sb.now += 300  # pulling and building images takes far longer than the 10s timeout
        if " exec -T web " in command:
            attempts["n"] += 1
            return (0 if attempts["n"] >= 3 else 1), ""
        return 0, ""

    sb = FakeSandbox(script)
    ready = environment(sb).bring_up(parse_recipe("svc", RECIPE))
    assert [r.service for r in ready] == ["web", "db"], "the slow build used up the readiness timeout"
