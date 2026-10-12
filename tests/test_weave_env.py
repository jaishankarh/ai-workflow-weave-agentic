"""The `weave-env` command, the agent's one way to manage its Environment (#54).

Pure logic: the command's entry point is driven with a fake shell (it records each command it would
run in the sandbox and answers from a script), a manifest file and a fake clock; no Docker. (The
same command on a real sysbox sandbox is in test_weave_env_on_sysbox.py.) Each test is named after
an acceptance criterion of #54.
"""

from __future__ import annotations

import io
import json
import shlex
from pathlib import Path

import pytest
import yaml

from workflow_weave.agent_worker import weave_env
from workflow_weave.agent_worker.environment import NETWORK, Environment, parse_recipe

SVC = """\
services:
  web: {image: python:3.12-slim}
  db: {image: postgres:16-alpine}
x-weave:
  secrets: [DB_PASSWORD]
  readiness:
    web: {command: 'curl -f localhost:8000', timeout: 10, interval: 2}
    db: {command: [pg_isready], timeout: 10, interval: 2}
  seed: {service: db, command: 'psql -f /seed.sql'}
"""
CLIENT = "x-weave:\n  depends_on: [svc]\n"


class Shell:
    """Plays the sandbox's shell for the command: records commands, answers from `script(command)`."""

    def __init__(self, script=None):
        self.script = script or (lambda c: None)
        self.commands: list[str] = []
        self.now = 0.0

    def __call__(self, command, timeout=120):
        self.commands.append(command)
        answer = self.script(command)
        if answer is not None:
            return answer
        if " ps " in command and " -q " in command:
            return 0, f"c-{command.split()[-1]}\n"
        if command.startswith("docker inspect") and "State.Status" in command:
            return 0, "running\n"
        if command.startswith("docker inspect") and "IPAddress" in command:
            return 0, "10.0.0.5\n"
        if command.startswith("docker inspect"):
            return 0, "DB_PASSWORD=hunter2\nPATH=/bin\n"
        return 0, ""

    def sleep(self, s):
        self.now += s

    def clock(self):
        return self.now


class FakeSandbox:
    def __init__(self):
        self.files, self.commands = {}, []

    def run(self, command, timeout=120, cwd="/"):
        self.commands.append(command)
        if " ps " in command and " -q " in command:
            return 0, f"c-{command.split()[-1]}\n"
        return 0, "10.0.0.2\n" if command.startswith("docker inspect") else ""

    def put_text(self, path, text):
        self.files[path] = text


def manifest_for(tmp_path: Path, repos=(("svc", SVC), ("client", CLIENT))) -> Path:
    """The manifest the worker would write for these Repos brought up in order."""
    entries = []
    for name, text in repos:
        env = Environment(FakeSandbox(), name, now=lambda: "t", working_copy=f"/workspace/{name}",
                          secrets={"DB_PASSWORD": "hunter2"})
        env.bring_up(parse_recipe(name, text))
        entries.append({"repo": name, "source": "working copy", "branch": "main", "touched": True,
                        **env.describe()})
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(weave_env.manifest(entries, NETWORK)))
    return path


def weave(tmp_path, *argv, shell=None, manifest=True, hosts="127.0.0.1 localhost\n"):
    """Run the command; returns (exit status, what it printed, the shell, the hosts file)."""
    shell = shell or Shell()
    path = manifest_for(tmp_path) if manifest is True else (manifest or tmp_path / "absent.json")
    hosts_file = tmp_path / "hosts"
    hosts_file.write_text(hosts)
    out = io.StringIO()
    code = weave_env.main(
        list(argv), manifest_path=path, shell=shell, hosts_path=hosts_file, out=out,
        clock=shell.clock, sleep=shell.sleep,
    )
    return code, out.getvalue(), shell, hosts_file


# ---- a run on a Repo with no Environment gets a clear message rather than a crash

@pytest.mark.parametrize("argv", [["status"], ["logs", "svc"], ["rebuild", "svc"], ["reset"]])
def test_a_run_on_a_repo_with_no_environment_gets_a_clear_message_from_the_command_rather_than_a_crash(
    tmp_path, argv
):
    code, out, shell, _ = weave(tmp_path, *argv, manifest=False)
    assert code != 0
    assert "no Environment" in out and "Run recipe" in out
    assert "Traceback" not in out
    assert shell.commands == []


