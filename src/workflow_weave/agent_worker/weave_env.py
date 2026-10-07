"""`weave-env`: the agent's one command for its Environment (#54, ADR 0004).

This file is installed into the run's sandbox as `/usr/local/bin/weave-env` and run there by the
agent, so it uses the standard library only and imports nothing from this package. The agent
manages its Environment through it and never needs raw Docker:

    weave-env status                   every service of every Repo, and whether it is ready
    weave-env logs <repo> [service]    recent output of one service (or of all of the Repo's)
    weave-env rebuild <repo>           rebuild that Repo's project from its working copy, keeping its data
    weave-env reset                    tear the whole Environment down and bring it up fresh, re-seeded

What the Environment is comes from a manifest the worker writes into the sandbox once the
Environment is up (`MANIFEST_PATH`): for each Repo in dependency order, its recipe, services,
readiness checks, seed and the *names* of its Test secrets. It never holds a secret value, since
the agent must not (Spec 2): when a container has to be recreated, the values are read back from
the Repo's own containers, which have had them since bring-up, and handed on the same way the
worker does (the recipe's named secrets, and only those, in the environment of `docker compose`).
Output is redacted of those values too.

The manifest is absent when the run has no Environment (no touched Repo has a Run recipe); the
command then says so rather than failing.

The steps of a bring-up here (create, join the Environment network under `<service>.<repo>`, start,
name the services in /etc/hosts, wait for readiness, seed) are the ones `Environment.bring_up` and
`seed_in_dependency_order` take on the worker side; a change to one belongs in the other.
"""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
import time
from typing import Any, Callable, Mapping, Sequence, TextIO

MANIFEST_PATH = "/weave/env/manifest.json"
# Where the worker installs this file in the sandbox.
INSTALL_PATH = "/usr/local/bin/weave-env"
MANIFEST_VERSION = 1
LOG_TAIL_LINES = 200
UP_TIMEOUT = 1800.0
DOWN_TIMEOUT = 600.0

Shell = Callable[..., "tuple[int, str]"]


def manifest(entries: Sequence[Mapping[str, Any]], network: str) -> dict:
    """What the command is told about the Environment: `entries` (one per Repo, dependencies first)
    each carry the Repo's placement and `Environment.describe()`."""
    return {"version": MANIFEST_VERSION, "network": network, "repos": [dict(e) for e in entries]}


class Failure(Exception):
    """The command cannot do what was asked; the message is what the agent is shown."""


def _tail(text: str, chars: int = 800) -> str:
    text = (text or "").strip()
    return text[-chars:] if len(text) > chars else text


def _local_shell(command: str, timeout: float = 120) -> tuple[int, str]:
    try:
        r = subprocess.run(["sh", "-c", command], capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout:.0f}s"
    return r.returncode, (r.stdout or "") + (r.stderr or "")


