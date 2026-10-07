"""Settings the agent worker runs with: Products, Agent profiles, where runs are kept."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

import yaml

from .model import Outcome
from .outcomes import CLAUDE_AGENT_ACP_ERROR_KINDS
from .push_gateway import PushGateway
from .subscriptions import SubscriptionStore


@dataclass(frozen=True)
class RepoConfig:
    """A Repo as the Product's configuration describes it.

    `source` is where the sandbox clones the Repo from; for now a path on the
    Sandbox host. `skill_overrides` names central skills for which this Repo's
    own skill of the same name is used instead.

    `coding_standards` selects the Repo's Coding standards: `central` (the
    Product's one coding-standards file), `central+repo` (that file and the
    Repo's own `rules_files`) or `repo` (the Repo's own `rules_files` alone).
    A rules file is a path in the Repo, a file or a folder of files, always
    read as it stands on the Repo's Base branch.
    """

    name: str
    source: str
    skill_overrides: frozenset[str] = frozenset()
    coding_standards: str = "central"
    rules_files: tuple[str, ...] = ()

    @property
    def uses_product_standards(self) -> bool:
        return self.coding_standards in ("central", "central+repo")

    @property
    def uses_repo_rules(self) -> bool:
        return self.coding_standards in ("central+repo", "repo")


CODING_STANDARDS_MODES = ("central", "central+repo", "repo")


# Skills whose output the Dispatcher reads back: a Skill override naming one is
# refused (the fix skill, the review skill, the test rules). Their skills arrive in
# later specs; these are the names they will have in the Central skills.
PROTECTED_SKILLS: frozenset[str] = frozenset({"fix", "review", "test-rules"})


class ProductConfigError(ValueError):
    """A Product's configuration is invalid."""


@dataclass(frozen=True)
class ProductConfig:
    name: str
    repos: dict[str, RepoConfig] = field(default_factory=dict)


def load_product_config(path: str | Path, protected_skills: frozenset[str] = PROTECTED_SKILLS) -> ProductConfig:
    """Read one Product's configuration file.

    ```yaml
    product: ahdismoi
    repos:
      ahdismoi:
        source: /srv/repos/ahdismoi
        skill_overrides: [tdd]     # optional; never a protected skill
        coding_standards: central+repo   # central (default) | central+repo | repo
        rules_files: [CLAUDE.md, .claude/rules]   # required unless central
    ```
    """
    data = yaml.safe_load(Path(path).read_text()) or {}
    name = data.get("product")
    if not name:
        raise ProductConfigError(f"{path}: 'product' is required")
    repos = {}
    for repo_name, repo in (data.get("repos") or {}).items():
        if not isinstance(repo, dict) or not repo.get("source"):
            raise ProductConfigError(f"{path}: repo {repo_name!r} needs a 'source'")
        overrides = repo.get("skill_overrides") or []
        if not isinstance(overrides, list) or not all(isinstance(o, str) and o for o in overrides):
            raise ProductConfigError(f"{path}: repo {repo_name!r}: 'skill_overrides' must be a list of skill names")
        if refused := sorted(set(overrides) & protected_skills):
            raise ProductConfigError(
                f"{path}: repo {repo_name!r}: a Skill override is not allowed for protected skill(s) "
                f"{', '.join(refused)} (their output is read back by the Dispatcher)"
            )
        mode = repo.get("coding_standards") or "central"
        if mode not in CODING_STANDARDS_MODES:
            raise ProductConfigError(
                f"{path}: repo {repo_name!r}: 'coding_standards' must be one of {', '.join(CODING_STANDARDS_MODES)}, "
                f"not {mode!r}"
            )
        rules = repo.get("rules_files") or []
        if not isinstance(rules, list) or not all(isinstance(r, str) and r.strip("/") for r in rules):
            raise ProductConfigError(f"{path}: repo {repo_name!r}: 'rules_files' must be a list of paths in the Repo")
        if mode != "central" and not rules:
            raise ProductConfigError(
                f"{path}: repo {repo_name!r}: coding_standards {mode!r} needs 'rules_files' naming the Repo's rules files"
            )
        repos[repo_name] = RepoConfig(
            name=repo_name,
            source=str(repo["source"]),
            skill_overrides=frozenset(overrides),
            coding_standards=mode,
            rules_files=tuple(r.strip("/") for r in rules),
        )
    return ProductConfig(name=name, repos=repos)


@dataclass(frozen=True)
class AgentProfile:
    """How to run one agent in a sandbox: its image and ACP command.

    `agent` is the agent provider whose Subscriptions it runs on (e.g.
    `claude-code`); it defaults to the profile's name. `error_kinds` maps the
    agent's own error kinds to the outcome they mean; any other kind is an
    infra-failure. The default is claude-agent-acp's vocabulary.
    `sandbox_env_removed` names environment variables that never reach the
    sandbox, even if a Subscription's credential env names them.
    """

    name: str
    image: str
    acp_command: list[str]
    acp_session_mode: str | None = None
    agent: str | None = None
    error_kinds: Mapping[str, Outcome] = field(default_factory=lambda: dict(CLAUDE_AGENT_ACP_ERROR_KINDS))
    sandbox_env_removed: frozenset[str] = frozenset()

    @property
    def agent_provider(self) -> str:
        return self.agent or self.name


# Built from sandbox/claude-code/Dockerfile (see README "Images").
CLAUDE_CODE_IMAGE = "weave/claude-code:dev"
# Either of these would win over CLAUDE_CODE_OAUTH_TOKEN, so the run would not use
# the subscription (#13).
CLAUDE_CODE_ENV_REMOVED = frozenset({"ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL"})


def claude_code_profile(
    *, image: str = CLAUDE_CODE_IMAGE, acp_command: list[str] | None = None, name: str = "claude-code"
) -> AgentProfile:
    """The Claude Code Agent profile on a subscription token.

    Its Subscriptions' credential env is `CLAUDE_CODE_OAUTH_TOKEN` (from `claude setup-token`).
    Claude Code runs through claude-agent-acp in `bypassPermissions` mode (allowed as root
    because the image sets `IS_SANDBOX=1`), so a run never stops for a permission prompt.
    `acp_command` is replaceable only so tests can put the probe in Claude Code's place.
    """
    return AgentProfile(
        name=name,
        image=image,
        acp_command=acp_command or ["claude-agent-acp"],
        acp_session_mode="bypassPermissions",
        agent="claude-code",
        error_kinds=dict(CLAUDE_AGENT_ACP_ERROR_KINDS),
        sandbox_env_removed=CLAUDE_CODE_ENV_REMOVED,
    )


@dataclass(frozen=True)
class WorkerSettings:
    runs_dir: Path
    products: dict[str, ProductConfig]
    agent_profiles: dict[str, AgentProfile]
    # Shared by every worker on the Sandbox host: it holds the live leases.
    subscriptions: SubscriptionStore
    # `docker run --ulimit nofile=N:N`; None leaves Docker's default.
    sandbox_nofile_limit: int | None = 65536
    sandbox_start_timeout: float = 120.0
    # Where the Central skills live (`central_skills.location` in weave.yaml).
    central_skills_location: Path | None = None
    # The sandboxes' only git remote (ADR 0009). None: the worker starts its own,
    # listening on the Docker bridge gateway.
    push_gateway: PushGateway | None = None
