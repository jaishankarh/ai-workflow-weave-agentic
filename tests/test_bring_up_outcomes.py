"""Bring-up failures report the right outcome (#51).

Seam A with no Docker: a whole run is started on a fixture Product while a scripted stand-in plays
the sandbox's shell (and so its Docker engine). The agent is stubbed so a test can tell whether one
was started. Each test is named after an acceptance criterion of #51. The same behaviour on a real
sysbox sandbox is not covered here (no daemon): see the README's "Running the tests".
"""

from __future__ import annotations

import json
import os
import re
import stat
import sys
from pathlib import Path

import pytest
from conftest import PRODUCT, probe_request, product_yaml_for, repo_with_recipe, wait_until_ended
from test_test_secrets import KOR_KEY, write_secrets

from workflow_weave.agent_worker import AgentProfile, Outcome, RunRecord, RunState, Started
from workflow_weave.agent_worker.secret_store import SecretStore

RECIPE = """\
services:
  web: {image: python:3.12-slim}
x-weave:
  readiness:
    web: {command: 'true', timeout: 0.3, interval: 0.1}
"""
SEED_RECIPE = RECIPE.replace("x-weave:\n", "x-weave:\n  seed: {service: web, command: seed}\n")
SECRET_RECIPE = RECIPE.replace("x-weave:\n", "x-weave:\n  secrets: [KORONA_API_KEY]\n")
BRANCH_BUG = "boom: migration 0007 from the Story's branch failed"
SEED_BUG = "seed: duplicate key in the Story's migration"
BASE_BUG = "boom: the recipe itself is wrong"
UNREACHABLE = 'Get "https://registry.invalid/v2/": dial tcp: lookup registry.invalid: no such host'


class AgentStarted(Exception):
    """Raised by the stubbed agent conversation: a test that sees it saw an agent start."""


class Engine:
    """What the scripted sandbox does. `breaks` says, for a Repo's recipe running from `/workspace`
    (the working copy, i.e. with the run's branches) or from `/weave/env/src` (a checkout at Base),
    how its service fails: None (healthy), "never ready", or the output of a failing `up`."""

    def __init__(self) -> None:
        self.working_copy_at_base = False  # whether the working copy is still the Base branch
        self.breaks: dict[str, str | None] = {"/workspace": None, "/weave/env/src": None}
        self.seed_breaks: dict[str, str | None] = {"/workspace": None, "/weave/env/src": None}
        self.secret_echo = ""
        self.commands: list[str] = []
        self.files: dict[str, str] = {}


ENGINE = Engine()


class ScriptedSandbox:
    instances: list["ScriptedSandbox"] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.workspace = None
        ScriptedSandbox.instances.append(self)

    # -- what the worker does around the Environment
    def sh(self, command, timeout=120, cwd="/workspace"):
        ENGINE.commands.append(command)
        return ""

    def put_text(self, path, text):
        ENGINE.files[path] = text

    def put_repo(self, *args, **kwargs):
        pass

    def put_checkout(self, *args, **kwargs):
        pass

    def put_base_checkout(self, *args, **kwargs):
        pass

    def put_task_branch_checkout(self, *args, **kwargs):
        return True

    def processes(self):
        return []

    def stopped(self):
        return None

    def last_logs(self, chars=1500):
        return ""

    def destroy(self):
        pass

    # -- the shell
    def run(self, command, timeout=120, cwd="/workspace"):
        ENGINE.commands.append(command)
        if command.startswith("cat "):
            return 0, ENGINE_RECIPE[0]
        if "rev-parse HEAD" in command:  # is the working copy still at the Base branch?
            return (0 if ENGINE.working_copy_at_base else 1), ""
        if command.startswith("docker inspect"):
            return 0, "10.0.0.2\n"
        m = re.search(r"-f (/[^ ]*?)/\.weave/compose\.yaml", command)
        if not m:
            return 0, ""
        where = "/workspace" if m.group(1).startswith("/workspace") else "/weave/env/src"
        broken = ENGINE.breaks[where]
        bug = BRANCH_BUG if where == "/workspace" else BASE_BUG
        if " ps " in command:
            return 0, f"c-{command.split()[-1]}\n"
        if " up " in command:
            if broken and broken != "never ready":
                return 1, broken + ENGINE.secret_echo
            return 0, ""
        if " exec " in command and " sh -c seed" in command:  # the recipe's seed command
            failing = ENGINE.seed_breaks[where]
            return (1, failing + ENGINE.secret_echo) if failing else (0, "")
        if " exec " in command:
            return (1 if broken == "never ready" else 0), ""
        if " logs " in command:
            return 0, f"web  | {bug}{ENGINE.secret_echo}\n"
        return 0, ""  # down, and the rest


