"""The agent worker (Seam A): start, status and cancel agent runs.

    worker = AgentWorker(settings)
    started = worker.start(RunRequest(...))   # returns at once
    # started: Started(run_id, subscription) | NoCapacity
    worker.status(started.run_id)            # running | cancelled | outcome + reason
    worker.cancel(started.run_id)            # closes the conversation, removes the sandbox
"""

from .config import AgentProfile, ProductConfig, RepoConfig, WorkerSettings, load_product_config
from .subscriptions import (
    Lease,
    Subscription,
    SubscriptionStore,
    load_configured_subscription_store,
    load_subscription_store,
)
from .model import (
    NoCapacity,
    Outcome,
    RepoTarget,
    RunInputs,
    RunRecord,
    RunRequest,
    RunState,
    RunStatus,
    Started,
    StartResult,
)
from .worker import AgentWorker, UnknownRun

__all__ = [
    "AgentProfile",
    "AgentWorker",
    "Lease",
    "NoCapacity",
    "Outcome",
    "ProductConfig",
    "RepoConfig",
    "RepoTarget",
    "RunInputs",
    "RunRecord",
    "RunRequest",
    "RunState",
    "RunStatus",
    "Started",
    "StartResult",
    "Subscription",
    "SubscriptionStore",
    "UnknownRun",
    "WorkerSettings",
    "load_product_config",
    "load_configured_subscription_store",
    "load_subscription_store",
]
