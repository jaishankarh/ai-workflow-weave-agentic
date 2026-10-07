"""The pilot Product, sri-aurobindo-works-chat, as configured in this repo (#44).

No sandboxes: these tests stop at loading the Product config and resolving each
Repo's Coding standards against the Central skills this repo ships.
"""

from __future__ import annotations

import dataclasses
import re
from pathlib import Path

import yaml

from conftest import make_repo

from workflow_weave.agent_worker import load_product_config
from workflow_weave.agent_worker.standards import product_standards_path, resolve

REPO = Path(__file__).resolve().parents[1]
PILOT = "sri-aurobindo-works-chat"
PILOT_CONFIG = REPO / "config" / "products" / f"{PILOT}.yaml"
CENTRAL = REPO / yaml.safe_load((REPO / "weave.yaml").read_text())["central_skills"]["location"]
CHAT, AHDISMOI = "sri-aurobindo-works-chat", "ahdismoi"


def test_the_pilot_product_config_loads_with_its_two_repos_on_their_base_branches():
    product = load_product_config(PILOT_CONFIG)

    assert product.name == PILOT
    assert set(product.repos) == {CHAT, AHDISMOI}
    assert {r.name: r.base_branch for r in product.repos.values()} == {CHAT: "main", AHDISMOI: "main"}
    assert all(not r.skill_overrides for r in product.repos.values())


def test_the_chat_repo_resolves_to_central_plus_its_claude_md_and_rules_files(tmp_path):
    product = load_product_config(PILOT_CONFIG)
    chat = product.repos[CHAT]
    assert (chat.coding_standards, chat.rules_files) == ("central+repo", ("CLAUDE.md", ".claude/rules"))

    # The real chat repo is not cloned here: a stand-in with the same rules files takes its place.
    source = make_repo(tmp_path / "chat", {
        "CLAUDE.md": "# chat\n", ".claude/rules/backend.md": "# backend\n", ".claude/rules/frontend.md": "# fe\n",
        "src/app.py": "",
    }, branch=chat.base_branch)
    product = dataclasses.replace(product, repos={CHAT: dataclasses.replace(chat, source=str(source))})

    resolved = resolve(product, [(CHAT, chat.base_branch)], CENTRAL)

    [repo] = resolved.repos
    assert repo.mode == "central+repo"
    assert [f.staged_as.removeprefix("weave/coding-standards/") for f in repo.files] == [
        "product/coding-standards.md",
        f"repos/{CHAT}/CLAUDE.md",
        f"repos/{CHAT}/.claude/rules/backend.md",
        f"repos/{CHAT}/.claude/rules/frontend.md",
    ]


def test_ahdismoi_resolves_to_the_central_product_standards_alone():
    product = load_product_config(PILOT_CONFIG)
    ahdismoi = product.repos[AHDISMOI]
    assert (ahdismoi.coding_standards, ahdismoi.rules_files) == ("central", ())

    resolved = resolve(product, [(AHDISMOI, ahdismoi.base_branch)], CENTRAL)

    [repo] = resolved.repos
    assert [f.staged_as for f in repo.files] == ["weave/coding-standards/product/coding-standards.md"]
    assert repo.files[0].content == product_standards_path(CENTRAL, PILOT).read_bytes()


def test_the_product_coding_standards_file_is_short_and_holds_the_diff_judgeable_rules():
    text = product_standards_path(CENTRAL, PILOT).read_text()

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