ENGINE_RECIPE = [RECIPE]


@pytest.fixture
def probe_profile():
    return AgentProfile(name="probe", image="never-started:test", acp_command=["true"])


@pytest.fixture
def stand_in_docker(tmp_path, monkeypatch):
    """A `docker` on PATH for a host with sysbox. It cannot run a sandbox."""
    bin_dir = tmp_path / "fake-bin"
    bin_dir.mkdir()
    script = bin_dir / "docker"
    script.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\n"
        "if sys.argv[1:2] == ['info']:\n"
        "    print(json.dumps({'runc': {'path': 'runc'}, 'sysbox-runc': {'path': 'sysbox-runc'}}))\n"
        "    sys.exit(0)\n"
        "if sys.argv[1:3] == ['network', 'inspect']:\n"
        "    print('127.0.0.1')\n"
        "    sys.exit(0)\n"
        "sys.exit(1)\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")


@pytest.fixture
def engine(monkeypatch, stand_in_docker):
    from workflow_weave.agent_worker import worker as worker_module

    global ENGINE
    ENGINE = Engine()
    ENGINE_RECIPE[0] = RECIPE
    ScriptedSandbox.instances.clear()
    started: list[str] = []

    def no_conversation(**kwargs):
        started.append("conversation")
        raise AgentStarted("an agent was started")

    monkeypatch.setattr(worker_module, "Sandbox", ScriptedSandbox)
    monkeypatch.setattr(worker_module, "require_runtime", lambda runtime: None)
    monkeypatch.setattr(worker_module, "stage_skills", lambda sandbox, plan: None)
    monkeypatch.setattr(worker_module, "stage_user_files", lambda sandbox, make: "/home/user")
    monkeypatch.setattr(worker_module, "stage_mcp_servers", lambda sandbox, servers: None)
    monkeypatch.setattr(worker_module, "ACPAgent", lambda **kwargs: object())
    monkeypatch.setattr(worker_module, "Conversation", no_conversation)
    ENGINE.agents_started = started
    return ENGINE


def run_to_the_end(make_worker, tmp_path, *, secrets: dict | None = None, recipe: str = RECIPE):
    ENGINE_RECIPE[0] = recipe
    repo = repo_with_recipe(tmp_path / "recipe-repos", "svc", recipe)
    worker = make_worker(product_yaml=product_yaml_for({"svc": repo}, PRODUCT))
    if secrets is not None:
        store = SecretStore(write_secrets(tmp_path / "s", PRODUCT, secrets).parent)
        worker.settings = worker.settings.__class__(**{**vars(worker.settings), "test_secrets": store})
    try:
        started = worker.start(probe_request({"end": "succeed"}, repos=["svc"]))
        assert isinstance(started, Started)
        final = wait_until_ended(worker, started.run_id, timeout=30)
        return worker, started, final, worker.record(started.run_id)
    finally:
        worker.shutdown()


def ups(engine) -> list[str]:
    return [c for c in engine.commands if " up --no-start" in c]


def assert_no_agent_and_the_lease_released(engine, worker, final):
    assert engine.agents_started == [], "an agent was started"
    assert "AgentStarted" not in (final.reason or "")
    assert worker.settings.subscriptions.in_use("probe-subscription") == 0, "the lease was not released"


# ---------------------------------------------------------------------------- host and network faults


