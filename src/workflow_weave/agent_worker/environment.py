"""The Environment: a Repo's Run recipe brought up inside the run's sandbox (ADR 0004).

A Repo's Run recipe is one committed compose file, `.weave/compose.yaml`, kept apart from any
developer `docker-compose.yml`. The workflow's own fields sit in one top-level `x-weave:` block,
which Compose ignores, so the file also runs with plain `docker compose -f .weave/compose.yaml up`:

    services:
      web:
        image: python:3.12-slim
        command: python -m http.server 8000
    x-weave:
      readiness:
        web:                       # one check per service, run inside that service's container
          command: python -c "import urllib.request as u; u.urlopen('http://localhost:8000')"
          timeout: 60              # seconds to become ready after `up` (default 60)
          interval: 1              # seconds between attempts (default 1)

`command` is a string (run with `sh -c`) or a list (run as is); exit status 0 means ready. A recipe
may have no services at all. Other `x-weave` fields (dependencies, seed, secrets, databases, MCP
servers) belong to later tickets of Spec 2 and are ignored here, except `secrets`:

    x-weave:
      secrets: [KORONA_API_KEY]   # Test secrets this Repo's services need, by name only

Each named secret reaches every service of the recipe as an environment variable of that name,
with the value from the Product's Test secrets; nothing else from the Test secrets does, and none
reaches the agent's sandbox environment. A recipe never holds a value.
"""

from __future__ import annotations

import shlex
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

import yaml

from .model import Outcome
from .secret_store import SECRET_NAME, redact

# Fixed, and never the developer's `docker-compose.yml`.
RECIPE_PATH = ".weave/compose.yaml"
WEAVE_KEY = "x-weave"
# The one network every Repo's services join; each service is `<service>.<repo>` on it.
NETWORK = "weave-env"
# Where the generated override lives inside the sandbox (never in a working copy).
ENV_DIR = "/weave/env"

DEFAULT_READY_TIMEOUT = 60.0
DEFAULT_READY_INTERVAL = 1.0
# How long `docker compose up` may take (it pulls and builds images).
UP_TIMEOUT = 1800.0
LOG_TAIL_LINES = 20


class RecipeError(ValueError):
    """The Repo's Run recipe cannot be used; the message says which Repo and what to fix."""


class EnvironmentBringUpError(RuntimeError):
    """The Environment could not be brought up. Raised before any agent starts."""

    def __init__(self, reason: str, outcome: Outcome = Outcome.NEEDS_SETUP) -> None:
        super().__init__(reason)
        self.reason = reason
        self.outcome = outcome


class LogsNotSaved(RuntimeError):
    """Some service logs could not be read; `saved` has the ones that were written."""

    def __init__(self, message: str, saved: dict[str, str]) -> None:
        super().__init__(message)
        self.saved = saved


@dataclass(frozen=True)
class ReadinessCheck:
    command: tuple[str, ...]
    timeout: float = DEFAULT_READY_TIMEOUT
    interval: float = DEFAULT_READY_INTERVAL


@dataclass(frozen=True)
class Recipe:
    repo: str
    services: tuple[str, ...]
    readiness: dict[str, ReadinessCheck]
    # Names of the Test secrets the recipe needs (never values).
    secrets: tuple[str, ...] = ()


def parse_recipe(repo: str, text: str) -> Recipe:
    """Read and check a Repo's Run recipe. Raises RecipeError naming the Repo and what to fix."""

    def bad(problem: str) -> RecipeError:
        return RecipeError(f"Repo {repo!r}: the Run recipe {RECIPE_PATH} {problem}")

    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise bad(f"is not valid YAML ({e})") from e
    if not isinstance(doc, dict):
        raise bad("must be a mapping (a compose file)")
    services = doc.get("services") or {}
    if not isinstance(services, dict):
        raise bad("has `services` that is not a mapping")
    names = tuple(str(s) for s in services)
    block = doc.get(WEAVE_KEY)
    if block is None:
        raise bad(f"has no `{WEAVE_KEY}:` block (add one with a readiness check per service)")
    if not isinstance(block, dict):
        raise bad(f"has a `{WEAVE_KEY}:` block that is not a mapping")
    checks = block.get("readiness") or {}
    if not isinstance(checks, dict):
        raise bad(f"has `{WEAVE_KEY}.readiness` that is not a mapping of service to check")
    for service in checks:
        if str(service) not in names:
            raise bad(f"has a readiness check for unknown service {str(service)!r}")
    readiness: dict[str, ReadinessCheck] = {}
    for service in names:
        if service not in checks:
            raise bad(f"has no readiness check for service {service!r} (`{WEAVE_KEY}.readiness.{service}`)")
        readiness[service] = _parse_check(checks[service], service, bad)
    return Recipe(repo=repo, services=names, readiness=readiness, secrets=_parse_secret_names(block, bad))


