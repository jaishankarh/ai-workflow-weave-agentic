"""The agent worker: start, status and cancel agent runs in fresh sandboxes."""

from __future__ import annotations

import json
import secrets
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from openhands.sdk import Conversation
from openhands.sdk.agent import ACPAgent
from openhands.sdk.conversation.response_utils import get_agent_final_response

from .config import WorkerSettings
from .model import NoCapacity, Outcome, RunRecord, RunRequest, RunState, RunStatus, Started, StartResult
from .sandbox import Sandbox, stage_skills
from .subscriptions import Lease
from . import local_tickets
from .push_gateway import GATEWAY_HOST, PushGateway, RunRemotes
from .staging import StagingError, StagingPlan, central_skills_version, read_repo_skills
from .staging import plan as plan_skills
from workflow_weave.central_skills import CentralSkills

TERMINAL = {"finished", "error", "stuck"}
# The agent is asked to end its last reply with one of these lines.
DONE_MARK = "RUN-OUTCOME: done"
GAVE_UP_MARK = "RUN-OUTCOME: gave-up"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _new_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + secrets.token_hex(4)


class UnknownRun(KeyError):
    pass


class _Run:
    def __init__(self, record: RunRecord, request: RunRequest, lease: Lease) -> None:
        self.record = record
        self.request = request
        self.lease = lease
        self.cancel_requested = threading.Event()
        self.done = threading.Event()
        self.thread: threading.Thread | None = None


