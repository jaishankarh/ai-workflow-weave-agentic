"""The agent worker (Seam A): start, status and cancel agent runs.

    worker = AgentWorker(settings)
    started = worker.start(RunRequest(...))   # returns at once
    # started: Started(run_id, subscription) | NoCapacity | NeedsSetup
    worker.status(started.run_id)            # running | cancelled | outcome + reason
    worker.cancel(started.run_id)            # closes the conversation, removes the sandbox
"""

from .config import (
    PROTECTED_SKILLS,
    AgentProfile,
    ProductConfig,
    ProductConfigError,
    RepoConfig,
    WorkerSettings,
    claude_code_profile,
    load_product_config,
)
from .secret_store import (
    ProductSecrets,
    SecretsError,
    SecretStore,
    load_configured_secret_store,
)
from .subscriptions import (
    Lease,
    Subscription,
    SubscriptionStore,
    load_configured_subscription_store,
    load_subscription_store,
)
from .model import (
    NeedsSetup,
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
    "NeedsSetup",
    "NoCapacity",
    "Outcome",
    "PROTECTED_SKILLS",
    "ProductConfig",
    "ProductConfigError",
    "ProductSecrets",
    "RepoConfig",
    "RepoTarget",
    "RunInputs",
    "RunRecord",
    "RunRequest",
    "RunState",
    "RunStatus",
    "SecretStore",
    "SecretsError",
    "Started",
    "StartResult",
    "Subscription",
    "SubscriptionStore",
    "UnknownRun",
    "WorkerSettings",
    "claude_code_profile",
    "load_product_config",
    "load_configured_secret_store",
    "load_configured_subscription_store",
    "load_subscription_store",
]
