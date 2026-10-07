"""A Product's Test secrets: one file per Product on the Sandbox host, outside every repo (#52).

Pure logic, no Docker: a fake sandbox plays the Sandbox host's shell and a stand-in `docker` plays
its engine. Each test is named after an acceptance criterion of #52 (or the invariant it needs).
The same behaviour on a real sysbox sandbox is in test_test_secrets_on_sysbox.py.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest
import yaml
from conftest import PRODUCT, probe_request, product_yaml_for, repo_with_recipe, wait_until_ended

from workflow_weave.agent_worker import AgentProfile, NeedsSetup, Outcome, RunRecord, RunState, Started
from workflow_weave.agent_worker.environment import Environment, EnvironmentBringUpError, parse_recipe, RecipeError
from workflow_weave.agent_worker.secret_store import (
    SecretStore, SecretsError, load_configured_secret_store, redact,
)

KOR_KEY = "kor-test-key-8f3a91c2"
STRIPE_KEY = "payments-test-key-4eC39HqL"
OTHER_PRODUCT_KEY = "other-products-key-77d1"


def write_secrets(directory: Path, product: str, secrets: dict, mode: int = 0o600) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{product}.yaml"
    path.write_text(yaml.safe_dump(secrets))
    path.chmod(mode)
    return path


@pytest.fixture
def secrets_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "host-secrets"
    write_secrets(directory, PRODUCT, {"KORONA_API_KEY": KOR_KEY, "STRIPE_KEY": STRIPE_KEY, "UNUSED": "never-named"})
    write_secrets(directory, "other-product", {"KORONA_API_KEY": OTHER_PRODUCT_KEY, "OTHERS_ONLY": "x"})
    return directory


# ----------------------------------------------------------------------------- the file


def test_the_secrets_a_recipe_names_are_read_from_the_products_own_file(secrets_dir):
    got = SecretStore(secrets_dir).for_product(PRODUCT).select(["KORONA_API_KEY"])
    assert got == {"KORONA_API_KEY": KOR_KEY}


def test_a_product_cannot_read_another_products_test_secrets(secrets_dir):
    mine = SecretStore(secrets_dir).for_product(PRODUCT)
    # The same name in another Product's file never leaks: only this Product's value is seen.
    assert mine.select(["KORONA_API_KEY"]) == {"KORONA_API_KEY": KOR_KEY}
    # A name only the other Product has is missing for this one.
    assert mine.missing(["OTHERS_ONLY"]) == ["OTHERS_ONLY"]


@pytest.mark.parametrize("product", ["../other-product", "a/b", "", ".", "..", "x\0y", "/etc/passwd"])
def test_a_product_name_cannot_point_the_lookup_at_another_file(secrets_dir, product):
    with pytest.raises(SecretsError, match="Product name"):
        SecretStore(secrets_dir).for_product(product)


def test_a_file_that_others_can_read_is_refused_naming_the_file_and_the_fix(secrets_dir):
    path = write_secrets(secrets_dir, PRODUCT, {"A": "v"}, mode=0o644)
    with pytest.raises(SecretsError) as e:
        SecretStore(secrets_dir).for_product(PRODUCT)
    assert str(path) in str(e.value) and "chmod 600" in str(e.value) and "644" in str(e.value)


@pytest.mark.parametrize("mode", [0o600, 0o400])
def test_a_file_only_its_owner_can_use_is_accepted(secrets_dir, mode):
    write_secrets(secrets_dir, PRODUCT, {"A": "v"}, mode=mode)
    assert SecretStore(secrets_dir).for_product(PRODUCT).select(["A"]) == {"A": "v"}


def test_a_product_with_no_file_has_no_secrets_and_the_error_names_where_to_put_one(secrets_dir):
    with pytest.raises(SecretsError) as e:
        SecretStore(secrets_dir).for_product("no-such-product")
    assert str(secrets_dir / "no-such-product.yaml") in str(e.value)


@pytest.mark.parametrize(
    "text", ["- a\n- b\n", "A: [1, 2]\n", "A: {b: c}\n", "1bad: x\n", "HAS SPACE: x\n", "A: null\n", ": : :\n"]
)
def test_a_file_that_is_not_a_flat_mapping_of_names_to_values_is_refused(secrets_dir, text):
    path = write_secrets(secrets_dir, PRODUCT, {})
    path.write_text(text)
    with pytest.raises(SecretsError) as e:
        SecretStore(secrets_dir).for_product(PRODUCT)
    assert str(path) in str(e.value)


def test_a_refusal_never_prints_a_secret_value(secrets_dir):
    path = write_secrets(secrets_dir, PRODUCT, {})
    path.write_text(f"GOOD: {KOR_KEY}\nA: [{STRIPE_KEY}]\n")
    with pytest.raises(SecretsError) as e:
        SecretStore(secrets_dir).for_product(PRODUCT)
    assert KOR_KEY not in str(e.value) and STRIPE_KEY not in str(e.value)


def test_a_secret_the_product_lacks_is_reported_missing_by_name(secrets_dir):
    mine = SecretStore(secrets_dir).for_product(PRODUCT)
    assert mine.missing(["KORONA_API_KEY", "GONE", "ALSO_GONE"]) == ["GONE", "ALSO_GONE"]


def test_the_secrets_are_never_in_a_repr(secrets_dir):
    mine = SecretStore(secrets_dir).for_product(PRODUCT)
    assert KOR_KEY not in repr(mine) and KOR_KEY not in repr(SecretStore(secrets_dir))


def test_the_location_is_a_setting_next_to_the_subscription_stores_and_the_example_parses(tmp_path):
    config = tmp_path / "weave.yaml"
    config.write_text("subscription_store:\n  location: s.yaml\ntest_secrets:\n  location: sub/secrets\n")
    write_secrets(tmp_path / "sub" / "secrets", "p", {"A": "1"})
    assert load_configured_secret_store(config).for_product("p").select(["A"]) == {"A": "1"}
    config.write_text("subscription_store:\n  location: s.yaml\n")
    with pytest.raises(ValueError, match="test_secrets.location"):
        load_configured_secret_store(config)


def test_the_repos_own_weave_yaml_and_example_file_show_the_setting_and_the_format():
    root = Path(__file__).resolve().parent.parent
    location = yaml.safe_load((root / "weave.yaml").read_text())["test_secrets"]["location"]
    assert location.startswith("~/") and "secrets" in location  # on the host, outside every repo
    example = yaml.safe_load((root / "config" / "test-secrets.example.yaml").read_text())
    assert example and all(isinstance(v, str) for v in example.values())


def test_redact_hides_every_value_wherever_it_appears_and_leaves_the_rest():
    text = f"call failed: key={KOR_KEY} and {STRIPE_KEY}, again {KOR_KEY}"
    out = redact(text, [KOR_KEY, STRIPE_KEY])
    assert KOR_KEY not in out and STRIPE_KEY not in out
    assert out.startswith("call failed: key=") and "again" in out
    assert redact("nothing here", [KOR_KEY]) == "nothing here"
    assert redact("a  b", [""]) == "a  b"  # an empty value must not blank the text


# ----------------------------------------------------------------------------- the recipe


RECIPE = """\
services:
  web: {image: python:3.12-slim}
  api: {image: python:3.12-slim}
