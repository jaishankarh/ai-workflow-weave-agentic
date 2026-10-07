"""The Subscription store (ADR 0010): agent accounts owned by the Sandbox host, leased per run.

Spec 1 reads it from one config file on the Sandbox host:

```yaml
subscriptions:
  claude-main:                 # the Subscription's name
    agent: claude-code         # the agent provider it is an account for
    cap: 2                     # runs at once, shared by every Product using it
    env:                       # its credential, as sandbox environment
      CLAUDE_CODE_OAUTH_TOKEN: sk-ant-oat01-...
products:
  ahdismoi:                    # a Product's associations, per agent, in fallback order
    claude-code: [claude-main, claude-spare]
```

The association list is the only authority on which Product may lease which
Subscription. Where the file lives is the `subscription_store.location` setting
in `weave.yaml` (see `load_configured_subscription_store`); it holds
credentials, so it stays on the Sandbox host, outside any repo.

Live lease counts are kept here, in memory, so one store must be
shared by every worker on the host.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass(frozen=True)
class Subscription:
    name: str
    agent: str
    cap: int
    env: dict[str, str] = field(repr=False)  # the credential: never printed, never recorded


@dataclass(frozen=True)
class Lease:
    """One run's hold on a Subscription. Hand back with `SubscriptionStore.release`."""

    subscription: Subscription
    lease_id: int

    @property
    def name(self) -> str:
        return self.subscription.name

    @property
    def env(self) -> dict[str, str]:
        return self.subscription.env


class SubscriptionStore:
    def __init__(self, subscriptions: dict[str, Subscription], associations: dict[str, dict[str, list[str]]]) -> None:
        for product, by_agent in associations.items():
            for agent, names in by_agent.items():
                for name in names:
                    sub = subscriptions.get(name)
                    if sub is None:
                        raise ValueError(f"Product {product!r} is associated with unknown Subscription {name!r}")
                    if sub.agent != agent:
                        raise ValueError(
                            f"Product {product!r} lists Subscription {name!r} under agent {agent!r}, "
                            f"but it is for {sub.agent!r}"
                        )
        self._subscriptions = subscriptions
        self._associations = associations
        self._lock = threading.Lock()
        self._leases: dict[int, str] = {}  # lease id -> Subscription name
        self._next_id = 0

    def associated(self, product: str, agent: str) -> list[str]:
        """The Subscriptions a Product may lease for an agent, in fallback order."""
        return list(self._associations.get(product, {}).get(agent, []))

    def in_use(self, name: str) -> int:
        with self._lock:
            return sum(1 for n in self._leases.values() if n == name)

    def lease(self, product: str, agent: str) -> Lease | None:
        """Lease the first Subscription associated with the Product for the agent that has room.

        None when every associated Subscription is at its cap (or none is associated).
        """
        with self._lock:
            for name in self.associated(product, agent):
                sub = self._subscriptions[name]
                if sum(1 for n in self._leases.values() if n == name) < sub.cap:
                    self._next_id += 1
                    self._leases[self._next_id] = name
                    return Lease(sub, self._next_id)
        return None

    def release(self, lease: Lease) -> None:
        """Return a lease. Releasing one twice is harmless."""
        with self._lock:
            self._leases.pop(lease.lease_id, None)


def load_subscription_store(path: str | Path) -> SubscriptionStore:
    """Read the Subscription store's config file (format in the module docstring)."""
    data = yaml.safe_load(Path(path).read_text()) or {}
    subscriptions: dict[str, Subscription] = {}
    for name, s in (data.get("subscriptions") or {}).items():
        if not isinstance(s, dict) or not s.get("agent"):
            raise ValueError(f"{path}: Subscription {name!r} needs an 'agent'")
        cap = s.get("cap", 1)
        if not isinstance(cap, int) or cap < 1:
            raise ValueError(f"{path}: Subscription {name!r} needs a 'cap' of at least 1")
        env = s.get("env") or {}
        if not isinstance(env, dict) or not env:
            raise ValueError(f"{path}: Subscription {name!r} needs its credential 'env'")
        subscriptions[name] = Subscription(
            name=name, agent=str(s["agent"]), cap=cap, env={str(k): str(v) for k, v in env.items()}
        )
    associations: dict[str, dict[str, list[str]]] = {}
    for product, by_agent in (data.get("products") or {}).items():
        associations[product] = {agent: [str(n) for n in (names or [])] for agent, names in (by_agent or {}).items()}
    try:
        return SubscriptionStore(subscriptions, associations)
    except ValueError as e:
        raise ValueError(f"{path}: {e}") from None


def load_configured_subscription_store(config_path: str | Path) -> SubscriptionStore:
    """Read the store named by `subscription_store.location` in `weave.yaml`.

    A relative location is resolved from the config file's folder.
    """
    config_path = Path(config_path)
    config = yaml.safe_load(config_path.read_text()) or {}
    location = (config.get("subscription_store") or {}).get("location")
    if not location:
        raise ValueError(f"{config_path}: `subscription_store.location` is not set")
    return load_subscription_store((config_path.parent / Path(location).expanduser()).resolve())
