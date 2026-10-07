"""Seam A: the Claude Code Agent profile on a subscription token (#43).

Each test is named after an acceptance criterion of #43. Most run the Claude Code
profile's real sandbox image with the probe standing in for Claude Code; the
invalid-token test runs the real Claude Code CLI against api.anthropic.com with a
made-up token. Tests that need a real `claude setup-token` are skipped unless
`WEAVE_CLAUDE_CODE_TOKEN` is set (see README "Checking Claude Code with a real token").
"""

from __future__ import annotations

import subprocess

# The agent CLI versions the image pins (sandbox/claude-code/Dockerfile). Changing
# them is a deliberate upgrade: update both places.
CLAUDE_CODE_VERSION = "2.1.287"
CLAUDE_AGENT_ACP_VERSION = "0.86.0"


def in_image(image: str, script: str) -> str:
    """Run a shell script in a fresh container of the image, as the sandbox's agent would see it."""
    r = subprocess.run(
        ["docker", "run", "--rm", "--entrypoint", "bash", image, "-lc", script],
        capture_output=True, text=True, timeout=120,
    )
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


def test_agent_cli_versions_are_pinned_in_the_image(claude_code_image):
    # The Claude Code CLI the ACP adapter runs, and the adapter itself.
    claude = in_image(claude_code_image, '"$CLAUDE_CODE_EXECUTABLE" --version')
    acp = in_image(
        claude_code_image,
        "npm ls -g --depth=0 --json @agentclientprotocol/claude-agent-acp"
        " | python3 -c 'import json,sys; print(json.load(sys.stdin)[\"dependencies\"]"
        "[\"@agentclientprotocol/claude-agent-acp\"][\"version\"])'",
    )

    assert claude.split()[0] == CLAUDE_CODE_VERSION
    assert acp == CLAUDE_AGENT_ACP_VERSION
    assert in_image(claude_code_image, "command -v claude-agent-acp") != ""


# --------------------------------------------------------------------------- the profile's sandbox, seen by the probe

import hashlib  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402
from conftest import PRODUCT, probe_reports, probe_request, wait_until_ended  # noqa: E402

from workflow_weave.agent_worker import Outcome, claude_code_profile, load_subscription_store  # noqa: E402

FAKE_TOKEN = "sk-ant-oat01-not-a-real-token"


def subscription_store(tmp_path: Path, env: dict[str, str]):
    store = tmp_path / "claude-subscriptions.yaml"
    lines = ["subscriptions:", "  claude-main:", "    agent: claude-code", "    cap: 5", "    env:"]
    lines += [f"      {k}: {v}" for k, v in env.items()]
    lines += ["products:", f"  {PRODUCT}:", "    claude-code: [claude-main]"]
    store.write_text("\n".join(lines) + "\n")
    return load_subscription_store(store)


@pytest.fixture
def probe_as_claude_code(probe_claude_code_image):
    """The Claude Code Agent profile, with the probe standing in for Claude Code in its image."""
    return claude_code_profile(
        image=probe_claude_code_image,
        acp_command=["/opt/oh/bin/python", "/opt/probe/probe_agent.py"],
    )


def run_as_claude_code(make_worker, profile, store, script: dict, **kw):
    worker = make_worker(subscriptions=store, agent_profiles={"claude-code": profile})
    try:
        started = worker.start(probe_request(script, agent_profile="claude-code", **kw))
        final = wait_until_ended(worker, started.run_id)
        record = worker.record(started.run_id)
        reports = probe_reports(record.event_log) if record.event_log and record.event_log.exists() else []
        return final, record, (reports[0] if reports else None)
    finally:
        worker.shutdown()


