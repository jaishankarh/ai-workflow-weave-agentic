"""The agent worker: start, status and cancel agent runs in fresh sandboxes."""

from __future__ import annotations

import json
import secrets
import shlex
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, TypeVar

from openhands.sdk import Conversation
from openhands.sdk.agent import ACPAgent
from openhands.sdk.conversation.response_utils import get_agent_final_response

from .config import ProductConfig, WorkerSettings
from . import git
from .git import GitError
from .model import NeedsSetup, NoCapacity, Outcome, RunRecord, RunRequest, RunState, RunStatus, Started, StartResult
from .outcomes import (
    DONE_MARK, GAVE_UP_MARK, classify_error, classify_final_reply, last_error_detail, with_agent_words,
)
from .environment import (
    FROM_BASE_BRANCH, FROM_TASK_BRANCH, RECIPE_PATH, Environment, EnvironmentBringUpError, LogsNotSaved, Placement,
    Recipe, RecipeError, parse_recipe, resolve_environment,
)
from .sandbox import WORKDIR, Sandbox, SandboxError, require_runtime, stage_skills, stage_user_files
from . import standards
from .secret_store import SecretsError, redact
from .subscriptions import Lease
from . import local_tickets
from .push_gateway import GATEWAY_HOST, PushGateway, RunRemotes, without_code_host_tokens
from .staging import StagingError, StagingPlan, central_skills_version, read_repo_skills
from .staging import plan as plan_skills
from workflow_weave.central_skills import CentralSkills

T = TypeVar("T")

