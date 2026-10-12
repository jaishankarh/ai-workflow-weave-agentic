"""Environment MCP servers (#55): the built-in catalog and which databases get a server.

A database the Run recipe names (`x-weave.databases`, see `environment.py`) gets one MCP server for
the run when its kind is in the built-in catalog below *and* the Product enables that kind
(`database_mcp_kinds` in the Product's configuration). Each catalog entry pairs a pinned server with
the rule that turns a database's host, port, credentials and name into that server's settings.

The server is a stdio process the agent's own Claude Code starts from the user-level MCP
configuration (`stage_mcp_servers` in `sandbox.py`). It is read-write: the data is the run's own.
It is named for its Repo and service, and it is given one host, the database's address in the
run's own Environment (`<service>.<repo>`), which answers only inside that run's sandbox. Nothing
from the sandbox's or the Sandbox host's environment goes into it: a server's settings hold the
database's own throwaway credentials from the committed recipe and nothing else.

The servers are installed when the sandbox image is built, each in its own virtualenv under
`/opt/weave-mcp/<kind>` (see `sandbox/Dockerfile`), so a run needs no network to start one. The
versions below and the `ARG`s in that Dockerfile are the same pins; `tests` compare them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Iterable, Mapping

from .push_gateway import GATEWAY_HOST

MCP_ROOT = "/opt/weave-mcp"


@dataclass(frozen=True)
class Connection:
    """Where one database is and how to log in. All of it comes from the committed recipe."""

    host: str  # `<service>.<repo>`: that run's own database
    port: int
    user: str
    password: str
    database: str


@dataclass(frozen=True)
class McpServerSettings:
    """What starts one stdio MCP server: command, arguments and environment (nothing inherited)."""

    command: str
    args: tuple[str, ...] = ()
    env: Mapping[str, str] = field(default_factory=dict)

    def as_claude_entry(self) -> dict:
        """The entry for Claude Code's `mcpServers` (user scope)."""
        entry: dict = {"type": "stdio", "command": self.command, "args": list(self.args)}
        if self.env:
            entry["env"] = dict(self.env)
        return entry


@dataclass(frozen=True)
class CatalogEntry:
    """One database kind: its pinned server and the rule that wires it to a database."""

    kind: str
    default_port: int
    package: str  # PyPI distribution
    version: str  # pinned; the same pin as the sandbox image's build ARG
    executable: str  # console script installed by the package, in its own virtualenv
    settings_for: Callable[[Connection], McpServerSettings]
    # The database name when a recipe gives none (Neo4j's default database).
    default_database: str | None = None
    # Dependencies pinned besides the server itself, as pip requirements.
    extra_pins: tuple[str, ...] = ()

    @property
    def pin(self) -> str:
        return f"{self.package}=={self.version}"


def _postgres(c: Connection) -> McpServerSettings:
    # crystaldba/postgres-mcp: `--access-mode=unrestricted` is read-write. It reads the URI from
    # DATABASE_URI (not from an argument, so it is not in the process list).
    from urllib.parse import quote

    uri = f"postgresql://{quote(c.user, safe='')}:{quote(c.password, safe='')}@{c.host}:{c.port}/{quote(c.database, safe='')}"
    return McpServerSettings(
        PG_COMMAND, ("--access-mode=unrestricted",), {"DATABASE_URI": uri},
    )


def _neo4j(c: Connection) -> McpServerSettings:
    # neo4j-contrib's mcp-neo4j-cypher: read-write unless started with `--read-only`.
    return McpServerSettings(
        NEO4J_COMMAND, (),
        {
            "NEO4J_URI": f"bolt://{c.host}:{c.port}",
            "NEO4J_USERNAME": c.user,
            "NEO4J_PASSWORD": c.password,
            "NEO4J_DATABASE": c.database,
        },
    )


def _mysql(c: Connection) -> McpServerSettings:
    # designcomputer/mysql_mcp_server: runs any SQL it is given, with autocommit on.
    return McpServerSettings(
        MYSQL_COMMAND, (),
        {
            "MYSQL_HOST": c.host,
            "MYSQL_PORT": str(c.port),
            "MYSQL_USER": c.user,
            "MYSQL_PASSWORD": c.password,
            "MYSQL_DATABASE": c.database,
        },
    )


PG_COMMAND = f"{MCP_ROOT}/postgres/bin/postgres-mcp"
NEO4J_COMMAND = f"{MCP_ROOT}/neo4j/bin/mcp-neo4j-cypher"
MYSQL_COMMAND = f"{MCP_ROOT}/mysql/bin/mysql_mcp_server"

# The built-in catalog. Versions were checked against PyPI on 2026-10-08 (see README "Images").
CATALOG: dict[str, CatalogEntry] = {
    # postgres-mcp asks for `mcp>=1.5`, which resolves to mcp 2.x, where it fails at import.
    "postgres": CatalogEntry(
        "postgres", 5432, "postgres-mcp", "0.3.0", "postgres-mcp", _postgres, extra_pins=("mcp[cli]==1.30.0",)
    ),
    "neo4j": CatalogEntry(
        "neo4j", 7687, "mcp-neo4j-cypher", "0.6.0", "mcp-neo4j-cypher", _neo4j, default_database="neo4j"
    ),
    "mysql": CatalogEntry("mysql", 3306, "mysql-mcp-server", "0.4.4", "mysql_mcp_server", _mysql),
}