@pytest.mark.parametrize(
    "output",
    [
        UNREACHABLE,
        "Cannot connect to the Docker daemon at unix:///var/run/docker.sock. Is the docker daemon running?",
        "Error response from daemon: Get https://registry-1.docker.io/v2/: net/http: TLS handshake timeout",
        "dial tcp 10.1.2.3:443: i/o timeout",
    ],
)
def test_an_unreachable_image_registry_or_a_daemon_problem_gives_infra_failure_even_with_the_stories_branches(
    make_worker, tmp_path, engine, output
):
    engine.breaks["/workspace"] = output  # the run's branches are in place; not retried all the same
    worker, _, final, record = run_to_the_end(make_worker, tmp_path)
    assert final.outcome is Outcome.INFRA_FAILURE, final.reason
    assert "svc" in final.reason and output.split(":")[0][:20] in final.reason
    assert len(ups(engine)) == 1, "a host fault was retried at Base"
    assert_no_agent_and_the_lease_released(engine, worker, final)


def test_a_host_fault_on_the_retry_at_base_is_still_infra_failure(make_worker, tmp_path, engine):
    engine.breaks.update({"/workspace": "never ready", "/weave/env/src": UNREACHABLE})
    worker, _, final, _ = run_to_the_end(make_worker, tmp_path)
    assert final.outcome is Outcome.INFRA_FAILURE and "registry.invalid" in final.reason
    assert_no_agent_and_the_lease_released(engine, worker, final)


# ---------------------------------------------------------------------------- fails on every branch


@pytest.mark.parametrize("failure", ["never ready", "pull access denied for nosuchimage, repository does not exist"])
def test_a_recipe_whose_service_fails_on_every_branch_gives_needs_setup_with_the_repo_service_and_log_lines(
    make_worker, tmp_path, engine, failure
):
    engine.breaks.update({"/workspace": failure, "/weave/env/src": failure})
    worker, started, final, record = run_to_the_end(make_worker, tmp_path)
    assert final.state is RunState.ENDED and final.outcome is Outcome.NEEDS_SETUP, final.reason
    assert "svc" in final.reason
    assert (BASE_BUG in final.reason and "web" in final.reason) or "nosuchimage" in final.reason
    assert BRANCH_BUG not in final.reason, "the reason is about the Base branch, where a human has to fix it"
    assert len(ups(engine)) == 2, "retried exactly once"
    assert record.environment_bring_up_attempts == ["branches", "base"]
    assert_no_agent_and_the_lease_released(engine, worker, final)


# ---------------------------------------------------------------------------- breaks only on the branch


def test_a_repo_that_breaks_only_on_the_stories_branch_gives_environment_broken_with_the_logs(
    make_worker, tmp_path, engine
):
    engine.breaks["/workspace"] = "never ready"
    worker, started, final, record = run_to_the_end(make_worker, tmp_path)
    assert final.state is RunState.ENDED and final.outcome is Outcome.ENVIRONMENT_BROKEN, final.reason
    assert "svc" in final.reason and "web" in final.reason and BRANCH_BUG in final.reason
    assert len(ups(engine)) == 2
    assert record.environment_bring_up_attempts == ["branches", "base"]
    # The logs of the failed attempt outlive its teardown, beside the event log.
    log = Path(record.environment_logs["with-branches/svc/web"])
    assert BRANCH_BUG in log.read_text() and log.is_relative_to(Path(record.event_log).parent)
    assert_no_agent_and_the_lease_released(engine, worker, final)


def test_the_branch_attempt_is_taken_down_before_the_retry_so_the_retry_starts_clean(make_worker, tmp_path, engine):
    engine.breaks["/workspace"] = "never ready"
    run_to_the_end(make_worker, tmp_path)
    commands = engine.commands
    down = next(i for i, c in enumerate(commands) if " down " in c and "-p svc" in c and "/workspace/svc" in c)
    first_up = next(i for i, c in enumerate(commands) if " up --no-start" in c)
    retry_up = [i for i, c in enumerate(commands) if " up --no-start" in c][1]
    assert first_up < down < retry_up
    assert "/weave/env/src/svc/.weave/compose.yaml" in commands[retry_up]


# ---------------------------------------------------------------------------- already at Base