class _Weave:
    def __init__(self, data: dict, shell: Shell, hosts_path: str, out: TextIO,
                 clock: Callable[[], float], sleep: Callable[[float], None]) -> None:
        self.network: str = data["network"]
        self.repos: list[dict] = data["repos"]
        self.shell, self.hosts_path, self.out, self.clock, self.sleep = shell, hosts_path, out, clock, sleep
        # Every secret value read back so far, whichever Repo's: nothing shown may contain one.
        self.known: dict[tuple[str, str], str] = {}

    # ------------------------------------------------------------------ helpers

    def say(self, text: str = "") -> None:
        self.out.write(text + "\n")

    def entry(self, repo: str) -> dict:
        for e in self.repos:
            if e["repo"] == repo:
                return e
        raise Failure(f"unknown Repo {repo!r}; this Environment has: {', '.join(e['repo'] for e in self.repos)}")

    def compose(self, entry: dict, *args: str) -> str:
        return shlex.join(["docker", "compose", "-p", entry["repo"], "-f", entry["recipe_file"],
                           "-f", entry["override_file"], *args])

    def container_ids(self, entry: dict, service: str) -> list[str]:
        code, out = self.shell(self.compose(entry, "ps", "-a", "-q", service))
        return out.split() if code == 0 else []

    def secret_values(self, entry: dict) -> dict[str, str]:
        """The values of the Repo's named Test secrets, read back from its own containers."""
        wanted = set(entry["secrets"])
        found: dict[str, str] = {}
        if not wanted:
            return found
        for service in entry["services"]:
            for cid in self.container_ids(entry, service):
                code, out = self.shell(shlex.join(
                    ["docker", "inspect", "-f", "{{range .Config.Env}}{{println .}}{{end}}", cid]))
                if code != 0:
                    continue
                for line in out.splitlines():
                    name, _, value = line.partition("=")
                    if name in wanted and name not in found:
                        found[name] = value
                        self.known[(entry["repo"], name)] = value
        return found

    def need_secrets(self, entry: dict) -> dict[str, str]:
        values = self.secret_values(entry)
        if lacking := [n for n in entry["secrets"] if n not in values]:
            raise Failure(
                f"Repo {entry['repo']!r}: Test secret(s) {', '.join(lacking)} could not be read back from its "
                f"containers, so they cannot be recreated with them"
            )
        return values

    @staticmethod
    def with_secrets(command: str, entry: dict, values: Mapping[str, str]) -> str:
        given = " ".join(f"{n}={shlex.quote(values[n])}" for n in entry["secrets"] if n in values)
        return f"{given} {command}" if given else command

    def redact(self, text: str, values: Mapping[str, str]) -> str:
        for value in sorted({*values.values(), *self.known.values()}, key=len, reverse=True):
            if value:
                text = text.replace(value, "***")
        return text

    def address(self, entry: dict, service: str) -> str:
        return f"{service}.{entry['repo']}"

    # ------------------------------------------------------------------ status

    def status(self) -> None:
        rows = [("REPO", "SERVICE", "ADDRESS", "STATE", "READY", "RUNS FROM")]
        notes = []
        total = ready = 0
        for e in self.repos:
            origin = f"{e.get('source', '?')} ({e.get('branch', '?')})"
            if not e["services"]:
                needs = f"; depends on {', '.join(e['depends_on'])}" if e.get("depends_on") else ""
                notes.append(f"{e['repo']}: no services of its own{needs} ({origin})")
                continue
            for service in e["services"]:
                total += 1
                ids = self.container_ids(e, service)
                state = "absent"
                if ids:
                    code, out = self.shell(shlex.join(["docker", "inspect", "-f", "{{.State.Status}}", ids[0]]))
                    state = out.strip() or "unknown" if code == 0 else "unknown"
                ok = state == "running" and self.check(e, service)
                ready += ok
                rows.append((e["repo"], service, self.address(e, service), state,
                             "ready" if ok else "not ready", origin))
        widths = [max(len(r[i]) for r in rows) for i in range(5)]
        for r in rows:
            self.say("  ".join(c.ljust(w) for c, w in zip(r[:5], widths)) + "  " + r[5])
        for note in notes:
            self.say(note)
        self.say(f"{ready} of {total} services ready")

    def check(self, entry: dict, service: str) -> bool:
        check = entry["readiness"][service]
        code, _ = self.shell(self.compose(entry, "exec", "-T", service, *check["command"]),
                             timeout=min(check["timeout"], 60))
        return code == 0

    def own_services(self, entry: dict, service: str | None = None) -> list[str]:
        """The Repo's services (or the one named), or a Failure saying what there is."""
        if not entry["services"]:
            raise Failure(f"Repo {entry['repo']!r} runs no services of its own (it only depends on "
                          f"{', '.join(entry['depends_on']) or 'other Repos'}), so it has nothing to act on")
        if service is not None and service not in entry["services"]:
            raise Failure(f"Repo {entry['repo']!r} has no service {service!r}; it has: {', '.join(entry['services'])}")
        return [service] if service else list(entry["services"])

    # ------------------------------------------------------------------ logs

    def logs(self, repo: str, service: str | None) -> None:
        entry = self.entry(repo)
        services = self.own_services(entry, service)
        args = ["logs", "--no-color", "--tail", str(LOG_TAIL_LINES), *([service] if service else [])]
        code, out = self.shell(self.compose(entry, *args), timeout=120)
        shown = self.redact(out, self.secret_values(entry))
        if code != 0:
            raise Failure(f"the logs of Repo {repo!r} ({', '.join(services)}) could not be read: {_tail(shown, 500)}")
        self.out.write(shown if shown.endswith("\n") or not shown else shown + "\n")

    # ------------------------------------------------------------------ bring-up steps shared by rebuild and reset

    def fail(self, entry: dict, problem: str, output: str, values: Mapping[str, str]) -> Failure:
        return Failure(f"Repo {entry['repo']!r}: {problem}: {_tail(self.redact(output, values))}")

    def up(self, entry: dict, values: Mapping[str, str]) -> None:
        """Create (and build, from the Repo's recipe where it stands now) and start the Repo's
        services, join them to the Environment network as `<service>.<repo>`, name them in the
        sandbox, and return once every one has passed its readiness check. Volumes are untouched."""
        if not entry["services"]:
            return
        self.shell(f"docker network inspect {shlex.quote(self.network)} >/dev/null 2>&1 || "
                   f"docker network create {shlex.quote(self.network)}")
        started = self.clock()
        create = self.with_secrets(self.compose(entry, "up", "--no-start", "--build"), entry, values)
        code, out = self.shell(create, timeout=UP_TIMEOUT)
        if code != 0:
            raise self.fail(entry, "its services did not start", out, values)
        for service in entry["services"]:
            ids = self.container_ids(entry, service)
            if not ids:
                raise Failure(f"Repo {entry['repo']!r}: service {service!r} has no container to join the "
                              f"Environment network")
            for cid in ids:
                code, out = self.shell(shlex.join(["docker", "network", "connect", "--alias",
                                                   self.address(entry, service), self.network, cid]))
                # Still joined (the container was not recreated): nothing to do.
                if code != 0 and "already exists" not in out:
                    raise Failure(f"Repo {entry['repo']!r}: service {service!r} could not join the Environment "
                                  f"network as {self.address(entry, service)}: {_tail(out, 300)}")
        start = self.with_secrets(self.compose(entry, "up", "-d", "--no-recreate"), entry, values)
        code, out = self.shell(start, timeout=UP_TIMEOUT)
        if code != 0:
            raise self.fail(entry, "its services did not start", out, values)
        self.name_in_sandbox(entry)
        for service in entry["services"]:
            check = entry["readiness"][service]
            deadline = started + check["timeout"]
            while not self.check(entry, service):
                if self.clock() >= deadline:
                    _, logs = self.shell(self.compose(entry, "logs", "--no-color", "--tail", "20", service))
                    raise self.fail(
                        entry, f"service {service!r} was not ready {check['timeout']:.0f}s after start "
                        f"(its readiness check never passed). Last log lines", logs, values)
                self.sleep(check["interval"])
            self.say(f"{entry['repo']}/{service}: ready")

    def name_in_sandbox(self, entry: dict) -> None:
        """Point `<service>.<repo>` at each service's current address in the sandbox's own hosts file
        (Docker's DNS answers only inside the Environment's containers), one line per name."""
        names = {self.address(entry, s): s for s in entry["services"]}
        new = []
        for name, service in names.items():
            ids = self.container_ids(entry, service)
            code, out = self.shell(shlex.join(["docker", "inspect", "-f",
                                               '{{(index .NetworkSettings.Networks "' + self.network + '").IPAddress}}',
                                               ids[0]]))
            ip = out.strip()
            if code != 0 or not ip:
                raise Failure(f"Repo {entry['repo']!r}: service {service!r} has no address on the Environment "
                              f"network ({_tail(out, 300)})")
            new.append(f"{ip} {name}")
        self.edit_hosts(drop=set(names), add=new)

    def edit_hosts(self, drop: set[str], add: Sequence[str]) -> None:
        try:
            with open(self.hosts_path) as f:
                lines = f.read().splitlines()
            lines = [line for line in lines if not (line.split() and line.split()[-1] in drop)]
            with open(self.hosts_path, "w") as f:
                f.write("\n".join([*lines, *add]) + "\n")
        except OSError as e:
            raise Failure(f"the service addresses could not be written to {self.hosts_path}: {e}")

    def seed(self, entry: dict, values: Mapping[str, str]) -> None:
        seed = entry.get("seed")
        if not seed:
            return
        passed = [arg for name in entry["secrets"] for arg in ("-e", name)]
        command = self.with_secrets(self.compose(entry, "exec", "-T", *passed, seed["service"], *seed["command"]),
                                    entry, values)
        code, out = self.shell(command, timeout=seed["timeout"])
        if code != 0:
            raise self.fail(entry, f"its seed command in service {seed['service']!r} failed with exit status "
                                   f"{code}", out, values)
        self.say(f"{entry['repo']}: seeded")

    # ------------------------------------------------------------------ rebuild

    def rebuild(self, repo: str) -> None:
        entry = self.entry(repo)
        self.own_services(entry)
        values = self.need_secrets(entry)
        self.say(f"Rebuilding Repo {repo!r} from {entry['recipe_file']} (its data is kept)")
        self.up(entry, values)
        self.say(f"Repo {repo!r} rebuilt; every service is ready and its data is as it was")

    # ------------------------------------------------------------------ reset

    def reset(self) -> None:
        with_services = [e for e in self.repos if e["services"]]
        # The values come from the containers about to be removed, so read them first.
        values = {e["repo"]: self.need_secrets(e) for e in with_services}
        self.say("Resetting the Environment: removing every service and its data")
        for e in reversed(with_services):  # the Repos that need others go before those they need
            code, out = self.shell(self.compose(e, "down", "-v", "--remove-orphans"), timeout=DOWN_TIMEOUT)
            if code != 0:
                raise self.fail(e, "its project could not be torn down", out, values[e["repo"]])
        self.edit_hosts(drop={self.address(e, s) for e in with_services for s in e["services"]}, add=[])
        for e in with_services:
            self.up(e, values[e["repo"]])
        for e in with_services:
            self.seed(e, values[e["repo"]])
        self.say("Environment reset: fresh, and seeded in dependency order")


