"""What callers of the agent worker send and get back."""

from __future__ import annotations

import enum
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class RepoTarget:
    """One Repo a run works on, with the branches it uses."""

    name: str
    integration_branch: str
    base_branch: str = "main"


@dataclass(frozen=True)
class RunInputs:
    """The run inputs: the spec and its Tasks, as text."""

    spec: str
    tasks: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class RunRequest:
    """Everything `start` needs. Names refer to the worker's settings."""

    product: str
    agent_profile: str
    repos: list[RepoTarget]
    skill: str
    inputs: RunInputs
    # The Repo whose Task the agent is working on, among `repos` (default: the first). Its working
    # copy is what its Environment runs from; any other Repo in `repos` runs from its Task branch.
    working_on: str | None = None

    @property
    def working_repo(self) -> str:
        return self.working_on or self.repos[0].name


class Outcome(str, enum.Enum):
    """The one final outcome of a run that ended on its own (ADR 0002)."""

    SUCCEEDED = "succeeded"
    INFRA_FAILURE = "infra-failure"
    AGENT_GAVE_UP = "agent-gave-up"
    QUOTA_EXHAUSTED = "quota-exhausted"
    NEEDS_SETUP = "needs-setup"
    # The Environment came up with every Repo at its Base branch but not with the run's branches (#51):
    # the Story broke it. A Dispatcher treats it like red Checks; `reason` carries the logs.
    ENVIRONMENT_BROKEN = "environment-broken"


class RunState(str, enum.Enum):
    RUNNING = "running"
    ENDED = "ended"  # ended on its own; carries an Outcome
    CANCELLED = "cancelled"  # stopped by `cancel`; not an Outcome


@dataclass(frozen=True)
class Started:
    """`start` accepted the run; it is now running in the background.

    `subscription` is the name of the Subscription leased for it (never its credential).
    """

    run_id: str
    subscription: str


@dataclass(frozen=True)
class NoCapacity:
    """`start` refused the run: every Subscription its Product may lease for
    the agent is at its cap. Not an outcome; the caller may queue the run."""

    product: str
    agent: str
    subscriptions: list[str]


@dataclass(frozen=True)
class NeedsSetup:
    """`start` refused the run before starting a sandbox: something a human must
    set up is missing (a Repo's `CONTEXT.md`, a selected Coding standards file,
    the Product's Subscription for the agent). Its outcome is `needs-setup`; `reason` names what to fix."""

    reason: str

    @property
    def outcome(self) -> "Outcome":
        return Outcome.NEEDS_SETUP


StartResult = Started | NoCapacity | NeedsSetup


@dataclass(frozen=True)
class RunStatus:
    run_id: str
    state: RunState
    outcome: Outcome | None = None
    reason: str | None = None
    # Local tickets the agent marked done, once the run has ended (see RunRecord).
    tickets_done: list[str] | None = None

    @property
    def is_running(self) -> bool:
        return self.state is RunState.RUNNING

    @property
    def is_cancelled(self) -> bool:
        return self.state is RunState.CANCELLED

    @property
    def is_final(self) -> bool:
        return self.state is not RunState.RUNNING


@dataclass
class RunRecord:
    """What is kept about a run outside its sandbox, beside its event log."""

    run_id: str
    product: str
    agent_profile: str
    subscription: str  # the leased Subscription's name, never its credential
    skill: str
    repos: list[dict]
    state: RunState
    started_at: str
    ended_at: str | None = None
    outcome: Outcome | None = None
    reason: str | None = None
    event_log: Path | None = None
    # Processes still alive in the sandbox after the agent's conversation was
    # closed, just before the sandbox was removed. Should always be empty.
    processes_left_after_close: list[str] | None = None
    # Central skills used: {"version", "upstream_commit", "upstream_repository"}.
    central_skills: dict | None = None
    # Skills staged at user level, and central skills not staged because a Skill override applied.
    skills_staged: list[str] | None = None
    skill_overrides_applied: list[str] | None = None
    # Every clash with a Repo's own skill and every override disagreement (also in run.log).
    skill_clashes: list[str] | None = None
    # Local tickets the agent marked done (`spec`, `01`, `02`, ... in task order), read back
    # from its tracker copy when the run ended (ADR 0009). None if they could not be read.
    tickets_done: list[str] | None = None
    # The always-on file staged at user level, and each Repo's resolved Coding standards
    # (where each file was read from).
    always_on_file: str | None = None
    coding_standards: dict[str, list[str]] | None = None
    # The Environment (#49): each service started from a Repo's Run recipe with when it passed its
    # readiness check ({"repo", "service", "address", "ready_at", "seconds_to_ready"}), and where
    # each service's log was saved outside the sandbox ({"<repo>/<service>": path}). None: the
    # run had no Environment.
    environment_services: list[dict] | None = None
    environment_logs: dict[str, str] | None = None
    # Several Repos (#50): every Repo in the Environment and what it ran from ({"repo", "source":
    # "working copy" | "Task branch" | "Base branch", "branch", "touched"}), dependencies first.
    environment_repos: list[dict] | None = None
    # Seeding (#53): each Repo's seed command that ran, dependencies first ({"repo", "service",
    # "command", "seeded_at", "seconds"}); a Repo with no seed is absent. None: nothing was seeded.
    environment_seeds: list[dict] | None = None
    # The Test secrets given to the run (#52): the names each Repo's recipe asked for,
    # {"<repo>": [names]}. Never a value. None: no recipe named any.
    test_secrets_given: dict[str, list[str]] | None = None
    # How the Environment was brought up (#51): "branches" (some Repo ran from the Story's work)
    # and/or "base" (every Repo at its Base branch), in order. ["branches", "base"] is the one retry.
    # None: the run had no Environment.
    environment_bring_up_attempts: list[str] | None = None

    def status(self) -> RunStatus:
        return RunStatus(self.run_id, self.state, self.outcome, self.reason, self.tickets_done)

    def to_json(self) -> str:
        d = asdict(self)
        d["state"] = self.state.value
        d["outcome"] = self.outcome.value if self.outcome else None
        d["event_log"] = str(self.event_log) if self.event_log else None
        return json.dumps(d, indent=2)

    @classmethod
    def from_json(cls, text: str) -> "RunRecord":
        d = json.loads(text)
        d["state"] = RunState(d["state"])
        d["outcome"] = Outcome(d["outcome"]) if d.get("outcome") else None
        d["event_log"] = Path(d["event_log"]) if d.get("event_log") else None
        return cls(**d)