def test_the_claude_code_sandbox_env_holds_the_oauth_token_and_no_api_key_or_base_url_even_if_set_on_the_host(
    make_worker, probe_as_claude_code, tmp_path, monkeypatch
):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-host-key")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://host-proxy.invalid")
    # Even a Subscription whose credential env names them must not get them into the sandbox.
    store = subscription_store(tmp_path, {
        "CLAUDE_CODE_OAUTH_TOKEN": FAKE_TOKEN,
        "ANTHROPIC_API_KEY": "sk-ant-api03-subscription-key",
        "ANTHROPIC_BASE_URL": "http://subscription-proxy.invalid",
    })
    final, record, report = run_as_claude_code(
        make_worker, probe_as_claude_code, store,
        {"end": "succeed", "fingerprint_env": ["CLAUDE_CODE_OAUTH_TOKEN"]},
    )

    assert final.outcome is Outcome.SUCCEEDED, final.reason
    assert "CLAUDE_CODE_OAUTH_TOKEN" in report["env_names"]
    assert report["env_fingerprints"]["CLAUDE_CODE_OAUTH_TOKEN"] == hashlib.sha256(FAKE_TOKEN.encode()).hexdigest()
    assert "ANTHROPIC_API_KEY" not in report["env_names"]
    assert "ANTHROPIC_BASE_URL" not in report["env_names"]


# --------------------------------------------------------------------------- the real Claude Code CLI


@pytest.fixture
def real_claude_code(claude_code_image):
    return claude_code_profile(image=claude_code_image)


def test_an_invalid_token_is_needs_setup_within_seconds(make_worker, real_claude_code, tmp_path):
    # The real Claude Code CLI, asking api.anthropic.com with a token it has never issued.
    store = subscription_store(tmp_path, {"CLAUDE_CODE_OAUTH_TOKEN": FAKE_TOKEN})
    final, record, _ = run_as_claude_code(make_worker, real_claude_code, store, {"end": "succeed"})

    assert final.outcome is Outcome.NEEDS_SETUP, final.reason
    # The reason names the problem and Claude Code's own error (its API's 401).
    assert "credential" in final.reason and "401" in final.reason, (final.reason, record.event_log.read_text()[-3000:])
    first, last = _event_span(record.event_log)
    assert (last - first).total_seconds() < 30, "the rejected token was retried instead of reported"
    assert FAKE_TOKEN not in (record.event_log.read_text() + (tmp_path / "runs").joinpath(record.run_id, "record.json").read_text())


def _event_span(event_log: Path):
    import json
    from datetime import datetime

    times = [datetime.fromisoformat(json.loads(line)["timestamp"]) for line in event_log.read_text().splitlines()]
    assert times, "empty event log"
    return min(times), max(times)


# --------------------------------------------------------------------------- the real Claude Code, on a scripted fake API


@pytest.fixture
def claude_code_on_fake_api(fake_api_claude_code_image):
    """The real Claude Code Agent profile, its model answers scripted (tests/fake_anthropic/)."""
    return claude_code_profile(image=fake_api_claude_code_image)


def agent_said(event_log: Path) -> str:
    """Everything the agent replied in the run, from the saved event log."""
    import json

    said = []
    for line in event_log.read_text().splitlines():
        ev = json.loads(line)
        if ev.get("kind") == "StreamingDeltaEvent" and ev.get("source") == "agent":
            said.append(ev.get("content") or "")
    return "".join(said)


def test_permissions_are_auto_approved_and_a_run_needs_no_interactive_input(
    make_worker, claude_code_on_fake_api, tmp_path
):
    # Each of these asks for permission in Claude Code's default mode: a shell command,
    # and a file written outside the working directory.
    store = subscription_store(tmp_path, {"CLAUDE_CODE_OAUTH_TOKEN": FAKE_TOKEN})
    turns = [
        {"tool": "Bash", "input": {"command": "echo RAN-$((6*7))", "description": "run a command"}},
        {"tool": "Write", "input": {"file_path": "/etc/weave-permission-check", "content": "WROTE-OUTSIDE\n"}},
        {"tool": "Bash", "input": {"command": "cat /etc/weave-permission-check", "description": "read it back"}},
        {"seen": ["RAN-42", "WROTE-OUTSIDE"]},
    ]
    final, record, _ = run_as_claude_code(make_worker, claude_code_on_fake_api, store, {"turns": turns})

    assert final.outcome is Outcome.SUCCEEDED, final.reason
    assert "seen: RAN-42, WROTE-OUTSIDE" in agent_said(record.event_log)


