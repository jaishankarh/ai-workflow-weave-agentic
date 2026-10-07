"""The probe's MCP report (used by test_environment_mcp_on_sysbox.py), checked here against a tiny
stdio MCP server so the probe's client path is exercised without Docker.

The probe acts as Claude Code does for user-level servers: it reads `~/.claude.json` `mcpServers`,
starts each server with exactly the command, arguments and environment configured there, and calls
the tools the script names.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

PROBE = Path(__file__).parent / "probe" / "probe_agent.py"

FAKE_SERVER = '''\
import os
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("fake")

@mcp.tool()
def echo(text: str) -> str:
    return f"{os.environ.get('FAKE_DB', 'unset')}:{text}"

mcp.run()
'''


@pytest.fixture
def probe():
    spec = importlib.util.spec_from_file_location("probe_agent_under_test", PROBE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def configure(home: Path, tmp_path: Path, servers: dict) -> None:
    (home / ".claude.json").write_text(json.dumps({"mcpServers": servers}))


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


def fake_entry(tmp_path):
    script = tmp_path / "fake_server.py"
    script.write_text(FAKE_SERVER)
    return {"type": "stdio", "command": sys.executable, "args": [str(script)], "env": {"FAKE_DB": "db.svc"}}


def test_the_probe_reports_no_mcp_servers_when_the_user_level_configuration_has_none(probe, home):
    assert probe.report_mcp({"script": {}}) == {"configured": {}}
    configure(home, home, {})
    assert probe.report_mcp({"script": {}}) == {"configured": {}}


def test_the_probe_names_the_configured_servers_without_their_environment_values(probe, home, tmp_path):
    configure(home, tmp_path, {"svc-db": fake_entry(tmp_path)})
    report = probe.report_mcp({"script": {}})
    assert list(report["configured"]) == ["svc-db"]
    entry = report["configured"]["svc-db"]
    assert entry["env_names"] == ["FAKE_DB"] and "db.svc" not in json.dumps(report)


def test_the_probe_lists_a_servers_tools_and_calls_one_with_the_servers_configured_environment(probe, home, tmp_path):
    configure(home, tmp_path, {"svc-db": fake_entry(tmp_path)})
    report = probe.report_mcp({"script": {"mcp": {
        "tools": True,
        "calls": [{"server": "svc-db", "tool": "echo", "arguments": {"text": "hello"}}],
    }}})
    assert report["tools"] == {"svc-db": ["echo"]}
    (call,) = report["calls"]
    assert call["ok"] is True and call["text"] == "db.svc:hello", call


def test_a_call_to_a_server_that_is_not_configured_or_fails_is_reported_not_raised(probe, home, tmp_path):
    configure(home, tmp_path, {"svc-db": fake_entry(tmp_path)})
    report = probe.report_mcp({"script": {"mcp": {"calls": [
        {"server": "ghost", "tool": "echo", "arguments": {}},
        {"server": "svc-db", "tool": "no-such-tool", "arguments": {}},
    ]}}})
    ghost, bad_tool = report["calls"]
    assert ghost["ok"] is False and "not configured" in ghost["error"]
    assert bad_tool["ok"] is False and bad_tool["error"]
