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
import shutil
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
    load_subscription_store,
)

os.environ.setdefault("OPENHANDS_SUPPRESS_BANNER", "1")
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")

ROOT = Path(__file__).resolve().parent.parent
_TAG = os.environ.get("WEAVE_TEST_IMAGE_TAG", "test")
SANDBOX_IMAGE = f"weave/sandbox:{_TAG}"
PROBE_IMAGE = f"weave/probe-agent:{_TAG}"
CLAUDE_CODE_IMAGE = f"weave/claude-code:{_TAG}"
# The Claude Code image with the probe added: what the Claude Code Agent profile's
# sandbox gives an agent, seen by the probe instead of Claude Code.
PROBE_CLAUDE_CODE_IMAGE = f"weave/probe-claude-code:{_TAG}"
# TEST ONLY: the Claude Code image with Claude Code pointed at a scripted fake Messages API.
FAKE_API_CLAUDE_CODE_IMAGE = f"weave/claude-code-fake-api:{_TAG}"
PRODUCT = "probe-product"
# The Subscription the default Product leases: roomy enough never to be full.
PROBE_SUBSCRIPTION = "probe-subscription"


def _docker_build(tag: str, context: Path, build_args: dict[str, str]) -> None:
    cmd = ["docker", "build", "-q", "-t", tag]
    if net := os.environ.get("WEAVE_TEST_BUILD_NETWORK"):
        cmd += ["--network", net]
    for k, v in build_args.items():
        cmd += ["--build-arg", f"{k}={v}"]
    subprocess.run([*cmd, str(context)], check=True, capture_output=True, text=True)


def _building() -> bool:
    return os.environ.get("WEAVE_TEST_SKIP_BUILD") != "1"


@pytest.fixture(scope="session")
def sandbox_image() -> str:
    if _building():
        base = os.environ.get("WEAVE_TEST_BASE_IMAGE", "ubuntu:24.04")
        _docker_build(SANDBOX_IMAGE, ROOT / "sandbox", {"BASE_IMAGE": base})
    return SANDBOX_IMAGE


@pytest.fixture(scope="session")
def probe_image(sandbox_image: str) -> str:
    if _building():
        _docker_build(PROBE_IMAGE, ROOT / "tests" / "probe", {"SANDBOX_IMAGE": sandbox_image})
    return PROBE_IMAGE


@pytest.fixture(scope="session")
def claude_code_image(sandbox_image: str) -> str:
    """The Claude Code Agent profile's image (sandbox/claude-code/), with its pinned agent CLIs."""
    if _building():
        _docker_build(CLAUDE_CODE_IMAGE, ROOT / "sandbox" / "claude-code", {"SANDBOX_IMAGE": sandbox_image})
    return CLAUDE_CODE_IMAGE


@pytest.fixture(scope="session")
def fake_api_claude_code_image(claude_code_image: str) -> str:
    if _building():
        _docker_build(FAKE_API_CLAUDE_CODE_IMAGE, ROOT / "tests" / "fake_anthropic",
                      {"CLAUDE_CODE_IMAGE": claude_code_image})
    return FAKE_API_CLAUDE_CODE_IMAGE


@pytest.fixture(scope="session")
def probe_claude_code_image(claude_code_image: str) -> str:
    if _building():
        _docker_build(PROBE_CLAUDE_CODE_IMAGE, ROOT / "tests" / "probe", {"SANDBOX_IMAGE": claude_code_image})
    return PROBE_CLAUDE_CODE_IMAGE


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


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout.strip()


def make_code_host(root: Path, files: dict[str, str]) -> tuple[Path, Path]:
    """A bare repo acting as the Code host, and the Sandbox host's clone of it (remote `origin`)."""
    seed = make_repo(root / "seed", files)
    code_host = root / "code-host" / "app.git"
    _git("clone", "-q", "--bare", str(seed), str(code_host))
    clone = root / "sandbox-host" / "app"
    _git("clone", "-q", str(code_host), str(clone))
    return code_host, clone