def clash_fixture(tmp_path: Path, override: bool):
    """Central skills and a Repo that both have a `clash` skill; the run's skill calls it."""
    from conftest import make_repo

    def skill(name: str, body: str) -> str:
        return f"---\nname: {name}\ndescription: the {name} skill\n---\n\n{body}\n"

    central = tmp_path / "central" / "skills"
    for rel, text in {
        "upstream/UPSTREAM.json": '{"repository": "https://example.invalid/skills.git", "commit": "%s"}' % ("b" * 40),
        "upstream/engineering/work/SKILL.md": skill("work", "Call the Skill tool with `clash`."),
        "upstream/engineering/clash/SKILL.md": skill("clash", "CENTRAL-CLASH-BODY"),
    }.items():
        (central / rel).parent.mkdir(parents=True, exist_ok=True)
        (central / rel).write_text(text)
    repo = make_repo(tmp_path / "clash-repos" / "app", {
        "CONTEXT.md": "# Context: app\n", "README.md": "README-OF-APP\n",
        ".claude/skills/clash/SKILL.md": skill("clash", "REPO-CLASH-BODY"),
    })
    product = f"product: {PRODUCT}\nrepos:\n  app:\n    source: {repo}\n"
    if override:
        product += "    skill_overrides: [clash]\n"
    return central, product


# What the agent does: read a file in the Repo (Claude Code then discovers the Repo's own
# skills), load `clash` by name, and try the Repo's directory-scoped variant by its own name.
CLASH_TURNS = [
    {"tool": "Read", "input": {"file_path": "/workspace/app/README.md"}},
    {"tool": "Skill", "input": {"skill": "clash"}},
    {"tool": "Skill", "input": {"skill": "app:clash"}},
    {"seen": ["README-OF-APP", "CENTRAL-CLASH-BODY", "REPO-CLASH-BODY", "Directory-scoped variants"]},
]


def test_with_a_central_and_a_project_skill_of_the_same_name_claude_code_loads_the_central_one(
    make_worker, claude_code_on_fake_api, tmp_path
):
    central, product = clash_fixture(tmp_path, override=False)
    store = subscription_store(tmp_path, {"CLAUDE_CODE_OAUTH_TOKEN": FAKE_TOKEN})
    worker = make_worker(subscriptions=store, agent_profiles={"claude-code": claude_code_on_fake_api},
                         product_yaml=product, central_skills_location=central)
    try:
        started = worker.start(probe_request({"turns": CLASH_TURNS}, agent_profile="claude-code", skill="work"))
        final = wait_until_ended(worker, started.run_id)
        said = agent_said(worker.record(started.run_id).event_log)
    finally:
        worker.shutdown()

    assert final.outcome is Outcome.SUCCEEDED, final.reason
    # The central skill loads, and Claude Code is neither shown nor able to load the
    # Repo's same-named skill.
    assert "seen: README-OF-APP, CENTRAL-CLASH-BODY\n" in said, said


def test_with_a_skill_override_claude_code_loads_the_repos_own_skill(make_worker, claude_code_on_fake_api, tmp_path):
    central, product = clash_fixture(tmp_path, override=True)
    store = subscription_store(tmp_path, {"CLAUDE_CODE_OAUTH_TOKEN": FAKE_TOKEN})
    worker = make_worker(subscriptions=store, agent_profiles={"claude-code": claude_code_on_fake_api},
                         product_yaml=product, central_skills_location=central)
    try:
        started = worker.start(probe_request({"turns": CLASH_TURNS}, agent_profile="claude-code", skill="work"))
        final = wait_until_ended(worker, started.run_id)
        said = agent_said(worker.record(started.run_id).event_log)
    finally:
        worker.shutdown()

    assert final.outcome is Outcome.SUCCEEDED, final.reason
    assert "REPO-CLASH-BODY" in said and "CENTRAL-CLASH-BODY" not in said, said
