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

Several Repos (#50): `x-weave.depends_on` lists the Repos this one needs (`depends_on: [svc]`); they
are brought up first, each as its own Compose project. A recipe may have no services and only
`depends_on`.

Seeding (#53): every Environment starts from empty databases, and a Repo that needs data says how to
put it there with a seed, a service and a command:

    x-weave:
      seed:
        service: db                         # one of this recipe's own services
        command: psql -U app -f /seed.sql   # string (sh -c) or list; timeout: seconds (default 300)

It runs once, after all the Repos' services are ready, Repos in dependency order (a Repo is seeded
after the Repos it depends on). It runs inside that service's own container, in the Repo's own
Compose project, with only the Test secrets the recipe names (so a Repo has no credential for a
sibling's databases). A Repo with nothing to seed omits the field. Tracked Seed scripts are Spec 2b
(ADR 0011); the workflow only runs this command. `seed_in_dependency_order` is the one function that
seeds, so `reset` can call it again after bringing everything up fresh.
"""

from __future__ import annotations

import re
import shlex
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import yaml

from . import weave_env
from .model import Outcome
from .secret_store import SECRET_NAME, redact

# Fixed, and never the developer's `docker-compose.yml`.
RECIPE_PATH = ".weave/compose.yaml"
WEAVE_KEY = "x-weave"
# The one network every Repo's services join; each service is `<service>.<repo>` on it.
NETWORK = "weave-env"
# Where the generated override lives inside the sandbox (never in a working copy).
ENV_DIR = "/weave/env"
# Where a Repo that does not run from the agent's working copy is checked out (Task or Base branch).
CHECKOUT_DIR = f"{ENV_DIR}/src"
WORKDIR = "/workspace"

# What a Repo in the Environment runs from.
FROM_WORKING_COPY = "working copy"
FROM_TASK_BRANCH = "Task branch"
FROM_BASE_BRANCH = "Base branch"

DEFAULT_READY_TIMEOUT = 60.0
DEFAULT_READY_INTERVAL = 1.0
# How long `docker compose up` may take (it pulls and builds images).
UP_TIMEOUT = 1800.0
LOG_TAIL_LINES = 20
DEFAULT_SEED_TIMEOUT = 300.0


class RecipeError(ValueError):
    """The Repo's Run recipe cannot be used; the message says which Repo and what to fix."""


class EnvironmentBringUpError(RuntimeError):
    """The Environment could not be brought up. Raised before any agent starts.

    `retryable` is true only for a service that would not start or become ready (#51): the one
    failure a Story's own branches can cause, so the only one worth trying again at Base.
    """

    def __init__(self, reason: str, outcome: Outcome = Outcome.NEEDS_SETUP, retryable: bool = False) -> None:
        super().__init__(reason)
        self.reason = reason
        self.outcome = outcome
        self.retryable = retryable


# What a container engine says when the host or the network is at fault, not the recipe (#51):
# an unreachable registry or daemon, a DNS or TLS failure, a timed-out connection, a registry that
# is down or rate-limiting. A recipe naming an image that does not exist, or that needs a login
# ("pull access denied", "manifest unknown"), is not in this list: a human fixes that.
_HOST_FAULT = re.compile(
    r"cannot connect to the docker daemon|is the docker daemon running|error during connect"
    r"|no such host|server misbehaving|temporary failure in name resolution|name or service not known"
    r"|dial tcp|connection refused|connection reset|network is unreachable|no route to host"
    r"|tls handshake timeout|i/o timeout|context deadline exceeded|client\.timeout|request canceled while waiting"
    r"|toomanyrequests|too many requests|unexpected http status: 5\d\d|\b50[234] (bad gateway|service unavailable"
    r"|gateway time-?out)|no space left on device|unexpected eof",
    re.IGNORECASE,
)


_DAEMON_DOWN = re.compile(
    r"cannot connect to the docker daemon|is the docker daemon running|error during connect", re.IGNORECASE
)


def is_host_fault(output: str, daemon_only: bool = False) -> bool:
    """Whether an engine's output says the Sandbox host or its network failed, rather than the recipe.

    `daemon_only` is for the output of a readiness check, which is the service's own and so may
    say "connection refused" about itself: only the engine being unreachable counts there."""
    return bool((_DAEMON_DOWN if daemon_only else _HOST_FAULT).search(output or ""))


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
class Seed:
    """A Repo's seed: a command to run in one of its own services (#53)."""

    service: str
    command: tuple[str, ...]
    timeout: float = DEFAULT_SEED_TIMEOUT


@dataclass(frozen=True)
class Recipe:
    repo: str
    services: tuple[str, ...]
    readiness: dict[str, ReadinessCheck]
    depends_on: tuple[str, ...] = ()
    # Names of the Test secrets the recipe needs (never values).
    secrets: tuple[str, ...] = ()
    # What to run once the services are ready (None: nothing to seed).
    seed: Seed | None = None


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
    depends_on = block.get("depends_on") or []
    if not isinstance(depends_on, list) or not all(isinstance(d, str) and d.strip() for d in depends_on):
        raise bad(f"has `{WEAVE_KEY}.depends_on` that is not a list of Repo names")
    return Recipe(
        repo=repo,
        services=names,
        readiness=readiness,
        depends_on=tuple(depends_on),
        secrets=_parse_secret_names(block, bad),
        seed=_parse_seed(block, names, bad),
    )


def _parse_seed(block: dict, services: tuple[str, ...], bad: Callable[[str], RecipeError]) -> Seed | None:
    raw = block.get("seed")
    if raw is None:
        return None
    where = f"`{WEAVE_KEY}.seed`"
    if not isinstance(raw, dict):
        raise bad(f"has {where} that is not a mapping with a `service` and a `command`")
    service = raw.get("service")
    if not isinstance(service, str) or not service:
        raise bad(f"has {where} without a `service` (the recipe's own service to run the seed command in)")
    if service not in services:
        raise bad(f"has {where} naming service {service!r}, which the recipe does not define")
    command = raw.get("command")
    if isinstance(command, str) and command.strip():
        argv: tuple[str, ...] = ("sh", "-c", command)
    elif isinstance(command, list) and command and all(isinstance(c, str) for c in command):
        argv = tuple(command)
    else:
        raise bad(f"has {where} without a `command` (a non-empty string, or a list of strings)")
    timeout = raw.get("timeout", DEFAULT_SEED_TIMEOUT)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
        raise bad(f"has {where} whose `timeout` is not a positive number of seconds")
    return Seed(service, argv, float(timeout))



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
    """The generated compose override: it labels each container with its Repo and service, and names the Test secrets it needs.

    It never names the Environment network. Compose gives a service its bare name (`db`) as an alias
    on every network it attaches it to, so two Repos with a `db` would both answer to `db` there;
    each service instead joins the network with `docker network connect --alias <service>.<repo>`
    (see `Environment.bring_up`), which adds no other name. The recipe's own default network keeps
    working as with standard tooling."""
    doc = {
        "services": {
            s: {
                "labels": {"weave.repo": repo, "weave.service": s},
                # Names only: Compose takes each value from the environment of the `up` command.
                **({"environment": list(recipe.secrets)} if recipe.secrets else {}),
            }
            for s in recipe.services
        }
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


@dataclass(frozen=True)
class SeedRun:
    """One Repo's seed command that ran to success."""

    repo: str
    service: str
    command: str  # as the recipe wrote it, with any secret value redacted
    seeded_at: str  # when it finished, UTC
    seconds: float

    def as_record(self) -> dict:
        return {
            "repo": self.repo, "service": self.service, "command": self.command,
            "seeded_at": self.seeded_at, "seconds": self.seconds,
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
        # The last seed that succeeded (None: no seed, or not seeded yet).
        self.seeded: SeedRun | None = None
        # The /etc/hosts lines added for this Repo's services, so `tear_down` can take them out again.
        self._hosts_lines: list[str] = []

    def describe(self) -> dict:
        """This Repo as the in-sandbox `weave-env` command is told about it (#54): its recipe,
        services, readiness checks, seed and the *names* of its Test secrets, never a value."""
        if self.recipe is None:
            raise RuntimeError(f"Repo {self.repo!r}: not brought up, so there is nothing to describe")
        r = self.recipe
        return {
            "repo": self.repo,
            "recipe_file": self._recipe_file,
            "override_file": self._override_file,
            "services": list(r.services),
            "depends_on": list(r.depends_on),
            "readiness": {
                s: {"command": list(c.command), "timeout": c.timeout, "interval": c.interval}
                for s, c in r.readiness.items()
            },
            "secrets": list(r.secrets),
            "seed": (
                {"service": r.seed.service, "command": list(r.seed.command), "timeout": r.seed.timeout}
                if r.seed else None
            ),
        }

    def _compose(self, *args: str) -> str:
        return shlex.join(compose_args(self.repo, self._recipe_file, self._override_file) + list(args))

    def _with_secrets(self, command: str, recipe: Recipe) -> str:
        """`command` run with the recipe's named secrets (and only those) in its environment."""
        given = " ".join(f"{name}={shlex.quote(self._secrets[name])}" for name in recipe.secrets)
        return f"{given} {command}" if given else command

    def _redacted(self, text: str) -> str:
        return redact(text, self._secrets.values())

    def _failure(self, message: str, engine_output: str, daemon_only: bool = False) -> EnvironmentBringUpError:
        """A service that would not come up: `infra-failure` when the engine's output blames the host
        or network (never retried), else `needs-setup` that may be retried at Base (#51)."""
        if is_host_fault(engine_output, daemon_only):
            return EnvironmentBringUpError(message, Outcome.INFRA_FAILURE)
        return EnvironmentBringUpError(message, Outcome.NEEDS_SETUP, retryable=True)

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
        # Create (and build) first, join the shared network, then start: a service that reaches a
        # sibling Repo while starting must already be on that network. Secret values go on both `up`
        # commands (Compose reads them from the environment of whichever creates or starts the container).
        code, out = self.sandbox.run(
            self._with_secrets(self._compose("up", "--no-start", "--build"), recipe), timeout=UP_TIMEOUT, cwd="/"
        )
        if code != 0:
            raise self._failure(
                f"Repo {self.repo!r}: its Run recipe's services did not start ({RECIPE_PATH}): "
                f"{self._redacted(_tail(out))}", out,
            )
        containers = {service: self._attach(service) for service in recipe.services}
        code, out = self.sandbox.run(
            self._with_secrets(self._compose("up", "-d", "--no-recreate"), recipe), timeout=UP_TIMEOUT, cwd="/"
        )
        if code != 0:
            raise self._failure(
                f"Repo {self.repo!r}: its Run recipe's services did not start ({RECIPE_PATH}): "
                f"{self._redacted(_tail(out))}", out,
            )
        self._name_in_sandbox(containers)
        for service in recipe.services:
            check = recipe.readiness[service]
            deadline = started + check.timeout
            while True:
                code, last = self.sandbox.run(
                    self._compose("exec", "-T", service, *check.command), timeout=min(check.timeout, 60), cwd="/"
                )
                if code == 0:
                    break
                if self._clock() >= deadline:
                    raise self._failure(
                        f"Repo {self.repo!r}: service {service!r} was not ready {check.timeout:.0f}s after start "
                        f"(its readiness check never passed). Last log lines: {self._redacted(self._last_log_lines(service))}",
                        last, daemon_only=True,
                    )
                self._sleep(check.interval)
            self.ready.append(
                ServiceReady(self.repo, service, f"{service}.{self.repo}", self._now(), self._clock() - started)
            )
        return self.ready

    def is_ready(self) -> bool:
        """Brought up, with every service past its readiness check."""
        return self.recipe is not None and {r.service for r in self.ready} == set(self.recipe.services)

    def seed(self) -> SeedRun | None:
        """Run the recipe's seed command once, in the named service of this Repo's own project.

        Returns None if the recipe has no seed. Only when every service has passed its readiness
        check: otherwise (or if the seed fails) EnvironmentBringUpError. The command sees the
        recipe's named Test secrets and no others, and runs in this Repo's own container, so it
        holds no credential for any other Repo's databases. Safe to call again for a reset.
        """
        if not self.is_ready():
            raise EnvironmentBringUpError(
                f"Repo {self.repo!r}: its seed command would run before its services are ready"
            )
        recipe = self.recipe
        if recipe.seed is None:
            return None
        seed = recipe.seed
        self.seeded = None
        started = self._clock()
        # `-e NAME` hands the named secret on to the command itself, not only to the container.
        passed = [arg for name in recipe.secrets for arg in ("-e", name)]
        code, out = self.sandbox.run(
            self._with_secrets(self._compose("exec", "-T", *passed, seed.service, *seed.command), recipe),
            timeout=seed.timeout, cwd="/",
        )
        shown = self._redacted(seed.command[2] if seed.command[:2] == ("sh", "-c") else shlex.join(seed.command))
        if code != 0:
            # Classified like a service that would not start (#51): a host or network fault is
            # `infra-failure`; anything else may be the Story's branches, so it is retried at Base.
            raise self._failure(
                f"Repo {self.repo!r}: its seed command in service {seed.service!r} failed with exit status "
                f"{code} ({shown}): {self._redacted(_tail(out))}", self._redacted(out),
            )
        self.seeded = SeedRun(self.repo, seed.service, shown, self._now(), self._clock() - started)
        return self.seeded

    def _attach(self, service: str) -> list[str]:
        """Join each of the service's containers to the Environment network as `<service>.<repo>`
        and no other name. Returns the container ids."""
        code, out = self.sandbox.run(self._compose("ps", "-a", "-q", service), cwd="/")
        ids = out.split() if code == 0 else []
        if not ids:
            raise self._failure(
                f"Repo {self.repo!r}: service {service!r} has no container to join the Environment network "
                f"({self._redacted(_tail(out, 300)) or 'none were created'})", out,
            )
        for cid in ids:
            code, out = self.sandbox.run(
                shlex.join(["docker", "network", "connect", "--alias", f"{service}.{self.repo}", NETWORK, cid]),
                cwd="/",
            )
            if code != 0:
                raise self._failure(
                    f"Repo {self.repo!r}: service {service!r} could not join the Environment network "
                    f"as {service}.{self.repo}: {self._redacted(_tail(out, 300))}", out,
                )
        return ids

    def _name_in_sandbox(self, containers: dict[str, list[str]]) -> None:
        """Let the sandbox's own shell (the agent's) resolve `<service>.<repo>` too: Docker's DNS
        answers only inside the Environment's containers. The address is the same in every run; the
        IP behind it is that run's."""
        lines = []
        for service, ids in containers.items():
            code, out = self.sandbox.run(
                shlex.join(["docker", "inspect", "-f",
                            '{{(index .NetworkSettings.Networks "' + NETWORK + '").IPAddress}}', ids[0]]),
                cwd="/",
            )
            ip = out.strip()
            if code != 0 or not ip:
                raise self._failure(
                    f"Repo {self.repo!r}: service {service!r} has no address on the Environment network "
                    f"({self._redacted(_tail(out, 300))})", out,
                )
            lines.append(f"{ip} {service}.{self.repo}")
        self._hosts_lines.extend(lines)
        entries = " ".join(shlex.quote(line) for line in lines)
        code, out = self.sandbox.run(f"printf '%s\\n' {entries} >> /etc/hosts", cwd="/")
        if code != 0:
            raise EnvironmentBringUpError(
                f"Repo {self.repo!r}: its service addresses could not be added to the sandbox's /etc/hosts: "
                f"{_tail(out, 300)}"
            )

    def _last_log_lines(self, service: str) -> str:
        code, out = self.sandbox.run(
            self._compose("logs", "--no-color", "--tail", str(LOG_TAIL_LINES), service), cwd="/"
        )
        return _tail(out) if code == 0 else f"(logs could not be read: {_tail(out, 300)})"

    def tear_down(self) -> None:
        """Remove this Repo's containers and volumes and the addresses it added to the sandbox's
        /etc/hosts, so a second bring-up of the same Repo starts clean (#51). Raises
        EnvironmentBringUpError (`infra-failure`) if the engine will not: a retry on top of the old
        containers would prove nothing."""
        if self.recipe is None or not self.recipe.services:
            return
        code, out = self.sandbox.run(self._compose("down", "-v", "--remove-orphans"), timeout=300, cwd="/")
        if code != 0:
            raise EnvironmentBringUpError(
                f"Repo {self.repo!r}: its first Environment could not be taken down before the retry at Base: "
                f"{self._redacted(_tail(out, 300))}", Outcome.INFRA_FAILURE,
            )
        if self._hosts_lines:
            # Not `sed -i`: /etc/hosts is a bind mount and cannot be replaced, only rewritten.
            drop = " ".join(f"-e {shlex.quote(line)}" for line in self._hosts_lines)
            self.sandbox.run(
                f"grep -v -x -F {drop} /etc/hosts > /tmp/hosts.keep; cat /tmp/hosts.keep > /etc/hosts", cwd="/"
            )
            self._hosts_lines.clear()
        self.ready.clear()

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


def seed_in_dependency_order(environments: Sequence[Environment]) -> list[SeedRun]:
    """Seed each Repo that has a seed command, in the order given: dependencies before the Repos
    that need them (the order `resolve_environment` returns). Nothing is seeded until every Repo's
    services are ready, so a Repo is never seeded against a sibling that is still starting; the
    first failing seed stops the rest. `reset` calls this again after bringing everything up fresh."""
    for environment in environments:
        if not environment.is_ready():
            raise EnvironmentBringUpError(
                f"Repo {environment.repo!r}: its services are not ready, so no Repo is seeded yet"
            )
    return [run for environment in environments if (run := environment.seed())]


def environment_manifest(placements: Sequence["Placement"], environments: Sequence[Environment]) -> dict:
    """What the in-sandbox `weave-env` command is told about the Environment (#54): each Repo in
    dependency order (as `environments` is) with where it runs from and `Environment.describe()`.
    Test secret names only, never values."""
    by_repo = {p.repo: p for p in placements}
    entries = []
    for environment in environments:
        placement = by_repo[environment.repo]
        entries.append({**placement.as_record(), "path": placement.path, **environment.describe()})
    return weave_env.manifest(entries, NETWORK)


# ---------------------------------------------------------------- which Repos, in what order, from where


@dataclass(frozen=True)
class Placement:
    """Where one Repo of the Environment runs from."""

    repo: str
    source: str  # FROM_WORKING_COPY, FROM_TASK_BRANCH or FROM_BASE_BRANCH
    branch: str
    path: str  # the checkout the recipe is read and run from, inside the sandbox
    touched: bool  # the Story touches it; False: pulled in by another recipe's `depends_on`

    def as_record(self) -> dict:
        return {"repo": self.repo, "source": self.source, "branch": self.branch, "touched": self.touched}


def place(
    repo: str, touched: Mapping[str, Any], working_on: str, base_branch_of: Callable[[str], str],
    all_at_base: bool = False,
) -> Placement:
    """The agent's working copy for the Repo being worked on; the Task branch (the Integration
    branch) for another Repo the Story touches; the Base branch for any Repo the Story does not
    touch. An Environment therefore never holds another Story's unmerged work (ADR 0004).

    `all_at_base` is the retry of #51: every Repo, touched or not, at its Base branch, in a checkout
    the agent does not edit."""
    if all_at_base:
        return Placement(repo, FROM_BASE_BRANCH, base_branch_of(repo), f"{CHECKOUT_DIR}/{repo}", repo in touched)
    if repo == working_on:
        return Placement(repo, FROM_WORKING_COPY, touched[repo].integration_branch, f"{WORKDIR}/{repo}", True)
    path = f"{CHECKOUT_DIR}/{repo}"
    if repo in touched:
        return Placement(repo, FROM_TASK_BRANCH, touched[repo].integration_branch, path, True)
    return Placement(repo, FROM_BASE_BRANCH, base_branch_of(repo), path, False)


def resolve_environment(
    touched: Mapping[str, Any],
    working_on: str,
    base_branch_of: Callable[[str], str],
    load: Callable[[Placement], Recipe | None],
    all_at_base: bool = False,
) -> list[tuple[Placement, Recipe]]:
    """The Repos of the Environment with their recipes, dependencies before the Repos that need them.

    `touched` maps each Repo the Story touches to its target (`integration_branch`); `load` makes
    the Repo's checkout available and returns its Run recipe, or None if it has none. A touched Repo
    with no recipe is left out; a dependency with none, or a dependency cycle, is a RecipeError.
    """
    loaded: dict[str, tuple[Placement, Recipe | None]] = {}
    ordered: list[tuple[Placement, Recipe]] = []
    done: set[str] = set()

    def visit(repo: str, path: tuple[str, ...]) -> None:
        if repo in path:
            cycle = " -> ".join((*path[path.index(repo):], repo))
            raise RecipeError(f"Repo {repo!r}: the Run recipes' depends_on form a cycle: {cycle}")
        if repo in done:
            return
        if repo not in loaded:
            placement = place(repo, touched, working_on, base_branch_of, all_at_base)
            loaded[repo] = (placement, load(placement))
        placement, recipe = loaded[repo]
        if recipe is None:
            if path:
                raise RecipeError(
                    f"Repo {repo!r}: Repo {path[-1]!r} depends on it, but it has no Run recipe ({RECIPE_PATH})"
                )
            return  # a touched Repo without a recipe simply is not in the Environment
        for dependency in recipe.depends_on:
            visit(dependency, (*path, repo))
        done.add(repo)
        ordered.append((placement, recipe))

    for repo in touched:
        visit(repo, ())
    return ordered