def _parse_secret_names(block: dict, bad: Callable[[str], RecipeError]) -> tuple[str, ...]:
    raw = block.get("secrets")
    if raw is None:
        return ()
    where = f"`{WEAVE_KEY}.secrets`"
    # Names only: a mapping (NAME: value) would put a value in a committed file.
    if not isinstance(raw, list) or not all(isinstance(n, str) for n in raw):
        raise bad(f"has {where} that is not a list of secret names (names only, never values)")
    for name in raw:
        if not SECRET_NAME.fullmatch(name):
            raise bad(f"has {where} naming {name!r}, which is not a valid environment variable name")
    if len(set(raw)) != len(raw):
        dup = next(n for n in raw if raw.count(n) > 1)
        raise bad(f"names secret {dup!r} twice in {where}")
    return tuple(raw)


def _parse_check(raw: Any, service: str, bad: Callable[[str], RecipeError]) -> ReadinessCheck:
    where = f"readiness check for service {service!r}"
    if not isinstance(raw, dict):
        raise bad(f"has a {where} that is not a mapping with a `command`")
    command = raw.get("command")
    if isinstance(command, str) and command.strip():
        argv: tuple[str, ...] = ("sh", "-c", command)
    elif isinstance(command, list) and command and all(isinstance(c, str) for c in command):
        argv = tuple(command)
    else:
        raise bad(f"has a {where} without a `command` (a non-empty string, or a list of strings)")
    values: dict[str, float] = {}
    for key, default in (("timeout", DEFAULT_READY_TIMEOUT), ("interval", DEFAULT_READY_INTERVAL)):
        value = raw.get(key, default)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
            raise bad(f"has a {where} whose `{key}` is not a positive number of seconds")
        values[key] = float(value)
    return ReadinessCheck(argv, values["timeout"], values["interval"])


def compose_override(repo: str, recipe: Recipe) -> str:
    """The generated compose override: every service joins the Environment network as
    `<service>.<repo>`, and keeps the recipe's own default network so it behaves as it does
    under standard tooling."""
    doc = {
        "services": {
            s: {
                "networks": {NETWORK: {"aliases": [f"{s}.{repo}"]}, "default": {}},
                # Names only: Compose takes each value from the environment of the `up` command.
                **({"environment": list(recipe.secrets)} if recipe.secrets else {}),
            }
            for s in recipe.services
        },
        "networks": {NETWORK: {"external": True, "name": NETWORK}},
    }
    return yaml.safe_dump(doc, sort_keys=False)


def compose_args(repo: str, recipe_file: str, override_file: str) -> list[str]:
    """`docker compose` for one Repo's project. Both files are named explicitly, so Compose never
    looks for (and never reads) a developer's own `docker-compose.yml` or `compose.yaml`."""
    return ["docker", "compose", "-p", repo, "-f", recipe_file, "-f", override_file]


@dataclass(frozen=True)
class ServiceReady:
    """One service that passed its readiness check."""

    repo: str
    service: str
    address: str
    ready_at: str  # when it passed, UTC
    seconds_to_ready: float  # from the start of `up`

    def as_record(self) -> dict:
        return {
            "repo": self.repo, "service": self.service, "address": self.address,
            "ready_at": self.ready_at, "seconds_to_ready": self.seconds_to_ready,
        }


