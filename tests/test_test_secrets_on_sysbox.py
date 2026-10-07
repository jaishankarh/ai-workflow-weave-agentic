"""Seam A: Test secrets reach only the services whose recipe names them (#52).

Each test is named after an acceptance criterion of #52. Every test here runs a real sandbox on the
sysbox runtime, pulls alpine inside it from the internet, and is SKIPPED on a host that does not
list `sysbox-runc` in `docker info`. (The file loading, per-Product isolation, name resolution,
permission check and redaction are covered without Docker in test_test_secrets.py, which also
runs a whole run against a stand-in sandbox.)

NOT YET RUN: written on a host without sysbox. Expect to adjust on first real run.
"""

from __future__ import annotations

import yaml
from conftest import (
    PRODUCT, needs_sysbox, probe_reports, probe_request, product_yaml_for, repo_with_recipe, wait_until_ended,
)

from workflow_weave.agent_worker import NeedsSetup, Outcome, Started
from workflow_weave.agent_worker.secret_store import SecretStore

pytestmark = needs_sysbox

NAMED_VALUE = "kor-test-key-on-sysbox-91ac"
UNNAMED_VALUE = "stripe-test-key-on-sysbox-5be2"

RECIPE = """\
services:
  web:
    image: alpine:3.20
    command: sleep 600
x-weave:
  secrets: [KORONA_API_KEY]
  readiness:
    web: {command: 'true'}
"""


def _worker(make_worker, tmp_path, recipe=RECIPE, secrets=None):
    repo = repo_with_recipe(tmp_path / "recipe-repos", "svc", recipe)
    directory = tmp_path / "host-secrets"
    directory.mkdir()
    file = directory / f"{PRODUCT}.yaml"
    file.write_text(yaml.safe_dump(secrets if secrets is not None else
                                   {"KORONA_API_KEY": NAMED_VALUE, "STRIPE_KEY": UNNAMED_VALUE}))
    file.chmod(0o600)
    worker = make_worker(product_yaml=product_yaml_for({"svc": repo}, PRODUCT))
    worker.settings = worker.settings.__class__(**{**vars(worker.settings), "test_secrets": SecretStore(directory)})
    return worker


def _run(worker, script, timeout=600):
    started = worker.start(probe_request(script, repos=["svc"]))
    assert isinstance(started, Started)
    return started, wait_until_ended(worker, started.run_id, timeout=timeout)


def test_a_recipe_naming_a_secret_the_product_has_receives_its_value_in_its_service_and_not_the_agents_sandbox(
    make_worker, tmp_path
):
    worker = _worker(make_worker, tmp_path)
    look = [{"repo": "svc", "service": "web", "command": "printenv KORONA_API_KEY"}]
    try:
        started, final = _run(worker, {"end": "succeed", "exec_in_service": look})
        (report,) = probe_reports(worker.record(started.run_id).event_log)
    finally:
        worker.shutdown()
    assert final.outcome is Outcome.SUCCEEDED, final.reason
    assert report["environment"]["exec"][0]["output"].strip() == NAMED_VALUE
    assert not {"KORONA_API_KEY", "STRIPE_KEY"} & set(report.get("env_names", []))


def test_a_secret_the_recipe_does_not_name_is_absent_from_every_service(make_worker, tmp_path):
    worker = _worker(make_worker, tmp_path)
    look = [{"repo": "svc", "service": "web", "command": "printenv STRIPE_KEY; echo exit=$?"}]
    try:
        started, final = _run(worker, {"end": "succeed", "exec_in_service": look})
        (report,) = probe_reports(worker.record(started.run_id).event_log)
    finally:
        worker.shutdown()
    assert final.outcome is Outcome.SUCCEEDED, final.reason
    out = report["environment"]["exec"][0]["output"]
    assert UNNAMED_VALUE not in out and "exit=1" in out


def test_a_named_secret_the_product_lacks_gives_needs_setup_naming_the_repo_and_the_secret_and_no_sandbox_starts(
    make_worker, tmp_path
):
    worker = _worker(make_worker, tmp_path, secrets={"STRIPE_KEY": UNNAMED_VALUE})
    try:
        result = worker.start(probe_request({"end": "succeed"}, repos=["svc"]))
    finally:
        worker.shutdown()
    # `start` refused it: there is no run, so no run id that could have a sandbox.
    assert isinstance(result, NeedsSetup) and result.outcome is Outcome.NEEDS_SETUP
    assert "svc" in result.reason and "KORONA_API_KEY" in result.reason


def test_the_run_record_lists_the_secret_names_given_to_a_run_never_their_values(make_worker, tmp_path):
    worker = _worker(make_worker, tmp_path)
    try:
        started, final = _run(worker, {"end": "succeed"})
        record = worker.record(started.run_id)
        run_dir = worker.settings.runs_dir / started.run_id
        written = "".join(p.read_text(errors="replace") for p in run_dir.rglob("*") if p.is_file())
    finally:
        worker.shutdown()
    assert record.test_secrets_given == {"svc": ["KORONA_API_KEY"]}
    assert NAMED_VALUE not in written and UNNAMED_VALUE not in written