@pytest.mark.parametrize("failure", ["never ready", "pull access denied for nosuchimage"])
def test_a_run_that_starts_every_repo_at_base_and_fails_to_bring_up_an_environment_gives_needs_setup_with_no_retry(
    make_worker, tmp_path, engine, failure
):
    engine.working_copy_at_base = True  # a first implement run: the Integration branch is Base
    engine.breaks["/workspace"] = failure
    worker, _, final, record = run_to_the_end(make_worker, tmp_path)
    assert final.outcome is Outcome.NEEDS_SETUP, final.reason
    assert "svc" in final.reason
    assert len(ups(engine)) == 1, "retried though there was nothing to retry with"
    assert record.environment_bring_up_attempts == ["base"]
    assert_no_agent_and_the_lease_released(engine, worker, final)


# ---------------------------------------------------------------------------- healthy


def test_a_healthy_bring_up_does_not_retry_and_goes_on_to_start_the_agent(make_worker, tmp_path, engine):
    worker, _, final, record = run_to_the_end(make_worker, tmp_path)
    assert len(ups(engine)) == 1
    assert record.environment_bring_up_attempts == ["branches"]
    assert engine.agents_started == ["conversation"], "bring-up succeeded but no agent started"
    assert not any(" down " in c for c in engine.commands), "a healthy Environment was taken down before the agent"


# ---------------------------------------------------------------------------- the distinct status


def test_environment_broken_is_a_distinct_outcome_in_status_and_in_the_run_record(make_worker, tmp_path, engine):
    engine.breaks["/workspace"] = "never ready"
    worker, started, final, record = run_to_the_end(make_worker, tmp_path)
    assert Outcome.ENVIRONMENT_BROKEN.value == "environment-broken"
    assert len({o.value for o in Outcome}) == len(list(Outcome))
    assert worker.status(started.run_id).outcome is Outcome.ENVIRONMENT_BROKEN
    saved = json.loads((worker.settings.runs_dir / started.run_id / "record.json").read_text())
    assert saved["outcome"] == "environment-broken"
    assert RunRecord.from_json(json.dumps(saved)).outcome is Outcome.ENVIRONMENT_BROKEN
    # A record from before the retry existed still loads.
    del saved["environment_bring_up_attempts"]
    assert RunRecord.from_json(json.dumps(saved)).environment_bring_up_attempts is None


# ---------------------------------------------------------------------------- Test secrets still hold


@pytest.mark.parametrize(
    "breaks",
    [
        {"/workspace": "never ready", "/weave/env/src": None},  # environment-broken
        {"/workspace": "never ready", "/weave/env/src": "never ready"},  # needs-setup after the retry
        {"/workspace": "never ready", "/weave/env/src": "pull access denied: "},  # a failing `up` on retry
    ],
)
def test_no_test_secret_value_is_written_in_any_reason_log_or_record_of_either_attempt(
    make_worker, tmp_path, engine, breaks
):
    engine.breaks.update(breaks)
    engine.secret_echo = f" (env KORONA_API_KEY={KOR_KEY})"
    worker, started, final, record = run_to_the_end(
        make_worker, tmp_path, secrets={"KORONA_API_KEY": KOR_KEY}, recipe=SECRET_RECIPE
    )
    assert final.outcome in (Outcome.ENVIRONMENT_BROKEN, Outcome.NEEDS_SETUP), final.reason
    written = {p: p.read_text() for p in (worker.settings.runs_dir / started.run_id).rglob("*") if p.is_file()}
    assert any("with-branches" in str(p) for p in written), "the first attempt's logs were not saved"
    for path, text in written.items():
        assert KOR_KEY not in text, f"{path.name} holds a Test secret value"
    assert KOR_KEY not in (final.reason or "")
    assert record.test_secrets_given == {"svc": ["KORONA_API_KEY"]}


# ---------------------------------------------------------------------------- seeding (#53) inside each attempt


def seeds(engine) -> list[str]:
    return [c for c in engine.commands if " exec " in c and " sh -c seed" in c]


