# AI Workflow Weave

Developer workflow as an executable graph. See `CONTEXT.md` for the vocabulary
and `docs/adr/` for decisions.

## Agent worker (Spec 1)

`workflow_weave.agent_worker.AgentWorker` runs an Agent profile in a fresh,
throwaway Docker sandbox through the OpenHands SDK (`ACPAgent` against the
agent-server in the sandbox):

```python
worker = AgentWorker(WorkerSettings(runs_dir=..., products=..., agent_profiles=..., subscriptions=...))
started = worker.start(RunRequest(product=..., agent_profile=..., repos=[RepoTarget(...)],
                                  skill="implement-spec", inputs=RunInputs(spec=..., tasks=[...])))
worker.status(started.run_id)   # running | cancelled | ended with one Outcome + reason
worker.cancel(started.run_id)   # closes the agent's conversation, removes the sandbox
```

`start` leases a Subscription for the run's Product and the Agent profile's
agent from the Subscription store (ADR 0010): the first associated
Subscription below its cap, in the Product's fallback order. It returns
`Started(run_id, subscription)`, or `NoCapacity(product, agent, subscriptions)`
when every associated Subscription is full (not an outcome; queue and retry),
or `NeedsSetup(reason)` (outcome `needs-setup`) when a pre-check fails, without
taking a lease or starting a sandbox. The pre-checks, in order: the Product has a
Subscription associated for the agent; every Repo has a `CONTEXT.md` on its Base
branch. The lease is released however the run ends, cancel included. The credential
goes only into the sandbox environment; results and run records carry the
Subscription's name. The store is one YAML file on the Sandbox host
(`subscription_store.location` in `weave.yaml`; format in
`config/subscriptions.example.yaml`), loaded with
`load_configured_subscription_store` and passed as `WorkerSettings.subscriptions`.
Lease counts live in that store object, so share one store among all workers
on a host.

### Outcomes

A run that ends on its own has exactly one outcome; every outcome but
`succeeded` has a reason naming the problem and carrying the underlying error.
Agent errors are classified from the agent's own `errorKind`, never OpenHands'
codes; the table is `AgentProfile.error_kinds` (default: claude-agent-acp's).

| How the run ended | Outcome |
|---|---|
| Agent's last reply ends `RUN-OUTCOME: done` | `succeeded` |
| Agent finished otherwise, or with `RUN-OUTCOME: gave-up: <why>` | `agent-gave-up` |
| Agent error `errorKind` `authentication_failed`, or ACP code -32000 | `needs-setup` |
| Agent error `errorKind` `rate_limit` / `billing_error` | `quota-exhausted` |
| Any other agent error, a sandbox that fails to start or dies mid-run, unreachable Central skills | `infra-failure` |

`ACP_PROMPT_MAX_RETRIES=0` is set in every sandbox, so a rejected credential
surfaces in seconds; infrastructure retries are the caller's.

Each run's `record.json` and conversation `events.jsonl` are kept under
`runs_dir/<run id>/`, outside the sandbox.

### Skill staging

Each run stages the requested skill and every skill it calls into the agent's
user-level skills folder (`~/.claude/skills/`) in the sandbox, never into a
working copy (`WorkerSettings.central_skills_location`). A skill "calls"
another when a line of its `SKILL.md` mentions the Skill tool and names it in
backticks or double quotes (see `agent_worker/staging.py`). A Repo's
`skill_overrides` in the Product config leaves that central skill unstaged so
the Repo's own `.claude/skills/<name>` loads instead; overrides of
`PROTECTED_SKILLS` are refused on load. Clashes and override disagreements go
to `run.log` and the record's `skill_clashes`; the record's `central_skills`
holds the Central skills version and upstream commit.

### Images

- `sandbox/Dockerfile`: the base sandbox image (agent-server, git, Python).
  Its base image is the build ARG `BASE_IMAGE` (default `ubuntu:24.04`).
- `tests/probe/Dockerfile`: the probe Agent profile used by the tests, built
  on top of the sandbox image (build ARG `SANDBOX_IMAGE`).

The test suite builds both itself (as `weave/sandbox:test` and
`weave/probe-agent:test`). To build by hand:

```sh
docker build -t weave/sandbox:dev sandbox/
docker build --build-arg SANDBOX_IMAGE=weave/sandbox:dev -t weave/probe-agent:dev tests/probe/
```

### Running the tests

The tests start real Docker sandboxes, so Docker must be running.

On a normal machine:

```sh
uv sync
uv run pytest
```

Environment knobs for the test harness:

| Variable | Effect |
|---|---|
| `WEAVE_TEST_BASE_IMAGE` | Base image for the sandbox image (default `ubuntu:24.04`) |
| `WEAVE_TEST_BUILD_NETWORK` | Passed to `docker build --network` |
| `WEAVE_TEST_SKIP_BUILD=1` | Reuse the already built test images |
| `WEAVE_TEST_IMAGE_TAG` | Tag for the test images (default `test`); use one per worktree |
| `WEAVE_TEST_SANDBOX_NOFILE` | Sandbox open-files limit (default 65536; `0` = Docker's default) |

On a host with no image registry and a low open-files cap (such as the cloud
build host used for Spec 1), build from a local base image and lower the limit:

```sh
WEAVE_TEST_BASE_IMAGE=weave/ubuntu-base:noble WEAVE_TEST_BUILD_NETWORK=host \
WEAVE_TEST_SANDBOX_NOFILE=20000 uv run pytest
```