NO_ENVIRONMENT = (
    "This run has no Environment: none of the Repos it touches has a Run recipe (.weave/compose.yaml), "
    "so there are no services to show or manage. Nothing is wrong; a Repo without a Run recipe simply "
    "runs without an Environment."
)


def main(
    argv: Sequence[str],
    *,
    manifest_path: str = MANIFEST_PATH,
    shell: Shell = _local_shell,
    hosts_path: str = "/etc/hosts",
    out: TextIO | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    out = out or sys.stdout
    parser = argparse.ArgumentParser(
        prog="weave-env", description="Inspect and manage this run's Environment (no Docker commands needed)."
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="every service of every Repo, and whether it is ready")
    logs = sub.add_parser("logs", help="recent output of a service (all of the Repo's if none is named)")
    logs.add_argument("repo")
    logs.add_argument("service", nargs="?")
    rebuild = sub.add_parser("rebuild", help="rebuild a Repo's services from its working copy, keeping its data")
    rebuild.add_argument("repo")
    sub.add_parser("reset", help="tear the Environment down and bring it up fresh, re-seeded")
    args = parser.parse_args(list(argv))
    try:
        try:
            with open(manifest_path) as f:
                data = json.load(f)
        except FileNotFoundError:
            raise Failure(NO_ENVIRONMENT)
        except (OSError, ValueError) as e:
            raise Failure(f"the Environment's manifest {manifest_path} cannot be read ({e}); "
                          f"the run's Environment cannot be managed from here")
        weave = _Weave(data, shell, hosts_path, out, clock, sleep)
        if args.command == "status":
            weave.status()
        elif args.command == "logs":
            weave.logs(args.repo, args.service)
        elif args.command == "rebuild":
            weave.rebuild(args.repo)
        else:
            weave.reset()
        return 0
    except Failure as e:
        out.write(f"weave-env: {e}\n")
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
