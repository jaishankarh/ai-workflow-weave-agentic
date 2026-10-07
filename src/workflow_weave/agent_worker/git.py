"""Reading a Repo's clone on the Sandbox host, and reaching its Code host, with the host's own git."""

from __future__ import annotations

import subprocess


class GitError(RuntimeError):
    pass


def has_commit(source: str, ref: str) -> bool:
    """True when `ref` names a commit in the clone at `source` (False if the clone cannot be read)."""
    return subprocess.run(
        ["git", "-C", source, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], capture_output=True
    ).returncode == 0


def remotes(source: str) -> list[str]:
    r = subprocess.run(["git", "-C", source, "remote"], capture_output=True, text=True)
    return r.stdout.split() if r.returncode == 0 else []


def code_host_remote(source: str, configured: str | None) -> str | None:
    """The remote of the clone at `source` that leads to the Code host, or None if it has none.

    `configured` is the Repo's `push_remote`; when unset, `origin` if the clone has one.
    Raises GitError when a configured remote is not in the clone.
    """
    names = remotes(source)
    if configured is not None:
        if configured not in names:
            raise GitError(f"the clone at {source} has no remote {configured!r} (its push_remote)")
        return configured
    return "origin" if "origin" in names else None


def fetch_base(source: str, remote: str, base: str) -> str:
    """Fetch the Base branch from the Code host into the clone; return the ref to read it at."""
    tracking = f"refs/remotes/{remote}/{base}"
    r = subprocess.run(
        ["git", "-C", source, "fetch", "--quiet", remote, f"+refs/heads/{base}:{tracking}"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        raise GitError(f"cannot fetch Base branch {base!r} from {remote!r} into {source}: {r.stderr.strip()}")
    return tracking