def test_a_seed_that_fails_on_the_branches_but_passes_at_base_gives_environment_broken(make_worker, tmp_path, engine):
    engine.seed_breaks["/workspace"] = SEED_BUG
    worker, _, final, record = run_to_the_end(make_worker, tmp_path, recipe=SEED_RECIPE)
    assert final.outcome is Outcome.ENVIRONMENT_BROKEN, final.reason
    assert "svc" in final.reason and "seed" in final.reason and SEED_BUG in final.reason
    assert record.environment_bring_up_attempts == ["branches", "base"]
    assert len(ups(engine)) == 2 and len(seeds(engine)) == 2
    assert "with-branches/svc/web" in record.environment_logs, "the abandoned attempt's logs were not saved"
    assert [s["repo"] for s in record.environment_seeds] == ["svc"], "the retry's seed is what the record holds"
    assert_no_agent_and_the_lease_released(engine, worker, final)


def test_a_seed_that_fails_on_the_branches_and_at_base_gives_needs_setup(make_worker, tmp_path, engine):
    engine.seed_breaks.update({"/workspace": SEED_BUG, "/weave/env/src": "seed: the recipe's own seed is wrong"})
    worker, _, final, record = run_to_the_end(make_worker, tmp_path, recipe=SEED_RECIPE)
    assert final.outcome is Outcome.NEEDS_SETUP, final.reason
    assert "Base branch either" in final.reason and "the recipe's own seed is wrong" in final.reason
    assert SEED_BUG not in final.reason
    assert record.environment_bring_up_attempts == ["branches", "base"]
    assert not record.environment_seeds
    assert_no_agent_and_the_lease_released(engine, worker, final)


def test_the_seed_runs_again_on_the_retry_attempt_after_the_first_is_taken_down_with_its_volumes(
    make_worker, tmp_path, engine
):
    engine.breaks["/workspace"] = "never ready"  # the services, not the seed, fail first
    run_to_the_end(make_worker, tmp_path, recipe=SEED_RECIPE)
    commands = engine.commands
    assert len(seeds(engine)) == 1, "seeded an Environment whose services never became ready"
    assert "/weave/env/src/svc" in seeds(engine)[0]
    retry_up = [i for i, c in enumerate(commands) if " up --no-start" in c][1]
    assert commands.index(seeds(engine)[0]) > retry_up


def test_a_failed_seed_takes_the_first_attempt_down_with_volumes_before_the_retry_seeds(make_worker, tmp_path, engine):
    engine.seed_breaks["/workspace"] = SEED_BUG
    run_to_the_end(make_worker, tmp_path, recipe=SEED_RECIPE)
    commands = engine.commands
    first_seed, retry_seed = (commands.index(c) for c in seeds(engine))
    down = next(i for i, c in enumerate(commands) if " down -v" in c and "/workspace/svc" in c)
    assert first_seed < down < retry_seed


def test_a_seed_that_fails_for_a_host_fault_gives_infra_failure_without_a_retry(make_worker, tmp_path, engine):
    engine.seed_breaks["/workspace"] = UNREACHABLE
    worker, _, final, record = run_to_the_end(make_worker, tmp_path, recipe=SEED_RECIPE)
    assert final.outcome is Outcome.INFRA_FAILURE, final.reason
    assert len(ups(engine)) == 1 and len(seeds(engine)) == 1
    assert_no_agent_and_the_lease_released(engine, worker, final)


def test_no_test_secret_value_is_written_when_a_seed_fails_on_either_attempt(make_worker, tmp_path, engine):
    engine.seed_breaks.update({"/workspace": SEED_BUG, "/weave/env/src": SEED_BUG})
    engine.secret_echo = f" (env KORONA_API_KEY={KOR_KEY})"
    recipe = SECRET_RECIPE.replace("x-weave:\n", "x-weave:\n  seed: {service: web, command: seed}\n")
    worker, started, final, _ = run_to_the_end(
        make_worker, tmp_path, secrets={"KORONA_API_KEY": KOR_KEY}, recipe=recipe
    )
    assert final.outcome is Outcome.NEEDS_SETUP, final.reason
    for path in (worker.settings.runs_dir / started.run_id).rglob("*"):
        if path.is_file():
            assert KOR_KEY not in path.read_text(), f"{path.name} holds a Test secret value"
    assert KOR_KEY not in (final.reason or "")