x-weave:
  secrets: [KORONA_API_KEY, STRIPE_KEY]
  readiness:
    web: {command: 'true'}
    api: {command: 'true'}
"""


def test_a_recipe_names_its_secrets_by_name_only():
    assert parse_recipe("svc", RECIPE).secrets == ("KORONA_API_KEY", "STRIPE_KEY")
    no_secrets = RECIPE.replace("  secrets: [KORONA_API_KEY, STRIPE_KEY]\n", "")
    assert parse_recipe("svc", no_secrets).secrets == ()


@pytest.mark.parametrize(
    "line, fragment",
    [
        ("secrets: KORONA_API_KEY", "list of secret names"),
        ("secrets: [{KORONA_API_KEY: abc123}]", "list of secret names"),  # a value in the recipe: refused
        ("secrets: ['not a name']", "'not a name'"),
        ("secrets: [A, A]", "twice"),
    ],
)
def test_a_recipe_secrets_entry_that_is_not_a_list_of_names_is_refused_naming_the_repo(line, fragment):
    bad = RECIPE.replace("secrets: [KORONA_API_KEY, STRIPE_KEY]", line)
    with pytest.raises(RecipeError, match="svc") as e:
        parse_recipe("svc", bad)
    assert fragment in str(e.value) and "abc123" not in str(e.value)


# ----------------------------------------------------------------------------- the Environment


class FakeSandbox:
    def __init__(self, script=None):
        self.commands: list[str] = []
        self.files: dict[str, str] = {}
        self.script = script or (lambda command: (0, ""))

    def run(self, command, timeout=120, cwd="/workspace"):
        self.commands.append(command)
        code, out = self.script(command)
        # Like a real engine for the steps between the two `up` commands (#50): a container to join
        # the network, and the address it got there.
        if (code, out) == (0, ""):
            if " ps " in command and " -q " in command:
                return 0, "cid1\n"
            if command.startswith("docker inspect"):
                return 0, "10.0.0.2\n"
        return code, out

    def put_text(self, path, text):
        self.files[path] = text


def environment(sandbox, secrets):
    return Environment(sandbox, "svc", now=lambda: "t", clock=lambda: 0.0, sleep=lambda s: None, secrets=secrets)


def test_a_recipe_naming_a_secret_the_product_has_receives_its_value_in_its_services_environment():
    sb = FakeSandbox()
    environment(sb, {"KORONA_API_KEY": KOR_KEY, "STRIPE_KEY": STRIPE_KEY}).bring_up(parse_recipe("svc", RECIPE))
    override = yaml.safe_load(sb.files["/weave/env/svc.override.yaml"])
    # Every service of the recipe gets each named secret, by name; Compose takes the value from the
    # environment of the `up` command, so no file in the sandbox holds a value.
    for service in ("web", "api"):
        assert sorted(override["services"][service]["environment"]) == ["KORONA_API_KEY", "STRIPE_KEY"]
    # The 3-step bring-up (#50) creates with `up --no-start`, joins the network, then `up -d`: the
    # two `up` commands each carry the values, because either may create or start a container.
    ups = [c for c in sb.commands if " up " in c]
    assert len(ups) == 2 and "--no-start" in ups[0] and "--no-recreate" in ups[1]
    for up in ups:
        assert up.startswith("KORONA_API_KEY=") and f"KORONA_API_KEY={KOR_KEY} " in up and STRIPE_KEY in up


def test_a_secret_the_recipe_does_not_name_is_absent_from_every_service_and_every_command():
    sb = FakeSandbox()
    environment(sb, {"KORONA_API_KEY": KOR_KEY}).bring_up(
        parse_recipe("svc", RECIPE.replace("[KORONA_API_KEY, STRIPE_KEY]", "[KORONA_API_KEY]"))
    )
    everything = "\n".join(sb.commands) + "\n".join(sb.files.values())
    assert STRIPE_KEY not in everything and "STRIPE_KEY" not in everything and "UNUSED" not in everything
    # And only the commands that create or start the containers carry the value.
    assert [c for c in sb.commands if KOR_KEY in c] == [c for c in sb.commands if " up " in c]


def test_a_recipe_naming_no_secrets_gives_its_services_no_environment_entry_and_the_up_command_no_prefix():
    sb = FakeSandbox()
    environment(sb, {}).bring_up(parse_recipe("svc", RECIPE.replace("  secrets: [KORONA_API_KEY, STRIPE_KEY]\n", "")))
    assert "environment" not in yaml.safe_load(sb.files["/weave/env/svc.override.yaml"])["services"]["web"]
    ups = [c for c in sb.commands if " up " in c]
    assert len(ups) == 2 and all(up.startswith("docker compose") for up in ups)


def test_a_value_with_quotes_and_spaces_reaches_the_command_as_one_word():
    import shlex

    tricky = "pa ss'wo\"rd $HOME;`id`"
    sb = FakeSandbox()
    environment(sb, {"KORONA_API_KEY": tricky, "STRIPE_KEY": "s"}).bring_up(parse_recipe("svc", RECIPE))
    ups = [c for c in sb.commands if " up " in c]
    assert len(ups) == 2 and all(f"KORONA_API_KEY={tricky}" in shlex.split(up) for up in ups)


def test_an_environment_given_less_than_its_recipe_names_refuses_to_start_naming_the_missing_secret():
    sb = FakeSandbox()
    with pytest.raises(EnvironmentBringUpError) as e:
        environment(sb, {"KORONA_API_KEY": KOR_KEY}).bring_up(parse_recipe("svc", RECIPE))
    assert e.value.outcome is Outcome.NEEDS_SETUP and "STRIPE_KEY" in e.value.reason and "svc" in e.value.reason
    assert sb.commands == [], "something was started"


def test_a_secret_value_echoed_by_a_failing_start_is_not_in_the_reason():
    def script(command):
        if " up " in command:
            return 1, f"error pulling with token {KOR_KEY} for {STRIPE_KEY}"
        return 0, ""

    with pytest.raises(EnvironmentBringUpError) as e:
        environment(FakeSandbox(script), {"KORONA_API_KEY": KOR_KEY, "STRIPE_KEY": STRIPE_KEY}).bring_up(
            parse_recipe("svc", RECIPE)
        )
    assert "error pulling" in e.value.reason
    assert KOR_KEY not in e.value.reason and STRIPE_KEY not in e.value.reason


def test_a_secret_value_in_a_services_log_is_not_in_the_log_saved_outside_the_sandbox(tmp_path):
    def script(command):
        if " logs " in command:
            return 0, f"web | connecting with {KOR_KEY}\n"
        return 0, ""

    env = environment(FakeSandbox(script), {"KORONA_API_KEY": KOR_KEY, "STRIPE_KEY": STRIPE_KEY})
    env.bring_up(parse_recipe("svc", RECIPE))
    env.save_logs(tmp_path / "environment")
    saved = (tmp_path / "environment" / "svc" / "web.log").read_text()
    assert "connecting with" in saved and KOR_KEY not in saved


# ----------------------------------------------------------------------------- the worker


SECRET_RECIPE = "x-weave:\n  secrets: [KORONA_API_KEY, STRIPE_KEY]\n"


@pytest.fixture
def probe_profile():
    """Replaces the image-building fixture: no sandbox is really started here."""
    return AgentProfile(name="probe", image="never-started:test", acp_command=["true"])


@pytest.fixture
def stand_in_docker(tmp_path, monkeypatch):
    """A `docker` on PATH for a host with sysbox; records every call. It cannot run a sandbox."""
    bin_dir = tmp_path / "fake-bin"
    bin_dir.mkdir()
    calls = tmp_path / "docker-calls.jsonl"
    script = bin_dir / "docker"
    script.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\n"
        f"open({str(calls)!r}, 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "if sys.argv[1:2] == ['info']:\n"
        "    print(json.dumps({'runc': {'path': 'runc'}, 'sysbox-runc': {'path': 'sysbox-runc'}}))\n"
        "    sys.exit(0)\n"
        "if sys.argv[1:3] == ['network', 'inspect']:\n"
        "    print('127.0.0.1')\n"
        "    sys.exit(0)\n"
        "sys.stderr.write('stand-in docker: not supported\\n')\n"
        "sys.exit(1)\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    return lambda: [json.loads(line) for line in calls.read_text().splitlines()] if calls.exists() else []


def worker_for(make_worker, tmp_path, secret_store, recipe=SECRET_RECIPE, repo_name="svc"):
    repo = repo_with_recipe(tmp_path / "recipe-repos", repo_name, recipe)
    worker = make_worker(product_yaml=product_yaml_for({repo_name: repo}, PRODUCT))
    worker.settings = worker.settings.__class__(**{**vars(worker.settings), "test_secrets": secret_store})
    return worker


def test_a_named_secret_the_product_lacks_gives_needs_setup_naming_the_repo_and_the_secret_and_no_sandbox_starts(
    make_worker, tmp_path, stand_in_docker
):
    store = SecretStore(write_secrets(tmp_path / "s", PRODUCT, {"KORONA_API_KEY": KOR_KEY}).parent)
    worker = worker_for(make_worker, tmp_path, store)
    try:
        result = worker.start(probe_request({"end": "succeed"}, repos=["svc"]))
    finally:
        worker.shutdown()
    assert isinstance(result, NeedsSetup) and result.outcome is Outcome.NEEDS_SETUP
    assert "svc" in result.reason and "STRIPE_KEY" in result.reason and PRODUCT in result.reason
    assert "KORONA_API_KEY" not in result.reason, "a secret the Product has was reported missing"
    assert not [c for c in stand_in_docker() if c[:1] in (["run"], ["create"])], "a sandbox was started"
    assert KOR_KEY not in result.reason


def test_a_product_with_no_secrets_file_at_all_gives_needs_setup_naming_the_file(
    make_worker, tmp_path, stand_in_docker
):
    worker = worker_for(make_worker, tmp_path, SecretStore(tmp_path / "empty-dir"))
    try:
        result = worker.start(probe_request({"end": "succeed"}, repos=["svc"]))
    finally:
        worker.shutdown()
    assert isinstance(result, NeedsSetup)
    assert "svc" in result.reason and "KORONA_API_KEY" in result.reason and f"{PRODUCT}.yaml" in result.reason


def test_a_worker_with_no_secret_store_cannot_satisfy_a_recipe_that_names_a_secret(
    make_worker, tmp_path, stand_in_docker
):
    worker = worker_for(make_worker, tmp_path, None)
    try:
        result = worker.start(probe_request({"end": "succeed"}, repos=["svc"]))
    finally:
        worker.shutdown()
    assert isinstance(result, NeedsSetup) and "svc" in result.reason and "KORONA_API_KEY" in result.reason


def test_a_secrets_file_others_can_read_gives_needs_setup_before_any_sandbox(make_worker, tmp_path, stand_in_docker):
    path = write_secrets(tmp_path / "s", PRODUCT, {"KORONA_API_KEY": KOR_KEY, "STRIPE_KEY": STRIPE_KEY}, mode=0o666)
    worker = worker_for(make_worker, tmp_path, SecretStore(path.parent))
    try:
        result = worker.start(probe_request({"end": "succeed"}, repos=["svc"]))
    finally:
        worker.shutdown()
    assert isinstance(result, NeedsSetup) and "chmod 600" in result.reason and str(path) in result.reason
    assert KOR_KEY not in result.reason


def test_a_recipe_naming_no_secret_needs_no_secrets_file_and_no_store(make_worker, tmp_path, stand_in_docker):
    worker = worker_for(make_worker, tmp_path, None, recipe="x-weave: {}\n")
    try:
        result = worker.start(probe_request({"end": "succeed"}, repos=["svc"]))
        assert isinstance(result, Started)
        wait_until_ended(worker, result.run_id, timeout=30)  # ends in the stand-in host's infra-failure
    finally:
        worker.shutdown()


def test_a_secret_other_than_the_one_a_repo_names_does_not_count_for_it(make_worker, tmp_path, stand_in_docker):
    # Another Product's file has both names; this Product's has neither: still needs-setup.
    write_secrets(tmp_path / "s", "other-product", {"KORONA_API_KEY": OTHER_PRODUCT_KEY, "STRIPE_KEY": "o"})
    write_secrets(tmp_path / "s", PRODUCT, {"UNRELATED": "u"})
    worker = worker_for(make_worker, tmp_path, SecretStore(tmp_path / "s"))
    try:
        result = worker.start(probe_request({"end": "succeed"}, repos=["svc"]))
    finally:
        worker.shutdown()
    assert isinstance(result, NeedsSetup)
    assert "KORONA_API_KEY" in result.reason and "STRIPE_KEY" in result.reason
    assert OTHER_PRODUCT_KEY not in result.reason


def test_the_run_record_has_a_place_for_secret_names_and_old_records_still_load():
    rec = RunRecord(
        run_id="r", product="p", agent_profile="a", subscription="s", skill="k", repos=[],
        state=RunState.ENDED, started_at="t", test_secrets_given={"svc": ["KORONA_API_KEY"]},
    )
    assert RunRecord.from_json(rec.to_json()).test_secrets_given == {"svc": ["KORONA_API_KEY"]}
    d = json.loads(rec.to_json())
    del d["test_secrets_given"]
    assert RunRecord.from_json(json.dumps(d)).test_secrets_given is None


# ----------------------------------------------------------------------------- a whole run, no Docker


class RunSandbox:
    """Stands in for `Sandbox` in the worker: records its constructor arguments and every command."""

    instances: list["RunSandbox"] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.commands: list[str] = []
        self.files: dict[str, str] = {}
        self.workspace = None
        RunSandbox.instances.append(self)

    def sh(self, command, timeout=120, cwd="/workspace"):
        self.commands.append(command)
        return ""

    def run(self, command, timeout=120, cwd="/workspace"):
        self.commands.append(command)
        if command.startswith("cat "):
            return 0, RECIPE
        if " up " in command:
            return 1, f"pull access denied; env was KORONA_API_KEY={KOR_KEY} STRIPE_KEY={STRIPE_KEY}"
        if " logs " in command:
            return 0, f"web | started with {KOR_KEY}\n"
        return 0, ""

    def put_text(self, path, text):
        self.files[path] = text

    def put_repo(self, *args, **kwargs):
        pass

    def processes(self):
        return []

    def stopped(self):
        return None

    def last_logs(self, chars=1500):
        return ""

    def destroy(self):
        pass


@pytest.fixture
def sandbox_stand_in(monkeypatch):
    from workflow_weave.agent_worker import worker as worker_module

    RunSandbox.instances.clear()
    monkeypatch.setattr(worker_module, "Sandbox", RunSandbox)
    monkeypatch.setattr(worker_module, "require_runtime", lambda runtime: None)
    monkeypatch.setattr(worker_module, "stage_skills", lambda sandbox, plan: None)
    monkeypatch.setattr(worker_module, "stage_user_files", lambda sandbox, make: "/home/user")
    return RunSandbox.instances


def test_the_run_record_lists_the_secret_names_given_to_a_run_and_no_value_is_written_anywhere(
    make_worker, tmp_path, sandbox_stand_in, runs_dir, stand_in_docker
):
    store = SecretStore(
        write_secrets(
            tmp_path / "s", PRODUCT, {"KORONA_API_KEY": KOR_KEY, "STRIPE_KEY": STRIPE_KEY, "UNUSED": "unused-value-1"}
        ).parent
    )
    worker = worker_for(make_worker, tmp_path, store, recipe=RECIPE)
    try:
        started = worker.start(probe_request({"end": "succeed"}, repos=["svc"]))
        assert isinstance(started, Started)
        final = wait_until_ended(worker, started.run_id, timeout=30)
    finally:
        worker.shutdown()
    assert final.state is RunState.ENDED and final.outcome is Outcome.NEEDS_SETUP, final
    record = worker.record(started.run_id)
    assert record.test_secrets_given == {"svc": ["KORONA_API_KEY", "STRIPE_KEY"]}

    # Nothing written for the run (record, run.log, event log, saved service logs) holds a value,
    # and the Product's other secrets are not even named.
    written = {p: p.read_text() for p in (runs_dir / started.run_id).rglob("*") if p.is_file()}
    assert any(p.name == "run.log" for p in written) and any(p.name == "web.log" for p in written)
    for path, text in written.items():
        for value in (KOR_KEY, STRIPE_KEY, "unused-value-1"):
            assert value not in text, f"{path.name} holds a Test secret value"
    assert "pull access denied" in (final.reason or "")
    assert "UNUSED" not in "".join(written.values())

    # The agent's sandbox environment never gets a secret, by name or by value.
    (sandbox,) = sandbox_stand_in
    env_text = json.dumps(sandbox.kwargs.get("env"))
    assert not any(s in env_text for s in (KOR_KEY, STRIPE_KEY, "KORONA_API_KEY", "STRIPE_KEY", "UNUSED"))
    # The values reached the engine's `up` command only: no other command, no file in the sandbox.
    assert [c for c in sandbox.commands if KOR_KEY in c] == [c for c in sandbox.commands if " up " in c]
    assert not any(KOR_KEY in text or STRIPE_KEY in text for text in sandbox.files.values())


def test_a_secrets_file_that_lost_a_secret_after_start_stops_the_run_as_needs_setup_before_a_sandbox(
    make_worker, tmp_path, sandbox_stand_in, monkeypatch, stand_in_docker
):
    path = write_secrets(tmp_path / "s", PRODUCT, {"KORONA_API_KEY": KOR_KEY, "STRIPE_KEY": STRIPE_KEY})
    worker = worker_for(make_worker, tmp_path, SecretStore(path.parent), recipe=RECIPE)
    real_pre_check = worker._needs_setup

    def edited_after_check(*args, **kwargs):
        # The human edits the file between the pre-check in `start` and the run itself.
        problem = real_pre_check(*args, **kwargs)
        write_secrets(path.parent, PRODUCT, {"KORONA_API_KEY": KOR_KEY})
        return problem

    monkeypatch.setattr(worker, "_needs_setup", edited_after_check)
    try:
        started = worker.start(probe_request({"end": "succeed"}, repos=["svc"]))
        assert isinstance(started, Started)
        final = wait_until_ended(worker, started.run_id, timeout=30)
    finally:
        worker.shutdown()
    assert final.outcome is Outcome.NEEDS_SETUP and "svc" in final.reason and "STRIPE_KEY" in final.reason
    assert sandbox_stand_in == [], "a sandbox was started"
    assert worker.settings.subscriptions.in_use("probe-subscription") == 0, "the lease was not released"


# ----------------------------------------------------------------------------- a dependency Repo (#50)


def test_a_dependency_repos_secrets_are_read_when_its_recipe_is_loaded_and_scrubbed_like_the_others(secrets_dir):
    from types import SimpleNamespace

    from workflow_weave.agent_worker.worker import AgentWorker

    path = secrets_dir / f"{PRODUCT}.yaml"
    path.write_text(f"KORONA_API_KEY: {KOR_KEY}\nUNUSED: unused-value-1\n")
    path.chmod(0o600)
    notes: list[str] = []
    worker = object.__new__(AgentWorker)
    worker.settings = SimpleNamespace(test_secrets=SecretStore(secrets_dir))
    worker._note = lambda rec, line: notes.append(line)
    worker._save = lambda rec: None
    rec = RunRecord.__new__(RunRecord)
    rec.test_secrets_given = None
    run = SimpleNamespace(test_secrets={}, record=rec, request=SimpleNamespace(product=PRODUCT))

    got = worker._secrets_for_recipe(run, "dep", parse_recipe("dep", "x-weave:\n  secrets: [KORONA_API_KEY]\n"))
    assert got == {"KORONA_API_KEY": KOR_KEY}
    assert run.test_secrets == {"dep": got}, "the run scrubs this value from everything it records"
    assert rec.test_secrets_given == {"dep": ["KORONA_API_KEY"]}
    assert not any(KOR_KEY in n or "unused" in n for n in notes)

    with pytest.raises(EnvironmentBringUpError) as e:
        worker._secrets_for_recipe(run, "dep", parse_recipe("dep", "x-weave:\n  secrets: [NOPE]\n"))
    assert e.value.outcome is Outcome.NEEDS_SETUP and "NOPE" in e.value.reason
