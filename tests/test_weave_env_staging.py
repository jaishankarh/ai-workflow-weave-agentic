"""How `weave-env` gets into a run's sandbox and learns what the Environment is (#54).

No Docker: a fake sandbox for the worker's side, and the staged file itself run as the agent would
run it (a plain `python3 weave-env`, nothing of this package on its path).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

from test_weave_env import CLIENT, SVC, FakeSandbox

from workflow_weave.agent_worker import weave_env
from workflow_weave.agent_worker.environment import (
    FROM_BASE_BRANCH, FROM_WORKING_COPY, Environment, Placement, environment_manifest, parse_recipe,
)
from workflow_weave.agent_worker.sandbox import stage_weave_env


class StagingSandbox(FakeSandbox):
    def sh(self, command, timeout=120, cwd="/"):
        self.commands.append(command)
        return ""


def run_staged(sandbox, tmp_path, *args):
    """Run the staged file the way the agent does: as its own program, with a clean environment."""
    script = tmp_path / "weave-env"
    script.write_text(sandbox.files[weave_env.INSTALL_PATH])
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path)}
    return subprocess.run([sys.executable, "-I", str(script), *args], capture_output=True, text=True, env=env, cwd="/")


def test_the_command_is_installed_in_the_sandbox_as_a_program_the_agent_can_run(tmp_path):
    sandbox = StagingSandbox()
    stage_weave_env(sandbox)
    text = sandbox.files[weave_env.INSTALL_PATH]
    assert text.startswith("#!") and "def main(" in text
    assert any(c.startswith("chmod +x") and weave_env.INSTALL_PATH in c for c in sandbox.commands)


def test_the_installed_command_stands_alone_and_says_plainly_when_the_run_has_no_environment(tmp_path):
    sandbox = StagingSandbox()
    stage_weave_env(sandbox)
    r = run_staged(sandbox, tmp_path, "status")
    assert r.returncode != 0 and "no Environment" in r.stdout and "Traceback" not in r.stdout + r.stderr


def test_the_manifest_describes_the_repos_in_dependency_order_with_secret_names_and_no_secret_values():
    envs, placed = [], []
    for name, text, source in (("svc", SVC, FROM_BASE_BRANCH), ("client", CLIENT, FROM_WORKING_COPY)):
        env = Environment(FakeSandbox(), name, now=lambda: "t", working_copy=f"/workspace/{name}",
                          secrets={"DB_PASSWORD": "hunter2-value"})
        env.bring_up(parse_recipe(name, text))
        envs.append(env)
        placed.append(Placement(name, source, "main", f"/workspace/{name}", True))
    data = environment_manifest(placed, envs)
    assert [r["repo"] for r in data["repos"]] == ["svc", "client"]
    svc = data["repos"][0]
    assert svc["services"] == ["web", "db"] and svc["secrets"] == ["DB_PASSWORD"]
    assert svc["seed"]["service"] == "db" and svc["readiness"]["db"]["command"] == ["pg_isready"]
    assert svc["source"] == FROM_BASE_BRANCH and svc["branch"] == "main"
    assert data["repos"][1]["services"] == [] and data["repos"][1]["depends_on"] == ["svc"]
    assert "hunter2-value" not in json.dumps(data)


def test_the_staged_command_reads_the_manifest_the_worker_writes(tmp_path):
    sandbox = StagingSandbox()
    stage_weave_env(sandbox)
    env = Environment(FakeSandbox(), "client", now=lambda: "t", working_copy="/workspace/client")
    env.bring_up(parse_recipe("client", CLIENT))
    data = environment_manifest([Placement("client", FROM_WORKING_COPY, "main", "/workspace/client", True)], [env])
    (tmp_path / "manifest.json").write_text(json.dumps(data))
    script = tmp_path / "weave-env"
    script.write_text(sandbox.files[weave_env.INSTALL_PATH].replace(weave_env.MANIFEST_PATH, str(tmp_path / "manifest.json")))
    r = subprocess.run([sys.executable, "-I", str(script), "status"], capture_output=True, text=True)
    assert r.returncode == 0 and "client" in r.stdout and "no services" in r.stdout