class AgentWorker:
    """One start / status / cancel contract for agent runs.

    `start` returns at once; the run proceeds on a background thread in its
    own sandbox. The run record and event log are written under
    `settings.runs_dir/<run id>/` and outlive the sandbox.
    """

    def __init__(self, settings: WorkerSettings) -> None:
        self.settings = settings
        self._runs: dict[str, _Run] = {}
        self._lock = threading.Lock()
        settings.runs_dir.mkdir(parents=True, exist_ok=True)
        self._own_gateway = settings.push_gateway is None
        self.push_gateway = settings.push_gateway or PushGateway()

    # ------------------------------------------------------------------ contract

    def start(self, request: RunRequest) -> StartResult:
        product = self.settings.products.get(request.product)
        if product is None:
            raise ValueError(f"unknown Product {request.product!r}")
        profile = self.settings.agent_profiles.get(request.agent_profile)
        if profile is None:
            raise ValueError(f"unknown Agent profile {request.agent_profile!r}")
        for repo in request.repos:
            if repo.name not in product.repos:
                raise ValueError(f"Repo {repo.name!r} is not part of Product {request.product!r}")

        store = self.settings.subscriptions
        agent = profile.agent_provider
        lease = store.lease(request.product, agent)
        if lease is None:
            return NoCapacity(request.product, agent, store.associated(request.product, agent))
        try:
            return self._launch(request, lease)
        except BaseException:
            store.release(lease)
            raise

    def _launch(self, request: RunRequest, lease: Lease) -> Started:
        run_id = _new_run_id()
        run_dir = self.settings.runs_dir / run_id
        run_dir.mkdir(parents=True)
        record = RunRecord(
            run_id=run_id,
            product=request.product,
            agent_profile=request.agent_profile,
            subscription=lease.name,
            skill=request.skill,
            repos=[vars(r) for r in request.repos],
            state=RunState.RUNNING,
            started_at=_now(),
            event_log=run_dir / "events.jsonl",
        )
        run = _Run(record, request, lease)
        self._save(record)
        with self._lock:
            self._runs[run_id] = run
        run.thread = threading.Thread(target=self._execute, args=(run,), name=f"run-{run_id}", daemon=True)
        run.thread.start()
        return Started(run_id, lease.name)

    def status(self, run_id: str) -> RunStatus:
        with self._lock:
            run = self._runs.get(run_id)
        if run is not None:
            return run.record.status()
        return self.record(run_id).status()

    def record(self, run_id: str) -> RunRecord:
        """The saved run record (read from outside the sandbox)."""
        path = self.settings.runs_dir / run_id / "record.json"
        if not path.exists():
            raise UnknownRun(run_id)
        return RunRecord.from_json(path.read_text())

    def cancel(self, run_id: str, timeout: float = 120) -> RunStatus:
        """Stop a run: close its agent's conversation, then tear down its sandbox.

        Returns once the run has stopped. A run that already ended keeps its outcome.
        """
        with self._lock:
            run = self._runs.get(run_id)
        if run is None:
            return self.record(run_id).status()
        run.cancel_requested.set()
        run.done.wait(timeout)
        return run.record.status()

    def shutdown(self) -> None:
        """Cancel every run still going (e.g. when the host process stops)."""
        with self._lock:
            runs = list(self._runs.values())
        for run in runs:
            if not run.done.is_set():
                self.cancel(run.record.run_id)
        if self._own_gateway:
            self.push_gateway.shutdown()

    # ------------------------------------------------------------------ the run

    def _execute(self, run: _Run) -> None:
        rec = run.record
        profile = self.settings.agent_profiles[run.request.agent_profile]
        product = self.settings.products[run.request.product]
        sandbox: Sandbox | None = None
        conversation = None
        remotes: RunRemotes | None = None
        final: tuple[RunState, Outcome | None, str | None]
        try:
            staging = self._plan_skills(run, product)
            originals = local_tickets.write_originals(
                run.request.inputs, self.settings.runs_dir / rec.run_id / "tickets"
            )
            remotes = self.push_gateway.open_run(
                self.settings.runs_dir / rec.run_id / "remotes",
                {t.name: (product.repos[t.name].source, t.integration_branch) for t in run.request.repos},
            )
            sandbox = Sandbox(
                image=profile.image,
                run_id=rec.run_id,
                product=rec.product,
                # The credential goes in as sandbox environment only (ADR 0010, #13).
                env={"ACP_PROMPT_MAX_RETRIES": "0", **run.lease.env},
                nofile_limit=self.settings.sandbox_nofile_limit,
                start_timeout=self.settings.sandbox_start_timeout,
                # Read-only, so the agent cannot change the originals (ADR 0009).
                mounts=[(str(originals.resolve()), local_tickets.ORIGINALS_DIR)],
                extra_hosts=[f"{GATEWAY_HOST}:host-gateway"],
            )
            sandbox.sh(local_tickets.MAKE_TRACKER, cwd="/")
            for target in run.request.repos:
                sandbox.put_repo(
                    product.repos[target.name].source, target.name, target.base_branch, target.integration_branch,
                    remotes.url(target.name),
                )
            stage_skills(sandbox, staging)
            if run.cancel_requested.is_set():
                raise _Cancelled

            agent = ACPAgent(acp_command=profile.acp_command, acp_session_mode=profile.acp_session_mode)
            log = _EventLog(rec.event_log)
            conversation = Conversation(
                agent=agent, workspace=sandbox.workspace, visualizer=None, callbacks=[log.append]
            )
            conversation.send_message(_prompt(run.request))
            conversation.run(blocking=False)
            status = self._wait(conversation, run)
            if status is None:
                raise _Cancelled
            log.rewrite(conversation.state.events)
            final = (RunState.ENDED, *_classify(status, conversation))
        except _Cancelled:
            final = (RunState.CANCELLED, None, "cancelled")
        except Exception as e:  # anything that broke the run's infrastructure
            final = (RunState.ENDED, Outcome.INFRA_FAILURE, f"{type(e).__name__}: {e}"[:2000])
        finally:
            leftovers = self._stop(conversation, sandbox, rec)
            if remotes is not None:
                self.push_gateway.close_run(remotes)  # the run's push token stops working
            self.settings.subscriptions.release(run.lease)  # however the run ended
        rec.processes_left_after_close = leftovers
        if leftovers:
            self._note(rec, f"processes still running after the agent was closed: {leftovers}")
        state, outcome, reason = final
        rec.state, rec.outcome, rec.reason, rec.ended_at = state, outcome, reason, _now()
        self._save(rec)
        run.done.set()

    def _wait(self, conversation: Any, run: _Run) -> str | None:
        """Wait for the conversation to end; None if the run was cancelled first."""
        while True:
            if run.cancel_requested.wait(0.5):
                return None
            status = str(conversation.state.execution_status.value).lower()
            if status in TERMINAL:
                return status

    def _stop(self, conversation: Any, sandbox: Sandbox | None, rec: RunRecord) -> list[str] | None:
        """Close the conversation (never just interrupt it, #13), then remove the sandbox."""
        leftovers: list[str] | None = None
        if conversation is not None:
            try:
                _EventLog(rec.event_log).rewrite(conversation.state.events)
            except Exception:
                pass
            try:
                conversation.close()  # deletes the conversation on the agent-server
            except Exception:
                pass
        if sandbox is not None:
            try:
                if conversation is not None:
                    leftovers = _settle(sandbox.processes)
            except Exception:
                pass
            try:
                rec.tickets_done = local_tickets.done_tickets(sandbox.sh(local_tickets.READ_STATUSES, cwd="/"))
            except Exception:
                pass
            sandbox.destroy()
        return leftovers

    def _plan_skills(self, run: _Run, product: Any) -> StagingPlan:
        """Decide the run's skills, and record the Central skills version and every clash."""
        if self.settings.central_skills_location is None:
            raise StagingError("no Central skills location is configured")
        central = CentralSkills(Path(self.settings.central_skills_location))
        repos = [
            read_repo_skills(t.name, product.repos[t.name].source, t.base_branch, product.repos[t.name].skill_overrides)
            for t in run.request.repos
        ]
        plan = plan_skills(central, run.request.skill, repos)
        rec = run.record
        rec.central_skills = central_skills_version(central)
        rec.skills_staged = sorted(plan.staged)
        rec.skill_overrides_applied = plan.overridden
        rec.skill_clashes = plan.notes
        for line in plan.notes:
            self._note(rec, line)
        self._save(rec)
        return plan

    # ------------------------------------------------------------------ records

    def _save(self, rec: RunRecord) -> None:
        path = self.settings.runs_dir / rec.run_id / "record.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(rec.to_json())
        tmp.replace(path)

    def _note(self, rec: RunRecord, line: str) -> None:
        with open(self.settings.runs_dir / rec.run_id / "run.log", "a") as f:
            f.write(f"{_now()} {line}\n")


