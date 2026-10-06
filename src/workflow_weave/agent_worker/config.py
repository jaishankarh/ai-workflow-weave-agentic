"""Settings the agent worker runs with: Products, Agent profiles, where runs are kept."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .push_gateway import PushGateway
from .subscriptions import SubscriptionStore


@dataclass(frozen=True)
class RepoConfig:
    """A Repo as the Product's configuration describes it.

    `source` is where the sandbox clones the Repo from; for now a path on the
    Sandbox host. `skill_overrides` names central skills for which this Repo's
    own skill of the same name is used instead. Later tickets add
    `coding_standards` and `rules_files` here.
    """

    name: str
    source: str
    skill_overrides: frozenset[str] = frozenset()


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
        repos[repo_name] = RepoConfig(name=repo_name, source=str(repo["source"]), skill_overrides=frozenset(overrides))
    return ProductConfig(name=name, repos=repos)


@dataclass(frozen=True)
class AgentProfile:
    """How to run one agent in a sandbox: its image and ACP command.

    `agent` is the agent provider whose Subscriptions it runs on (e.g.
    `claude-code`); it defaults to the profile's name.
    """

    name: str
    image: str
    acp_command: list[str]
    acp_session_mode: str | None = None
    agent: str | None = None

    @property
    def agent_provider(self) -> str:
        return self.agent or self.name


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