def test_a_manifest_that_cannot_be_read_is_reported_not_crashed_on(tmp_path):
    bad = tmp_path / "manifest.json"
    bad.write_text("{not json")
    code, out, _, _ = weave(tmp_path, "status", manifest=bad)
    assert code != 0 and "manifest" in out and "Traceback" not in out


# ---- status lists every service and whether it is ready

def test_status_lists_every_service_and_whether_it_is_ready(tmp_path):
    def script(command):
        if "exec" in command and "pg_isready" in command:
            return 1, "no response"
        return None

    code, out, shell, _ = weave(tmp_path, "status", shell=Shell(script))
    assert code == 0
    lines = {tuple(line.split()[:2]): line for line in out.splitlines() if line.startswith(("svc", "client"))}
    assert "ready" in lines[("svc", "web")] and "not ready" not in lines[("svc", "web")]
    assert "not ready" in lines[("svc", "db")]
    assert "web.svc" in lines[("svc", "web")]
    # A Repo whose recipe has no services of its own is still listed, as such.
    assert "client" in out and "no services" in out
    # Each Repo's own Compose project is asked, never raw Docker names.
    assert any(shlex.split(c)[:5] == ["docker", "compose", "-p", "svc", "-f"] for c in shell.commands)


def test_status_says_a_service_whose_container_is_not_running_is_not_ready_without_running_its_check(tmp_path):
    def script(command):
        if command.startswith("docker inspect") and "State.Status" in command and "c-db" in command:
            return 0, "exited\n"
        return None

    code, out, shell, _ = weave(tmp_path, "status", shell=Shell(script))
    db = next(line for line in out.splitlines() if line.startswith("svc") and " db " in f" {line} ")
    assert "exited" in db and "not ready" in db
    assert not any("pg_isready" in c for c in shell.commands)


# ---- logs returns a named service's recent output

def test_logs_returns_a_named_services_recent_output(tmp_path):
    def script(command):
        if " logs " in command:
            return 0, "web  | listening on 8000\n"
        return None

    code, out, shell, _ = weave(tmp_path, "logs", "svc", "web", shell=Shell(script))
    assert code == 0 and "listening on 8000" in out
    (cmd,) = [c for c in shell.commands if " logs " in c]
    argv = shlex.split(cmd)
    assert argv[:5] == ["docker", "compose", "-p", "svc", "-f"]
    assert argv[argv.index("logs"):][:2] == ["logs", "--no-color"] and argv[-1] == "web"
    assert "--tail" in argv


def test_logs_without_a_service_returns_all_of_the_repos_services(tmp_path):
    code, _, shell, _ = weave(tmp_path, "logs", "svc", shell=Shell(lambda c: (0, "x\n") if " logs " in c else None))
    (cmd,) = [c for c in shell.commands if " logs " in c]
    assert code == 0 and shlex.split(cmd)[-2:] == ["--tail", "200"]


def test_logs_never_show_the_value_of_a_test_secret(tmp_path):
    code, out, _, _ = weave(
        tmp_path, "logs", "svc", "db", shell=Shell(lambda c: (0, "db | password is hunter2\n") if " logs " in c else None)
    )
    assert code == 0 and "hunter2" not in out and "password is ***" in out


# ---- the command works for every Repo and gives a clear error for an unknown Repo or service

@pytest.mark.parametrize("argv", [["logs", "ghost"], ["logs", "ghost", "web"], ["rebuild", "ghost"]])
def test_an_unknown_repo_gets_a_clear_error_naming_the_repos_there_are(tmp_path, argv):
    code, out, shell, _ = weave(tmp_path, *argv)
    assert code != 0 and "ghost" in out and "svc" in out and "client" in out and "Traceback" not in out
    assert not any(" logs " in c or " up " in c for c in shell.commands)


def test_an_unknown_service_gets_a_clear_error_naming_the_services_the_repo_has(tmp_path):
    code, out, shell, _ = weave(tmp_path, "logs", "svc", "ghost")
    assert code != 0 and "ghost" in out and "web" in out and "db" in out
    assert not any(" logs " in c for c in shell.commands)


def test_a_repo_with_no_services_of_its_own_says_so_rather_than_failing_obscurely(tmp_path):
    for argv in (["logs", "client"], ["rebuild", "client"]):
        code, out, _, _ = weave(tmp_path, *argv)
        assert code != 0 and "client" in out and "no services" in out and "Traceback" not in out