class Environment:
    """One Repo's Run recipe, brought up inside the run's sandbox.

    Each Repo is its own Compose project (`-p <repo>`); its services join the shared Environment
    network as `<service>.<repo>`. `sandbox` needs `run(command, timeout, cwd) -> (exit code,
    output)` (never raising on a non-zero exit) and `put_text(path, text)`. Nothing here outlives
    the sandbox: the engine, its containers and its network are the sandbox's own.
    """

    def __init__(
        self,
        sandbox: Any,
        repo: str,
        *,
        now: Callable[[], str],
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        working_copy: str | None = None,
        secrets: Mapping[str, str] | None = None,
    ) -> None:
        self.sandbox = sandbox
        self.repo = repo
        # The Product's Test secrets this Repo's recipe names, name -> value. Held here only to
        # start its containers and to keep values out of everything that is recorded.
        self._secrets = dict(secrets or {})
        self._now, self._clock, self._sleep = now, clock, sleep
        self._recipe_file = f"{working_copy or f'/workspace/{repo}'}/{RECIPE_PATH}"
        self._override_file = f"{ENV_DIR}/{repo}.override.yaml"
        self.recipe: Recipe | None = None
        # Services that passed their readiness check, in the order they did.
        self.ready: list[ServiceReady] = []

    def _compose(self, *args: str) -> str:
        return shlex.join(compose_args(self.repo, self._recipe_file, self._override_file) + list(args))

    def _with_secrets(self, command: str, recipe: Recipe) -> str:
        """`command` run with the recipe's named secrets (and only those) in its environment."""
        given = " ".join(f"{name}={shlex.quote(self._secrets[name])}" for name in recipe.secrets)
        return f"{given} {command}" if given else command

    def _redacted(self, text: str) -> str:
        return redact(text, self._secrets.values())

    def bring_up(self, recipe: Recipe) -> list[ServiceReady]:
        """Start the recipe's services and return once every one has passed its readiness check.

        A recipe with no services starts nothing. Raises EnvironmentBringUpError (naming the Repo,
        the service and the last log lines) if the services do not start or one does not become
        ready in time; the run then ends before its agent starts.
        """
        self.recipe = recipe
        if not recipe.services:
            return self.ready
        if absent := [n for n in recipe.secrets if n not in self._secrets]:
            raise EnvironmentBringUpError(
                f"Repo {self.repo!r}: its Run recipe names Test secret(s) {', '.join(absent)} that this run "
                f"was not given: add them to the Product's Test secrets"
            )
        self.sandbox.run(
            f"docker network inspect {NETWORK} >/dev/null 2>&1 || docker network create {NETWORK}", cwd="/"
        )
        self.sandbox.put_text(self._override_file, compose_override(self.repo, recipe))
        started = self._clock()
        code, out = self.sandbox.run(
            self._with_secrets(self._compose("up", "-d", "--build"), recipe), timeout=UP_TIMEOUT, cwd="/"
        )
        if code != 0:
            raise EnvironmentBringUpError(
                f"Repo {self.repo!r}: its Run recipe's services did not start ({RECIPE_PATH}): "
                f"{self._redacted(_tail(out))}"
            )
        for service in recipe.services:
            check = recipe.readiness[service]
            deadline = started + check.timeout
            while True:
                code, _ = self.sandbox.run(
                    self._compose("exec", "-T", service, *check.command), timeout=min(check.timeout, 60), cwd="/"
                )
                if code == 0:
                    break
                if self._clock() >= deadline:
                    raise EnvironmentBringUpError(
                        f"Repo {self.repo!r}: service {service!r} was not ready {check.timeout:.0f}s after start "
                        f"(its readiness check never passed). Last log lines: {self._redacted(self._last_log_lines(service))}"
                    )
                self._sleep(check.interval)
            self.ready.append(
                ServiceReady(self.repo, service, f"{service}.{self.repo}", self._now(), self._clock() - started)
            )
        return self.ready

    def _last_log_lines(self, service: str) -> str:
        code, out = self.sandbox.run(
            self._compose("logs", "--no-color", "--tail", str(LOG_TAIL_LINES), service), cwd="/"
        )
        return _tail(out) if code == 0 else f"(logs could not be read: {_tail(out, 300)})"

    def save_logs(self, destination: Path) -> dict[str, str]:
        """Write each service's log to `destination/<repo>/<service>.log` (outside the sandbox).

        Returns {"<repo>/<service>": path}. A service whose log cannot be read does not stop the
        others being saved; RuntimeError naming them is raised at the end.
        """
        saved: dict[str, str] = {}
        failed: list[str] = []
        if self.recipe is None:
            return saved
        for service in self.recipe.services:
            code, out = self.sandbox.run(
                self._compose("logs", "--no-color", "--timestamps", service), timeout=120, cwd="/"
            )
            if code != 0:
                failed.append(f"{service!r} ({_tail(out, 200)})")
                continue
            path = destination / self.repo / f"{service}.log"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(self._redacted(out))
            saved[f"{self.repo}/{service}"] = str(path)
        if failed:
            raise LogsNotSaved(f"Repo {self.repo!r}: logs of {', '.join(failed)} could not be saved", saved)
        return saved


def _tail(text: str, chars: int = 800) -> str:
    text = (text or "").strip()
    return text[-chars:] if len(text) > chars else text