def _settle(probe: Any, timeout: float = 10.0) -> list[str]:
    """Processes left once the agent has had a moment to exit after close."""
    deadline = time.monotonic() + timeout
    left = probe()
    while left and time.monotonic() < deadline:
        time.sleep(0.5)
        left = probe()
    return left


class _Cancelled(Exception):
    pass


class _EventLog:
    """The conversation's events as JSON lines, written as they arrive."""

    def __init__(self, path: Path | None) -> None:
        assert path is not None
        self.path = path
        self._lock = threading.Lock()

    def append(self, event: Any) -> None:
        line = json.dumps(event.model_dump(mode="json"), default=str)
        with self._lock, open(self.path, "a") as f:
            f.write(line + "\n")

    def rewrite(self, events: Any) -> None:
        lines = [json.dumps(e.model_dump(mode="json"), default=str) for e in events]
        if not lines:
            return
        tmp = self.path.with_suffix(".tmp")
        with self._lock:
            tmp.write_text("\n".join(lines) + "\n")
            tmp.replace(self.path)


def _prompt(request: RunRequest) -> str:
    tasks = "\n".join(f"- {t}" for t in request.inputs.tasks)
    repos = ", ".join(f"{r.name} (branch {r.integration_branch})" for r in request.repos)
    return (
        f"Run the `{request.skill}` skill.\n\n"
        f"Repos: {repos}\n\n"
        f"{local_tickets.prompt_pointer()}\n\n"
        f"## Spec\n\n{request.inputs.spec}\n\n## Tasks\n\n{tasks}\n\n"
        f"When you have finished, end your last reply with the line `{DONE_MARK}`. "
        f"If you cannot complete the skill, end it with `{GAVE_UP_MARK}: <why>` instead.\n"
    )


def _classify(status: str, conversation: Any) -> tuple[Outcome, str | None]:
    """Turn how the conversation ended into one outcome. #38 refines this."""
    if status != "finished":
        return Outcome.INFRA_FAILURE, f"conversation ended as {status}"
    reply = get_agent_final_response(conversation.state.events) or ""
    lines = [line.strip() for line in reply.strip().splitlines() if line.strip()]
    last = lines[-1] if lines else ""
    if last == DONE_MARK:
        return Outcome.SUCCEEDED, None
    if last.startswith(GAVE_UP_MARK):
        why = last[len(GAVE_UP_MARK):].lstrip(": ").strip()
        return Outcome.AGENT_GAVE_UP, f"agent gave up: {why or 'no reason given'}"
    return Outcome.AGENT_GAVE_UP, "agent ended without reporting the skill complete"
