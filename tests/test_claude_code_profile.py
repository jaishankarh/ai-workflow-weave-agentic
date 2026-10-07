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
