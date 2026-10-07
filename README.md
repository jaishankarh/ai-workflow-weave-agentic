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
branch; every selected Coding standards file exists. The lease is released however the run ends, cancel included. The credential
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

### Coding standards and the always-on file

Each Repo's `coding_standards` in the Product config is `central` (default),
`central+repo` or `repo`; except under `central` it must name `rules_files`
(paths in the Repo, files or folders), or loading the config fails. The
Product's own file lives in the Central skills at
`<central_skills.location>/products/<Product>/coding-standards.md` (here
`skills/products/<Product>/coding-standards.md`). A Repo's rules files are read
with git from its **Base branch** on the Sandbox host, never from the working
branch or the agent's working copy. A selected file that is missing refuses the
run as `needs-setup` naming it, before any sandbox starts.

The resolved files are copied to `~/.claude/weave/coding-standards/` in the
sandbox and an always-on file is staged as the user-level `~/.claude/CLAUDE.md`,
pointing at each Repo's `CONTEXT.md` in its working copy and at those copies.
Nothing goes into a working copy, so the Repo's own `CLAUDE.md` loads as committed.
The record keeps `always_on_file` and, per Repo, where each standard was read
from (`coding_standards`). See `agent_worker/standards.py`.

### Run inputs and outputs (ADR 0009)

**Local tickets.** The spec and each Task are written on the Sandbox host under
`runs_dir/<run id>/tickets/` and bind-mounted **read-only** into the sandbox at
`/weave/tickets/` (the kernel refuses writes, even as root). Beside them is
`/weave/tickets/issue-tracker.md`, a local-files tracker description modelled
on upstream `setup-matt-pocock-skills/issue-tracker-local.md`. The run's prompt
tells the agent to use it wherever a skill refers to
`docs/agents/issue-tracker.md`, so upstream skills run unforked and nothing is
written into a working copy. The agent works on a writable copy at
`/weave/tracker/` (`spec.md`, `issues/NN-<slug>.md`, each with a `Status:`
line); "closing" a ticket sets `Status: done`. When the run ends, before the
sandbox is removed, the worker reads those lines back into the record's
`tickets_done` (`spec`, `01`, `02`, ... in Task order). See
`agent_worker/local_tickets.py`.

**Push gateway.** The sandbox's only git credential is a random per-run token in
the URL of its only remote, `origin` = `http://weave-git:<port>/<token>/<repo>.git`
(`weave-git` maps to the Docker host). The worker runs a small smart-HTTP git
server (`agent_worker/push_gateway.py`, `git http-backend`) on the Docker
bridge gateway. Per run it keeps a bare mirror of each Repo whose
`pre-receive` hook accepts only `refs/heads/<Integration branch>` (no deletes,
tags or other branches, the Base branch included), and before accepting it
pushes the commit onward to the Repo's `source` with the Sandbox host's own git
credentials. A push is either refused or lands on the real Integration branch;
the token is revoked and the mirrors deleted when the run ends.
`WorkerSettings.push_gateway` lets several workers share one gateway (default:
each worker starts its own).

**No Tracker or Code host token.** Variables such as `GH_TOKEN`,
`GITHUB_TOKEN` and `GITLAB_TOKEN` are dropped from the sandbox environment even
if a Subscription's `env` names one (noted in `run.log`); the sandbox has no
git credential helper and no `gh`/`glab` config, so an issue, comment, label or
PR write has nothing to authenticate with.

**For the real Code host (GitHub).** The design holds as is: the gateway is the
only party with a GitHub credential, and the agent never sees it. Production
needs: a Repo `source` that is the GitHub URL, and a Sandbox host credential
that can push to it (a deploy key or fine-grained token with `contents: write`
only, used by the hook's onward push); ideally a GitHub ruleset restricting
that credential to the Integration branch pattern as a second line of defence;
and, for larger Repos, a persistent mirror per Repo instead of a fresh clone
per run. The gateway must listen only where sandboxes can reach it (the bridge
gateway, not `0.0.0.0`).

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
