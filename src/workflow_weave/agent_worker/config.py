"""Settings the agent worker runs with: Products, Agent profiles, where runs are kept."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass(frozen=True)
class RepoConfig:
    """A Repo as the Product's configuration describes it.

    `source` is where the sandbox clones the Repo from; for now a path on the
    Sandbox host. Later tickets add `skill_overrides`, `coding_standards` and
    `rules_files` here.
    """

    name: str
    source: str


@dataclass(frozen=True)
class ProductConfig:
    name: str
    repos: dict[str, RepoConfig] = field(default_factory=dict)


def load_product_config(path: str | Path) -> ProductConfig:
    """Read one Product's configuration file.

    ```yaml
    product: ahdismoi
    repos:
      ahdismoi:
        source: /srv/repos/ahdismoi
    ```
    """
    data = yaml.safe_load(Path(path).read_text()) or {}
    name = data.get("product")
    if not name:
        raise ValueError(f"{path}: 'product' is required")
    repos = {}
    for repo_name, repo in (data.get("repos") or {}).items():
        if not isinstance(repo, dict) or not repo.get("source"):
            raise ValueError(f"{path}: repo {repo_name!r} needs a 'source'")
        repos[repo_name] = RepoConfig(name=repo_name, source=str(repo["source"]))
    return ProductConfig(name=name, repos=repos)


@dataclass(frozen=True)
class AgentProfile:
    """How to run one agent in a sandbox: its image and ACP command."""

    name: str
    image: str
    acp_command: list[str]
    acp_session_mode: str | None = None


@dataclass(frozen=True)
class WorkerSettings:
    runs_dir: Path
    products: dict[str, ProductConfig]
    agent_profiles: dict[str, AgentProfile]
    # `docker run --ulimit nofile=N:N`; None leaves Docker's default.
    sandbox_nofile_limit: int | None = 65536
    sandbox_start_timeout: float = 120.0