# Kinds a recipe may name that sit in the Environment but have no server (Redis, #55).
NO_SERVER_KINDS: dict[str, int] = {"redis": 6379}  # kind -> default port
KINDS = tuple(CATALOG) + tuple(NO_SERVER_KINDS)


def default_port(kind: str) -> int:
    return CATALOG[kind].default_port if kind in CATALOG else NO_SERVER_KINDS[kind]


@dataclass(frozen=True)
class ExternalMcpDecl:
    """One External MCP server a Run recipe declares (`x-weave.external_mcp`, #56).

    Exactly one of `command` (a stdio server, started inside the run's sandbox: each run has its own
    copy) or `url` (a remote server). `secrets` are the names of the Test secrets it uses, and the
    only credentials it is given: a stdio server gets each as an environment variable of that name; a
    remote one gets them where its `headers` say `${NAME}`.
    """

    name: str
    command: str | None = None
    args: tuple[str, ...] = ()
    url: str | None = None
    headers: tuple[tuple[str, str], ...] = ()
    secrets: tuple[str, ...] = ()

    @property
    def transport(self) -> str:
        return "stdio" if self.command else "http"


@dataclass(frozen=True)
class DatabaseSpec:
    """One database a Run recipe names (`x-weave.databases`)."""

    service: str
    kind: str
    port: int
    user: str | None = None
    password: str | None = None
    database: str | None = None


class McpPlanError(ValueError):
    """The Environment's databases cannot all get a distinctly named server; the message says which."""


@dataclass(frozen=True)
class EnvironmentMcpServer:
    """One Environment MCP server to start for the run."""

    name: str  # as the agent sees it: named for its Repo and service
    repo: str
    service: str
    kind: str
    address: str  # the database's address in this run's Environment: the only host it is given
    package: str  # the pinned server, e.g. `postgres-mcp==0.3.0`
    settings: McpServerSettings

    def as_record(self) -> dict:
        """What the run record says: never a credential (those are in `settings` only)."""
        return {
            "name": self.name, "repo": self.repo, "service": self.service, "kind": self.kind,
            "address": self.address, "server": self.package,
        }


@dataclass(frozen=True)
class OmittedMcpServer:
    """A database the recipe names that gets no server, and why."""

    repo: str
    service: str
    kind: str
    reason: str

    def as_record(self) -> dict:
        return {"repo": self.repo, "service": self.service, "kind": self.kind, "reason": self.reason}


@dataclass
class McpPlan:
    started: list[EnvironmentMcpServer] = field(default_factory=list)
    omitted: list[OmittedMcpServer] = field(default_factory=list)

    def entries(self) -> dict[str, dict]:
        """The servers as Claude Code's `mcpServers` entries, by name."""
        return {s.name: s.settings.as_claude_entry() for s in self.started}


@dataclass(frozen=True)
class ExternalMcpServer:
    """One External MCP server to start for the run (#56)."""

    name: str  # as the agent sees it: `<repo>-<declared name>`
    repo: str
    declared: str  # the name the recipe gave it
    transport: str  # "stdio" or "http"
    secrets: tuple[str, ...]  # the Test secrets it was given, by name
    entry: dict  # Claude Code's `mcpServers` entry; holds the secrets' values, so never recorded

    def as_record(self) -> dict:
        """What the run record says: names only. Not the command, the url or any value."""
        return {
            "name": self.name, "repo": self.repo, "declared": self.declared, "transport": self.transport,
            "secrets": list(self.secrets),
        }


@dataclass
class ExternalMcpPlan:
    started: list[ExternalMcpServer] = field(default_factory=list)

    def entries(self) -> dict[str, dict]:
        return {s.name: s.entry for s in self.started}


# A stdio server is started through `env -i`, so that it sees exactly PATH, HOME and the Test secrets
# its recipe names. Claude Code starts a server in its own environment plus the entry's `env`, and
# the sandbox's environment carries the Subscription's credential: the server must not inherit it.
_ISOLATE = 'exec env -i PATH="$PATH" HOME="$HOME"{names} "$@"'
_ISOLATE_NAME = " {n}=\"${n}\""


def _stdio_entry(decl: ExternalMcpDecl, secrets: Mapping[str, str]) -> dict:
    script = _ISOLATE.format(names="".join(_ISOLATE_NAME.format(n=n) for n in decl.secrets))
    entry: dict = {
        "type": "stdio", "command": "/bin/sh",
        "args": ["-c", script, "weave-external-mcp", decl.command, *decl.args],
    }
    if decl.secrets:
        entry["env"] = {n: secrets[n] for n in decl.secrets}
    return entry


def _http_entry(decl: ExternalMcpDecl, secrets: Mapping[str, str]) -> dict:
    expand = re.compile(r"\$\{([^}]*)\}")
    entry: dict = {"type": "http", "url": decl.url}
    if decl.headers:
        entry["headers"] = {k: expand.sub(lambda m: secrets[m.group(1)], v) for k, v in decl.headers}
    return entry