def commit_on_code_host(code_host: Path, files: dict[str, str], branch: str = "main") -> str:
    """Someone else lands a commit on the Code host's branch; returns its commit."""
    work = code_host.parent / f"elsewhere-{len(list(code_host.parent.iterdir()))}"
    _git("clone", "-q", "--branch", branch, str(code_host), str(work))
    for rel, body in files.items():
        (work / rel).parent.mkdir(parents=True, exist_ok=True)
        (work / rel).write_text(body)
    _git("-C", str(work), "add", "-A")
    _git("-C", str(work), "-c", "user.email=else@weave.invalid", "-c", "user.name=else", "commit", "-qm", "later")
    _git("-C", str(work), "push", "-q", "origin", branch)
    return _git("-C", str(work), "rev-parse", "HEAD")


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


def add_product_standards(location: Path, products: list[str]) -> None:
    """Give each Product a coding-standards file in a Central skills location (#41)."""
    for name in products:
        f = location / "products" / name / "coding-standards.md"
        if not f.exists():
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(f"# Coding standards: {name}\n\n- No swallowed errors.\n")


@pytest.fixture
def make_worker(
    tmp_path: Path, runs_dir: Path, onboarded_repo: Path, probe_profile: AgentProfile
) -> Callable[..., AgentWorker]:
    """Build a worker. `products` names Products that each have the onboarded Repo
    (`product_yaml`, if given, replaces the first Product's config); `subscriptions` is the
    Subscription store (default: one roomy Subscription for PRODUCT)."""
    default_store = tmp_path / "subscriptions.yaml"
    default_store.write_text(
        f"subscriptions:\n  {PROBE_SUBSCRIPTION}:\n    agent: probe\n    cap: 10\n"
        f"    env: {{PROBE_TOKEN: probe-token}}\n"
        f"products:\n  {PRODUCT}:\n    probe: [{PROBE_SUBSCRIPTION}]\n"
    )

    def product_config(name: str, product_yaml: str | None):
        product_file = tmp_path / f"product-{name}.yaml"
        if product_yaml is not None:
            product_file.write_text(product_yaml)
        else:
            product_file.write_text(f"product: {name}\nrepos:\n  app:\n    source: {onboarded_repo}\n")
        return load_product_config(product_file)

    def make(
        products: list[str] = [PRODUCT],  # noqa: B006
        subscriptions=None,
        *,
        product_yaml: str | None = None,
        central_skills_location: Path | None = None,
        agent_profiles: dict[str, AgentProfile] | None = None,
    ) -> AgentWorker:
        if central_skills_location is None:
            # This repo's Central skills, plus a coding-standards file for each test Product.
            central_skills_location = tmp_path / "repo-central-skills"
            if not central_skills_location.exists():
                shutil.copytree(REPO_CENTRAL_SKILLS, central_skills_location,
                                ignore=shutil.ignore_patterns("products"))
            add_product_standards(central_skills_location, products)
        settings = WorkerSettings(
            runs_dir=runs_dir,
            products={
                name: product_config(name, product_yaml if i == 0 else None) for i, name in enumerate(products)
            },
            agent_profiles={"probe": probe_profile, **(agent_profiles or {})},
            subscriptions=subscriptions or load_subscription_store(default_store),
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
    script: dict,
    *,
    branch: str = "story-1",
    product: str = PRODUCT,
    skill: str = "implement-spec",
    repos: list[str] = ("app",),
    agent_profile: str = "probe",
) -> RunRequest:
    """A run request whose spec tells the probe how to behave."""
    return RunRequest(
        product=product,
        agent_profile=agent_profile,
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


def wait_until_hanging(worker: AgentWorker, run_id: str, command: str) -> str:
    """Wait until a hang-scripted probe is running its command; return the run's sandbox.

    Watches the run's live event log for the probe's "hanging command" tool call
    (sent just after the command started) rather than polling the sandbox, and fails
    at once if the run ends first. The budget covers a slow sandbox start under load.
    """
    budget = worker.settings.sandbox_start_timeout + 180

    def hanging() -> bool:
        status = worker.status(run_id)
        assert not status.is_final, f"run ended before its command hung: {status}"
        log = worker.record(run_id).event_log
        if not log or not log.exists():
            return False
        return any(
            f'"hanging command: {command}"' in line and '"ACPToolCallEvent"' in line
            for line in log.read_text().splitlines()
        )

    wait_for(hanging, budget, interval=1.0, what=f"run {run_id}'s command `{command}` to hang")
    [sandbox] = sandboxes_of(run_id)
    assert command in sandbox_processes(sandbox)
    return sandbox


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
