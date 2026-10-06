# AI Workflow Weave

Developer workflow as an executable graph. See `CONTEXT.md` for the vocabulary
and `docs/adr/` for decisions.

## Agent worker (Spec 1)

`workflow_weave.agent_worker.AgentWorker` runs an Agent profile in a fresh,
throwaway Docker sandbox through the OpenHands SDK (`ACPAgent` against the
agent-server in the sandbox):

```python
worker = AgentWorker(WorkerSettings(runs_dir=..., products=..., agent_profiles=...))
started = worker.start(RunRequest(product=..., agent_profile=..., repos=[RepoTarget(...)],
                                  skill="implement-spec", inputs=RunInputs(spec=..., tasks=[...])))
worker.status(started.run_id)   # running | cancelled | ended with one Outcome + reason
worker.cancel(started.run_id)   # closes the agent's conversation, removes the sandbox
```

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