TERMINAL = {"finished", "error", "stuck"}
# Under a run's folder, beside its event log: <repo>/<service>.log for each Environment service.
ENVIRONMENT_LOGS_DIR = "environment"
# How often a running run checks that its sandbox is still alive.
SANDBOX_CHECK_INTERVAL = 5.0


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
        # Each Repo's Test secrets (name -> value) this run was given, kept only to start its
        # services and to scrub values out of anything written down.
        self.test_secrets: dict[str, dict[str, str]] = {}


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
        if request.working_on is not None and request.working_on not in {r.name for r in request.repos}:
            raise ValueError(f"working_on {request.working_on!r} is not one of the run's Repos")

        store = self.settings.subscriptions
        agent = profile.agent_provider
        if problem := self._needs_setup(request, product, agent):
            return NeedsSetup(problem)
        lease = store.lease(request.product, agent)
        if lease is None:
            return NoCapacity(request.product, agent, store.associated(request.product, agent))
        try:
            return self._launch(request, lease)
        except BaseException:
            store.release(lease)
            raise

    def _needs_setup(self, request: RunRequest, product: ProductConfig, agent: str) -> str | None:
        """The pre-checks: what a human must set up before this run can start, or None.

        Checked in order, from the Sandbox host alone (no sandbox, no lease):
        the Product has a Subscription for the agent; every Repo's configured
        `push_remote` is in its clone; every Repo has a `CONTEXT.md` on its Base
        branch; every selected Coding standards file exists (the Product's file
        in the Central skills, each Repo's named rules files on its Base branch).
        The Base branch is the Code host's, fetched first when the clone has a
        Code host remote.
        """
        if not self.settings.subscriptions.associated(request.product, agent):
            return (
                f"Product {request.product!r} has no Subscription associated for agent {agent!r}: "
                f"associate one in the Subscription store"
            )
        try:
            code_hosts = _code_host_remotes(request, product)
        except GitError as e:
            return f"{e}: add that remote to the clone, or fix the Repo's push_remote"
        # A failed fetch is not refused here: the run itself reports it (as an infra-failure).
        refs = _base_refs(request, product, code_hosts, strict=False)
        missing = []
        for target in request.repos:
            source = product.repos[target.name].source
            if _missing_on_branch(source, refs[target.name], "CONTEXT.md"):
                missing.append(target.name)
        if missing:
            names = ", ".join(repr(m) for m in missing)
            branches = ", ".join(sorted({t.base_branch for t in request.repos if t.name in missing}))
            return (
                f"Repo {names} has no CONTEXT.md on its Base branch ({branches}): "
                f"onboard it (e.g. setup-matt-pocock-skills) before running agents on it"
            )
        absent = standards.missing(
            product, [(t.name, t.base_branch, refs[t.name]) for t in request.repos], self._central_location()
        )
        if absent:
            return "Coding standards missing: " + "; ".join(absent)
        problem, _ = self._test_secrets(request, product, refs)
        return problem

    def _test_secrets(
        self, request: RunRequest, product: ProductConfig, refs: dict[str, str]
    ) -> tuple[str | None, dict[str, dict[str, str]]]:
        """The Test secrets this run's Repos need: (what a human must fix or None, Repo -> name -> value).

        Each Repo's Run recipe on its Base branch names its secrets; a run is given those and only
        those, from its own Product's file. Nothing here starts a sandbox. The Product's file is
        read only when some recipe names a secret.
        """
        named = {
            t.name: names
            for t in request.repos
            if (names := _recipe_secret_names(t.name, product.repos[t.name].source, refs[t.name]))
        }
        if not named:
            return None, {}
        store = self.settings.test_secrets
        if store is None:
            repo, names = next(iter(named.items()))
            return (
                f"Repo {repo!r} names Test secret {names[0]!r} but no Test secrets are configured "
                f"(`test_secrets.location` in weave.yaml): add {request.product!r}'s Test secrets file",
                {},
            )
        try:
            have = store.for_product(request.product)
        except SecretsError as e:
            repo, names = next(iter(named.items()))
            return f"Repo {repo!r} names Test secret {names[0]!r}: {e}", {}
        lacking = [f"Repo {repo!r} names Test secret {n!r}" for repo, names in named.items() for n in have.missing(names)]
        if lacking:
            return (
                "; ".join(lacking) + f" that Product {request.product!r} does not have: add it to {have.path}",
                {},
            )
        return None, {repo: have.select(names) for repo, names in named.items()}

    def _central_location(self) -> Path | None:
        loc = self.settings.central_skills_location
        return Path(loc) if loc is not None else None

    def _launch(self, request: RunRequest, lease: Lease) -> Started:
        run_id = _new_run_id()
        run_dir = self._run_dir(run_id)
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
        path = self._run_dir(run_id) / "record.json"
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
        run_dir = self._run_dir(rec.run_id)
        sandbox: Sandbox | None = None
        conversation = None
        remotes: RunRemotes | None = None
        environments: list[Environment] = []
        leftovers: list[str] | None = None
        final: tuple[RunState, Outcome | None, str | None] = (
            RunState.ENDED, Outcome.INFRA_FAILURE, "the run stopped before it could report how it ended",
        )
        try:
            code_hosts = _code_host_remotes(run.request, product)
            refs = _base_refs(run.request, product, code_hosts, strict=True)
            # Only a run with a touched Repo that has a Run recipe gets an Environment, and so a
            # sandbox on sysbox; every other run stays on the default runtime, unchanged.
            with_recipe = [t.name for t in run.request.repos if _has_recipe(product.repos[t.name].source, refs[t.name])]
            # The Test secrets the recipes name, from the Product's own file (the same check `start`
            # made; repeated because the file may have changed since). Before any sandbox exists.
            problem, run.test_secrets = self._test_secrets(run.request, product, refs)
            if problem:
                raise EnvironmentBringUpError(problem)
            if run.test_secrets:
                rec.test_secrets_given = {repo: sorted(given) for repo, given in run.test_secrets.items()}
                self._save(rec)
                for repo, given in rec.test_secrets_given.items():
                    self._note(rec, f"Test secrets given to Repo {repo!r}'s services: {', '.join(given)} "
                                    f"(names only; values are never recorded)")
            runtime = None
            if with_recipe:
                runtime = self.settings.sandbox_runtime
                if not runtime:
                    raise SandboxError(
                        f"Repo {with_recipe[0]!r} has a Run recipe ({RECIPE_PATH}) but this worker has no "
                        f"sandbox_runtime set, so it cannot give the run an Environment"
                    )
                # Before anything is prepared or started: a host without the runtime is an infra-failure.
                require_runtime(runtime)
            for t in run.request.repos:
                source = product.repos[t.name].source
                if remote := code_hosts[t.name]:
                    self._note(rec, f"Repo {t.name!r}: Base branch {t.base_branch} fetched from the Code host "
                                    f"(remote {remote!r} of {source}); pushes continue to it")
                else:
                    self._note(rec, f"Repo {t.name!r}: its clone at {source} has no Code host remote; "
                                    f"Base branch {t.base_branch} read as it stands there, and pushes stop there")
            staging = self._plan_skills(run, product, refs)
            resolved = standards.resolve(
                product, [(t.name, t.base_branch, refs[t.name]) for t in run.request.repos], self._central_location()
            )
            originals = local_tickets.write_originals(run.request.inputs, run_dir / "tickets")
            remotes = self.push_gateway.open_run(
                run_dir / "remotes",
                {
                    t.name: (product.repos[t.name].source, t.integration_branch, code_hosts[t.name])
                    for t in run.request.repos
                },
            )
            env, dropped = without_code_host_tokens({**run.lease.env, "ACP_PROMPT_MAX_RETRIES": "0"})
            if dropped:
                self._note(rec, f"left out of the sandbox environment (Tracker / Code host tokens): {dropped}")
            if removed := sorted(k for k in env if k in profile.sandbox_env_removed):
                env = {k: v for k, v in env.items() if k not in removed}
                self._note(rec, f"left out of the sandbox environment (Agent profile {profile.name}): {removed}")
            sandbox = Sandbox(
                image=profile.image,
                run_id=rec.run_id,
                product=rec.product,
                # The credential goes in as sandbox environment only (ADR 0010, #13);
                # never a Tracker or Code host token (ADR 0009).
                env=env,
                nofile_limit=self.settings.sandbox_nofile_limit,
                start_timeout=self.settings.sandbox_start_timeout,
                # Read-only, so the agent cannot change the originals (ADR 0009).
                mounts=[(str(originals.resolve()), local_tickets.ORIGINALS_DIR)],
                extra_hosts=[f"{GATEWAY_HOST}:host-gateway"],
                runtime=runtime,
            )
            sandbox.sh(local_tickets.MAKE_TRACKER, cwd="/")
            for target in run.request.repos:
                sandbox.put_repo(
                    product.repos[target.name].source, target.name, target.base_branch, target.integration_branch,
                    remotes.url(target.name), base_ref=refs[target.name],
                )
            stage_skills(sandbox, staging)
            # The always-on file and the resolved Coding standards, at user level (ADR 0002).
            user_dir = stage_user_files(sandbox, lambda d: resolved.tarball(d, WORKDIR))
            rec.always_on_file = f"{user_dir}/{standards.ALWAYS_ON_FILE}"
            rec.coding_standards = {r.repo: [f.origin for f in r.files] for r in resolved.repos}
            if run.cancel_requested.is_set():
                raise _Cancelled

            # The Environment is up and ready before any agent starts (and so before any
            # Subscription use); a failure here ends the run with its reason.
            if with_recipe:
                self._bring_up_environment(run, product, sandbox, refs, environments)

            agent = ACPAgent(acp_command=profile.acp_command, acp_session_mode=profile.acp_session_mode)
            log = _EventLog(rec.event_log)
            conversation = Conversation(
                agent=agent, workspace=sandbox.workspace, visualizer=None, callbacks=[log.append]
            )
            conversation.send_message(_prompt(run.request))
            conversation.run(blocking=False)
            status = self._wait(conversation, run, sandbox)
            if status is None:
                raise _Cancelled
            log.rewrite(conversation.state.events)
            final = (RunState.ENDED, *_classify(status, conversation, profile.error_kinds))
        except _Cancelled:
            final = (RunState.CANCELLED, None, "cancelled")
        except EnvironmentBringUpError as e:
            final = (RunState.ENDED, e.outcome, e.reason[:2000])
        except SandboxError as e:
            final = (RunState.ENDED, Outcome.INFRA_FAILURE, f"sandbox failure: {e}"[:2000])
        except Exception as e:  # anything else that broke the run's infrastructure
            final = (RunState.ENDED, Outcome.INFRA_FAILURE, f"{type(e).__name__}: {e}"[:2000])
        finally:
            # Every end path releases the lease, saves the record and finishes the run,
            # however teardown goes (a teardown failure is noted in run.log).
            try:
                leftovers = self._stop(conversation, sandbox, rec, environments)
                if remotes is not None:
                    gateway_remotes = remotes
                    self._best_effort(rec, "revoking the run's push token",
                                      lambda: self.push_gateway.close_run(gateway_remotes))
            finally:
                try:
                    self.settings.subscriptions.release(run.lease)  # however the run ended
                finally:
                    self._finish(run, final, leftovers)

    def _finish(
        self, run: _Run, final: tuple[RunState, Outcome | None, str | None], leftovers: list[str] | None
    ) -> None:
        rec = run.record
        try:
            rec.processes_left_after_close = leftovers
            if leftovers:
                self._note(rec, f"processes still running after the agent was closed: {leftovers}")
            rec.state, rec.outcome, rec.reason = final
            if rec.reason:
                rec.reason = self._scrub(rec, rec.reason)
            rec.ended_at = _now()
            self._save(rec)
        finally:
            run.done.set()

    def _wait(self, conversation: Any, run: _Run, sandbox: Sandbox) -> str | None:
        """Wait for the conversation to end; None if the run was cancelled first.

        Raises SandboxError if the sandbox dies first.
        """
        next_check = time.monotonic() + SANDBOX_CHECK_INTERVAL
        while True:
            if run.cancel_requested.wait(0.5):
                return None
            try:
                status = str(conversation.state.execution_status.value).lower()
            except Exception:
                status = None  # the agent-server did not answer; the sandbox check decides
                next_check = 0.0
            if status in TERMINAL:
                return status
            if time.monotonic() >= next_check:
                if how := sandbox.stopped():
                    raise SandboxError(f"{how} mid-run: {sandbox.last_logs(500)}")
                next_check = time.monotonic() + SANDBOX_CHECK_INTERVAL

    def _bring_up_environment(
        self, run: _Run, product: ProductConfig, sandbox: Sandbox, refs: dict[str, str],
        environments: list[Environment],
    ) -> None:
        """Bring up every Repo of the Environment, dependencies first (#50): the touched Repos that
        have a Run recipe and, from their `depends_on`, the Repos the run does not touch. Each runs
        from the branch its place in the Story calls for; the run record says which."""
        rec, request = run.record, run.request
        touched = {t.name: t for t in request.repos}
        with_base_recipe = {t.name for t in request.repos if _has_recipe(product.repos[t.name].source, refs[t.name])}

        def base_branch_of(name: str) -> str:
            return touched[name].base_branch if name in touched else product.repos[name].base_branch

        def load(placement: Placement) -> Recipe | None:
            name = placement.repo
            if name not in product.repos:
                raise RecipeError(f"Repo {name!r} is named in a Run recipe's depends_on but is not part of "
                                  f"Product {request.product!r}")
            if placement.source == FROM_BASE_BRANCH:
                sandbox.put_checkout(
                    product.repos[name].source, name, self._dependency_base_ref(product, name, placement.branch),
                    placement.path,
                )
            elif placement.source == FROM_TASK_BRANCH:
                if not sandbox.put_task_branch_checkout(
                    name, placement.branch, touched[name].base_branch, placement.path
                ):
                    self._note(rec, f"Repo {name!r}: no Task branch {placement.branch!r} yet; its Environment "
                                    f"runs from Base branch {touched[name].base_branch}")
            code, text = sandbox.run(f"cat {shlex.quote(RECIPE_PATH)}", cwd=placement.path)
            if code != 0:
                if name in with_base_recipe:
                    raise RecipeError(f"Repo {name!r}: its Run recipe {RECIPE_PATH} is not in the {placement.source} "
                                      f"(it was on the Base branch): {text.strip()[-300:]}")
                return None
            return parse_recipe(name, text)

        try:
            placed = resolve_environment(touched, request.working_repo, base_branch_of, load)
        except RecipeError as e:
            raise EnvironmentBringUpError(str(e)) from e
        rec.environment_repos = [p.as_record() for p, _ in placed]
        self._save(rec)
        for placement, recipe in placed:
            given = self._secrets_for_recipe(run, placement.repo, recipe)
            environment = Environment(
                sandbox, placement.repo, now=_now, working_copy=placement.path, secrets=given
            )
            environments.append(environment)
            self._bring_up(rec, environment, recipe, environments)
            if run.cancel_requested.is_set():
                raise _Cancelled

    def _secrets_for_recipe(self, run: _Run, repo: str, recipe: Recipe) -> dict[str, str]:
        """The Test secrets `recipe` (as it runs: a working copy, Task branch or a dependency's Base
        branch) names, from the Product's own file. Names the start-time check already resolved are
        reused; others (a dependency Repo's, or one added on the branch) are read now and added to
        what this run scrubs from everything it records."""
        have = run.test_secrets.setdefault(repo, {})
        wanted = [n for n in recipe.secrets if n not in have]
        if wanted:
            store = self.settings.test_secrets
            if store is None:
                raise EnvironmentBringUpError(
                    f"Repo {repo!r} names Test secret {wanted[0]!r} but no Test secrets are configured "
                    f"(`test_secrets.location` in weave.yaml)"
                )
            try:
                product_secrets = store.for_product(run.request.product)
            except SecretsError as e:
                raise EnvironmentBringUpError(f"Repo {repo!r} names Test secret {wanted[0]!r}: {e}") from e
            if lacking := product_secrets.missing(wanted):
                raise EnvironmentBringUpError(
                    f"Repo {repo!r} names Test secret {lacking[0]!r} that Product {run.request.product!r} "
                    f"does not have: add it to {product_secrets.path}"
                )
            have.update(product_secrets.select(wanted))
            self._note(run.record, f"Test secrets given to Repo {repo!r}'s services: {', '.join(wanted)} (names only; values are never recorded)")
        picked = {n: have[n] for n in recipe.secrets}
        if picked:
            rec = run.record
            rec.test_secrets_given = {**(rec.test_secrets_given or {}), repo: sorted(picked)}
            self._save(rec)
        return picked

    def _dependency_base_ref(self, product: ProductConfig, name: str, base_branch: str) -> str:
        """Where to read the Base branch of a Repo the run does not touch: the Code host's, just
        fetched into the clone, or the clone's own branch when it has no Code host remote."""
        repo = product.repos[name]
        try:
            remote = git.code_host_remote(repo.source, repo.push_remote)
            return git.fetch_base(repo.source, remote, base_branch) if remote else f"refs/heads/{base_branch}"
        except GitError as e:
            raise GitError(f"Repo {name!r}: {e}") from e

    def _bring_up(
        self, rec: RunRecord, environment: Environment, recipe: Recipe, environments: list[Environment]
    ) -> None:
        """Bring one Repo's recipe up. The run record lists every service that became ready, even
        when a later one did not."""
        try:
            environment.bring_up(recipe)
        finally:
            # Whatever became ready is on the record, even when a later service did not.
            rec.environment_services = [r.as_record() for e in environments for r in e.ready]
            self._save(rec)

    def _stop(
        self, conversation: Any, sandbox: Sandbox | None, rec: RunRecord, environments: list[Environment] = ()
    ) -> list[str] | None:
        """Close the conversation (never just interrupt it, #13), then remove the sandbox.

        Best effort: each step that fails is noted in run.log, and the next one still runs. The
        Environment's service logs are saved outside the sandbox first, while they still exist.
        """
        leftovers: list[str] | None = None
        if conversation is not None:
            self._best_effort(rec, "saving the event log",
                              lambda: _EventLog(rec.event_log).rewrite(conversation.state.events))
            # Deletes the conversation on the agent-server.
            self._best_effort(rec, "closing the agent's conversation", conversation.close)
        if sandbox is not None:
            if environments:
                rec.environment_logs = self._save_environment_logs(rec, environments)
            if conversation is not None:
                leftovers = self._best_effort(rec, "listing processes left after close",
                                              lambda: _settle(sandbox.processes))
            rec.tickets_done = self._best_effort(
                rec, "reading back the local tickets marked done",
                lambda: local_tickets.done_tickets(sandbox.sh(local_tickets.READ_STATUSES, cwd="/")),
            )
            self._best_effort(rec, "removing the sandbox", sandbox.destroy)
        return leftovers

    def _save_environment_logs(self, rec: RunRecord, environments: list[Environment]) -> dict[str, str]:
        """Save every Environment service's log under the run's folder, beside the event log."""
        destination = self._run_dir(rec.run_id) / ENVIRONMENT_LOGS_DIR
        saved: dict[str, str] = {}
        for environment in environments:
            try:
                saved.update(environment.save_logs(destination))
            except LogsNotSaved as e:
                saved.update(e.saved)  # the ones that could be read are still on the record
                self._note(rec, f"teardown: saving the service logs failed: {e}")
            except Exception as e:
                self._note(rec, f"teardown: saving the service logs of Repo {environment.repo!r} failed: "
                                f"{type(e).__name__}: {e}")
        return saved

    def _best_effort(self, rec: RunRecord, what: str, step: Callable[[], T]) -> T | None:
        """Run one teardown step; if it fails, note it with its error and carry on."""
        try:
            return step()
        except Exception as e:
            self._note(rec, f"teardown: {what} failed: {type(e).__name__}: {e}")
            return None

    def _plan_skills(self, run: _Run, product: ProductConfig, refs: dict[str, str]) -> StagingPlan:
        """Decide the run's skills, and record the Central skills version and every clash."""
        if self.settings.central_skills_location is None:
            raise StagingError("no Central skills location is configured")
        central = CentralSkills(Path(self.settings.central_skills_location))
        repos = [
            read_repo_skills(
                t.name, product.repos[t.name].source, t.base_branch, product.repos[t.name].skill_overrides,
                ref=refs[t.name],
            )
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

    def _run_dir(self, run_id: str) -> Path:
        """Where a run's record, event log, run.log, tickets and gateway mirrors live."""
        return self.settings.runs_dir / run_id

    def _save(self, rec: RunRecord) -> None:
        path = self._run_dir(rec.run_id) / "record.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(rec.to_json())
        tmp.replace(path)

    def _note(self, rec: RunRecord, line: str) -> None:
        with open(self._run_dir(rec.run_id) / "run.log", "a") as f:
            f.write(f"{_now()} {self._scrub(rec, line)}\n")

    def _scrub(self, rec: RunRecord, text: str) -> str:
        """`text` without any Test secret value given to this run: the last guard before it is written."""
        with self._lock:
            run = self._runs.get(rec.run_id)
        given = run.test_secrets if run is not None else {}
        return redact(text, [v for values in given.values() for v in values.values()])


def _missing_on_branch(source: str, ref: str, path: str) -> bool:
    """True when a readable Repo's branch (read at `ref`) has no file at `path`.

    A Repo or branch that cannot be read at all is left for the run itself to
    report (as an infra-failure), not refused here.
    """
    if not git.has_commit(source, ref):
        return False
    return subprocess.run(["git", "-C", source, "cat-file", "-e", f"{ref}:{path}"], capture_output=True).returncode != 0


def _recipe_secret_names(repo: str, source: str, ref: str) -> tuple[str, ...]:
    """The Test secrets a Repo's Run recipe names on its Base branch (read at `ref`), by name.

    A Repo with no recipe, or a recipe that cannot be read or parsed, names none here: the run
    itself reports an unusable recipe.
    """
    if not _has_recipe(source, ref):
        return ()
    shown = subprocess.run(["git", "-C", source, "show", f"{ref}:{RECIPE_PATH}"], capture_output=True, text=True)
    if shown.returncode != 0:
        return ()
    try:
        return parse_recipe(repo, shown.stdout).secrets
    except RecipeError:
        return ()


def _has_recipe(source: str, ref: str) -> bool:
    """True when a readable Repo has a Run recipe on its Base branch (read at `ref`).

    A Repo or branch that cannot be read is not taken to have one: the run itself reports it.
    """
    return git.has_commit(source, ref) and not _missing_on_branch(source, ref, RECIPE_PATH)


def _code_host_remotes(request: RunRequest, product: ProductConfig) -> dict[str, str | None]:
    """Each Repo's Code host remote in its clone, or None; GitError if a configured one is missing."""
    out: dict[str, str | None] = {}
    for t in request.repos:
        repo = product.repos[t.name]
        try:
            out[t.name] = git.code_host_remote(repo.source, repo.push_remote)
        except GitError as e:
            raise GitError(f"Repo {t.name!r}: {e}") from e
    return out


def _base_refs(
    request: RunRequest, product: ProductConfig, code_hosts: dict[str, str | None], *, strict: bool
) -> dict[str, str]:
    """Where to read each Repo's Base branch: the Code host's, just fetched into the clone,
    or the clone's own branch when it has no Code host remote.

    With `strict`, a failed fetch raises GitError; otherwise the Base branch is read as
    last fetched.
    """
    refs: dict[str, str] = {}
    for t in request.repos:
        remote = code_hosts[t.name]
        if remote is None:
            refs[t.name] = t.base_branch
            continue
        try:
            refs[t.name] = git.fetch_base(product.repos[t.name].source, remote, t.base_branch)
        except GitError as e:
            if strict:
                raise GitError(f"Repo {t.name!r}: {e}") from e
            refs[t.name] = f"refs/remotes/{remote}/{t.base_branch}"
    return refs


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


def _classify(status: str, conversation: Any, error_kinds: Any) -> tuple[Outcome, str | None]:
    """Turn how the conversation ended into one outcome, from the agent's own error kind."""
    events = conversation.state.events
    if status == "finished":
        return classify_final_reply(get_agent_final_response(events) or "")
    detail = last_error_detail(events)
    if detail is not None:
        return classify_error(with_agent_words(detail, events), error_kinds)
    if status == "stuck":
        # OpenHands' stuck detector stopped an agent going round in circles: it did not
        # finish its skill, and nothing in the infrastructure failed.
        return Outcome.AGENT_GAVE_UP, "agent gave up: its conversation ended stuck (no progress), with no error"
    return Outcome.INFRA_FAILURE, f"the agent failed: its conversation ended as {status} with no error reported"
