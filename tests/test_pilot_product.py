"""The pilot Product, sri-aurobindo-works-chat, as configured in this repo (#44).

The config is loaded as committed. Each Repo's Coding standards are observed the way
an agent sees them: a probe run on the pilot config, with stand-in clones for its
Repos, reports the always-on file and what it points at.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from conftest import make_repo, probe_reports, probe_request, wait_until_ended
from test_coding_standards import pointed_contents

from workflow_weave.agent_worker import Outcome, load_product_config, load_subscription_store

REPO = Path(__file__).resolve().parents[1]
PILOT = "sri-aurobindo-works-chat"
PILOT_CONFIG = REPO / "config" / "products" / f"{PILOT}.yaml"
CENTRAL = REPO / yaml.safe_load((REPO / "weave.yaml").read_text())["central_skills"]["location"]
PILOT_STANDARDS = CENTRAL / "products" / PILOT / "coding-standards.md"
CHAT, AHDISMOI = "sri-aurobindo-works-chat", "ahdismoi"
CONTEXT = "# Context\n\nGlossary only.\n"
CHAT_RULES = {"CLAUDE.md": "# chat\n", ".claude/rules/backend.md": "# backend\n", ".claude/rules/frontend.md": "# fe\n"}


def test_the_pilot_product_config_loads_with_its_two_repos_on_their_base_branches():
    product = load_product_config(PILOT_CONFIG)

    assert product.name == PILOT
    assert set(product.repos) == {CHAT, AHDISMOI}
    assert {r.name: r.base_branch for r in product.repos.values()} == {CHAT: "main", AHDISMOI: "main"}
    assert all(not r.skill_overrides for r in product.repos.values())


@pytest.fixture
def pilot_worker(make_worker, tmp_path):
    """A worker on the pilot config as committed, except each Repo's `source`: the real
    Repos are not cloned here, so stand-ins with the same rules files take their place."""
    config = yaml.safe_load(PILOT_CONFIG.read_text())
    stand_ins = {
        CHAT: {"CONTEXT.md": CONTEXT, **CHAT_RULES, "src/app.py": ""},
        AHDISMOI: {"CONTEXT.md": CONTEXT, "CLAUDE.md": "# not selected: central only\n"},
    }
    for name, files in stand_ins.items():
        repo = config["repos"][name]
        repo["source"] = str(make_repo(tmp_path / "stand-ins" / name, files, branch=repo["base_branch"]))
    store = tmp_path / "pilot-subscriptions.yaml"
    store.write_text(
        "subscriptions:\n  pilot-probe:\n    agent: probe\n    cap: 2\n    env: {PROBE_TOKEN: t}\n"
        f"products:\n  {PILOT}:\n    probe: [pilot-probe]\n"
    )
    worker = make_worker(
        products=[PILOT], subscriptions=load_subscription_store(store),
        product_yaml=yaml.safe_dump(config), central_skills_location=CENTRAL,
    )
    yield worker
    worker.shutdown()


def _pointed(worker, repo: str) -> list[str]:
    """What the always-on file points the agent at, in a probe run on one pilot Repo."""
    started = worker.start(probe_request({"end": "succeed"}, product=PILOT, repos=[repo]))
    final = wait_until_ended(worker, started.run_id)
    assert final.outcome is Outcome.SUCCEEDED, final.reason
    [report] = probe_reports(worker.record(started.run_id).event_log)
    return pointed_contents(report)


def test_the_chat_repo_resolves_to_central_plus_its_claude_md_and_rules_files(pilot_worker):
    assert sorted(_pointed(pilot_worker, CHAT)) == sorted(
        [CONTEXT, PILOT_STANDARDS.read_text(), *CHAT_RULES.values()]
    )


def test_ahdismoi_resolves_to_the_central_product_standards_alone(pilot_worker):
    assert sorted(_pointed(pilot_worker, AHDISMOI)) == sorted([CONTEXT, PILOT_STANDARDS.read_text()])


def test_the_product_coding_standards_file_is_short_and_holds_the_diff_judgeable_rules():
    text = PILOT_STANDARDS.read_text()

    assert len(text.splitlines()) <= 40
    rules = re.findall(r"^## (.+)$", text, re.M)
    assert len(rules) == 3
    lowered = text.lower()
    for topic in ("hard-coded", "model name", "swallow"):
        assert topic in lowered
    # Every rule says how a reviewer judges it from a diff.
    assert lowered.count("judge:") == len(rules)


def test_no_subscription_credential_is_committed_and_the_pilot_names_subscriptions_only():
    raw = PILOT_CONFIG.read_text()
    assert not {"env", "subscriptions", "token"} & _keys(yaml.safe_load(raw))

    store = yaml.safe_load((REPO / "config" / "subscriptions.example.yaml").read_text())
    assert set(store["products"][PILOT]["claude-code"]) <= set(store["subscriptions"])
    for sub in store["subscriptions"].values():
        assert all(v.endswith("REPLACE-ME") for v in sub["env"].values())

    committed = [PILOT_CONFIG, *CENTRAL.joinpath("products", PILOT).rglob("*"), REPO / "weave.yaml"]
    for path in committed:
        if path.is_file():
            assert not re.search(r"sk-ant-[a-z0-9]+-(?!REPLACE-ME)[A-Za-z0-9_-]{8,}", path.read_text()), path


def _keys(node) -> set[str]:
    if isinstance(node, dict):
        return {str(k).lower() for k in node} | set().union(*(_keys(v) for v in node.values()))
    if isinstance(node, list):
        return set().union(*(_keys(v) for v in node)) if node else set()
    return set()
