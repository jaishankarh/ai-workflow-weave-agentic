"""Seam A test harness: real Docker sandboxes running the probe Agent profile.

Images are built once per session. Environment knobs (see README):
  WEAVE_TEST_BASE_IMAGE      base image for the sandbox image (default ubuntu:24.04)
  WEAVE_TEST_BUILD_NETWORK   value for `docker build --network` (e.g. host)
  WEAVE_TEST_SKIP_BUILD=1    use already built weave/sandbox:<tag> and weave/probe-agent:<tag>
  WEAVE_TEST_IMAGE_TAG       the <tag> above (default test); give each worktree its own
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Callable

import pytest

from workflow_weave import central_skills
from workflow_weave.agent_worker import (
    AgentProfile,
    AgentWorker,
    RepoTarget,
    RunInputs,
    RunRequest,
    WorkerSettings,
    load_product_config,
)

os.environ.setdefault("OPENHANDS_SUPPRESS_BANNER", "1")
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")

ROOT = Path(__file__).resolve().parent.parent
_TAG = os.environ.get("WEAVE_TEST_IMAGE_TAG", "test")
SANDBOX_IMAGE = f"weave/sandbox:{_TAG}"
PROBE_IMAGE = f"weave/probe-agent:{_TAG}"
PRODUCT = "probe-product"


def _docker_build(tag: str, context: Path, build_args: dict[str, str]) -> None:
    cmd = ["docker", "build", "-q", "-t", tag]
    if net := os.environ.get("WEAVE_TEST_BUILD_NETWORK"):
        cmd += ["--network", net]
    for k, v in build_args.items():
        cmd += ["--build-arg", f"{k}={v}"]
    subprocess.run([*cmd, str(context)], check=True, capture_output=True, text=True)


@pytest.fixture(scope="session")
def probe_image() -> str:
    if os.environ.get("WEAVE_TEST_SKIP_BUILD") != "1":
        base = os.environ.get("WEAVE_TEST_BASE_IMAGE", "ubuntu:24.04")
        _docker_build(SANDBOX_IMAGE, ROOT / "sandbox", {"BASE_IMAGE": base})
        _docker_build(PROBE_IMAGE, ROOT / "tests" / "probe", {"SANDBOX_IMAGE": SANDBOX_IMAGE})
    return PROBE_IMAGE


@pytest.fixture(scope="session")
def probe_profile(probe_image: str) -> AgentProfile:
    return AgentProfile(
        name="probe",
        image=probe_image,
        acp_command=["/opt/oh/bin/python", "/opt/probe/probe_agent.py"],
    )


# --------------------------------------------------------------------------- fixture Repos


def make_repo(path: Path, files: dict[str, str], branch: str = "main") -> Path:
    path.mkdir(parents=True)
    git = lambda *a: subprocess.run(["git", "-C", str(path), *a], check=True, capture_output=True)  # noqa: E731
    git("init", "-q", "-b", branch)
    git("config", "user.email", "fixture@weave.invalid")
    git("config", "user.name", "fixture")
    for rel, body in files.items():
        f = path / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(body)
    git("add", "-A")
    git("commit", "-qm", "fixture")
    return path


@pytest.fixture
def onboarded_repo(tmp_path: Path) -> Path:
    """A small Repo that has been onboarded (it has a CONTEXT.md)."""
    return make_repo(
        tmp_path / "repos" / "app",
        {"CONTEXT.md": "# Context: app\n\nGlossary only.\n", "README.md": "# app\n", "src/main.py": "print('hi')\n"},
    )


# --------------------------------------------------------------------------- worker


@pytest.fixture
def runs_dir(tmp_path: Path) -> Path:
    return tmp_path / "runs"


# This repo's own Central skills (the real upstream copy), found through weave.yaml.
REPO_CENTRAL_SKILLS = central_skills.load(ROOT / "weave.yaml").location


@pytest.fixture
def make_worker(
    tmp_path: Path, runs_dir: Path, onboarded_repo: Path, probe_profile: AgentProfile
) -> Callable[..., AgentWorker]:
    default_product = f"product: {PRODUCT}\nrepos:\n  app:\n    source: {onboarded_repo}\n"

    def make(*, product_yaml: str = default_product, central_skills_location: Path = REPO_CENTRAL_SKILLS) -> AgentWorker:
        product_file = tmp_path / "product.yaml"
        product_file.write_text(product_yaml)
        settings = WorkerSettings(
            runs_dir=runs_dir,
            products={PRODUCT: load_product_config(product_file)},
            agent_profiles={"probe": probe_profile},
            sandbox_nofile_limit=_nofile_limit(),
            central_skills_location=central_skills_location,
        )
        return AgentWorker(settings)

    return make


@pytest.fixture
def worker(make_worker: Callable[[], AgentWorker]):
    w = make_worker()
    yield w
    w.shutdown()


def probe_request(
    script: dict, *, branch: str = "story-1", skill: str = "implement-spec", repos: list[str] = ("app",)
) -> RunRequest:
    """A run request whose spec tells the probe how to behave."""
    return RunRequest(
        product=PRODUCT,
        agent_profile="probe",
        repos=[RepoTarget(name=r, integration_branch=branch, base_branch="main") for r in repos],
        skill=skill,
        inputs=RunInputs(spec=f"A spec for the probe.\nprobe: {json.dumps(script)}\n", tasks=["Task one"]),
    )


def wait_for(predicate: Callable[[], object], timeout: float = 120, interval: float = 0.5, what: str = "condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    raise AssertionError(f"timed out after {timeout}s waiting for {what}")


def wait_until_ended(worker: AgentWorker, run_id: str, timeout: float = 180):
    return wait_for(lambda: (s := worker.status(run_id)).is_final and s, timeout, what=f"run {run_id} to end")


# --------------------------------------------------------------------------- Sandbox host views


def sandboxes_of(run_id: str) -> list[str]:
    """Containers on the Sandbox host that belong to a run (running or not)."""
    out = subprocess.run(
        ["docker", "ps", "-aq", "--filter", f"label=weave.run-id={run_id}"],
        check=True, capture_output=True, text=True,
    ).stdout
    return out.split()


def sandbox_processes(container: str) -> str:
    return subprocess.run(
        ["docker", "exec", container, "ps", "-eo", "pid,stat,args"], capture_output=True, text=True
    ).stdout


def probe_reports(event_log: Path) -> list[dict]:
    """The probe's reports, read from a run's saved event log."""
    reports = []
    for line in event_log.read_text().splitlines():
        ev = json.loads(line)
        if ev.get("kind") == "ACPToolCallEvent" and ev.get("title") == "probe-report":
            reports.append(json.loads(ev["raw_output"]))
    return reports


def _nofile_limit() -> int | None:
    """WEAVE_TEST_SANDBOX_NOFILE overrides the sandbox open-files limit (0 = Docker's default)."""
    v = os.environ.get("WEAVE_TEST_SANDBOX_NOFILE")
    if v is None:
        return 65536
    return int(v) or None