def plan_external_servers(
    repos: Iterable[tuple[str, Iterable[ExternalMcpDecl]]],
    secrets: Mapping[str, Mapping[str, str]],
    taken: Iterable[str] = (),
) -> ExternalMcpPlan:
    """The External MCP servers to start: `repos` is (Repo name, the servers its recipe declares) in the
    order the Repos come up; `secrets` is each Repo's Test secrets by name (those the run was given);
    `taken` are the names of servers already there (the Environment MCP servers).

    A server is given only the secrets its declaration names. A name taken, or a secret missing,
    is a McpPlanError that names it (never a value).
    """
    plan = ExternalMcpPlan()
    named: dict[str, str] = {name: "an Environment MCP server" for name in taken}
    for repo, decls in repos:
        for decl in decls:
            name = server_name(repo, decl.name)
            if name in named:
                raise McpPlanError(
                    f"Repo {repo!r}'s external server {decl.name!r} would be the MCP server {name!r}, "
                    f"which is already {named[name]}: rename it"
                )
            named[name] = f"Repo {repo!r}'s external server {decl.name!r}"
            have = secrets.get(repo, {})
            if lacking := [n for n in decl.secrets if n not in have]:
                raise McpPlanError(f"MCP server {name!r} names Test secret {lacking[0]!r}, which this run was not given")
            given = {n: have[n] for n in decl.secrets}
            if bad := next((n for n, v in given.items() if "${" in v), None):
                raise McpPlanError(
                    f"MCP server {name!r}: the value of Test secret {bad!r} contains `${{`, which Claude Code "
                    f"would expand before the server saw it"
                )
            entry = _stdio_entry(decl, given) if decl.command else _http_entry(decl, given)
            plan.started.append(ExternalMcpServer(name, repo, decl.name, decl.transport, decl.secrets, entry))
    return plan


def _strings(value: object):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for k, v in value.items():
            yield from _strings(k)
            yield from _strings(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _strings(v)


MIN_FORGE_TOKEN_LENGTH = 6  # shorter "tokens" would match ordinary text


def refuse_forge_credentials(plan: ExternalMcpPlan, forge_values: Iterable[str]) -> None:
    """Raise McpPlanError if any server's configuration holds a Tracker or Code host credential
    (ADR 0009): one of `forge_values` (the push gateway's run token, any Code host token the worker or
    the Subscription had) or the push gateway's address. The last check before the configuration is
    written; the message names the server, never a value."""
    values = [v for v in forge_values if v and len(v) >= MIN_FORGE_TOKEN_LENGTH]
    for server in plan.started:
        for text in _strings(server.entry):
            if GATEWAY_HOST in text:
                raise McpPlanError(
                    f"MCP server {server.name!r} would reach the push gateway ({GATEWAY_HOST}): "
                    f"no External MCP server may reach the Tracker or Code host (ADR 0009)"
                )
            if any(v in text for v in values):
                raise McpPlanError(
                    f"MCP server {server.name!r} would hold a Tracker or Code host credential: "
                    f"no External MCP server may be given one (ADR 0009)"
                )


_NAME_CHARS = re.compile(r"[^A-Za-z0-9_-]")


def server_name(repo: str, service: str) -> str:
    """`<repo>-<service>`, kept to the characters an agent's tool names allow."""
    return _NAME_CHARS.sub("_", f"{repo}-{service}")


def plan_environment_servers(
    repos: Iterable[tuple[str, Iterable[DatabaseSpec]]], enabled: Iterable[str]
) -> McpPlan:
    """Which of the Environment's named databases get a server: `repos` is (Repo name, the
    databases its recipe names) in the order the Repos come up; `enabled` is the Product's kinds.

    Only a database a recipe names is considered, whatever its image. One of a kind the Product
    has not enabled, or one with no server (Redis), is listed as omitted with the reason.
    """
    enabled = frozenset(enabled)
    plan = McpPlan()
    named: dict[str, str] = {}
    for repo, databases in repos:
        for db in databases:
            if db.kind not in CATALOG:
                plan.omitted.append(OmittedMcpServer(
                    repo, db.service, db.kind, f"{db.kind} has no MCP server (it is in the Environment only)"
                ))
                continue
            if db.kind not in enabled:
                plan.omitted.append(OmittedMcpServer(
                    repo, db.service, db.kind, f"kind {db.kind!r} is not enabled for this Product"
                ))
                continue
            entry = CATALOG[db.kind]
            address = f"{db.service}.{repo}"
            name = server_name(repo, db.service)
            if name in named:
                raise McpPlanError(
                    f"{named[name]} and {address} would both be the MCP server {name!r}: rename a Repo or service"
                )
            named[name] = address
            connection = Connection(address, db.port, db.user or "", db.password or "", db.database or "")
            plan.started.append(EnvironmentMcpServer(
                name, repo, db.service, db.kind, address, entry.pin, entry.settings_for(connection)
            ))
    return plan
