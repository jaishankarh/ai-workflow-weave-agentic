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
| A line of the agent's final reply is `RUN-OUTCOME: done` (the last marker line counts) | `succeeded` |
| Agent finished with `RUN-OUTCOME: gave-up: <why>` or no marker, or OpenHands stopped it as stuck with no error | `agent-gave-up` |
| Agent error `errorKind` `authentication_failed`, or ACP code -32000 (claude-agent-acp's "Authentication required", e.g. for a rejected Claude Code token) | `needs-setup` |
| Agent error `errorKind` `rate_limit` / `billing_error` | `quota-exhausted` |
| Any other agent error, a sandbox that fails to start or dies mid-run, unreachable Central skills, a Sandbox host without the `sandbox_runtime` a run needs for its Environment (checked before anything starts) | `infra-failure` |
| A Repo's Run recipe that cannot be read, or whose services do not start or become ready (before any agent starts; see "Environments") | `needs-setup` |

`ACP_PROMPT_MAX_RETRIES=0` is set in every sandbox, so a rejected credential
surfaces in seconds; infrastructure retries are the caller's. When the error
itself is bare (claude-agent-acp's `[-32000] Authentication required`), the
reason also quotes what the agent said in the failed turn (e.g. Claude Code's
`API Error: 401 OAuth access token is invalid.`).

However a run ends, its lease is released, its record saved and its push token
revoked; a teardown step that fails (closing the conversation, removing the
sandbox) is noted in the run's `run.log` and does not stop the others.

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

The pilot Product is `config/products/sri-aurobindo-works-chat.yaml`: the chat
repo on `central+repo` (its `CLAUDE.md` and `.claude/rules`), Ahdismoi on
`central`, both on Base branch `main`; its standards are
`skills/products/sri-aurobindo-works-chat/coding-standards.md`. Set each Repo's
`source` to its clone on your Sandbox host.

**A central skill wins over a Repo's own skill of the same name.** Claude Code
documents "personal over project": `~/.claude/skills/<name>` beats the project's
`.claude/skills/<name>`. But a sandbox session starts in `/workspace`, with each
Repo below it, so to Claude Code a Repo's skills are *nested* skills
(`<repo>:<name>`), and a nested skill stays available beside the user-level one
with a note telling the agent to prefer it for files under that Repo (observed
with Claude Code 2.1.287). So for every clash the worker also turns the nested
skill off in the agent's user-level settings (`skillOverrides: {"<repo>:<name>":
"off"}` in `~/.claude/settings.json`): Claude Code no longer lists it and refuses
it by name. The working copy is untouched. With a Skill override nothing is
staged or turned off, and the Repo's own skill loads.

### Claude Code Agent profile

`claude_code_profile(image=...)` is the Agent profile for Claude Code on a
subscription token (agent provider `claude-code`). Its Subscriptions carry
`CLAUDE_CODE_OAUTH_TOKEN` (from `claude setup-token`, valid one year) as their
credential env:

```yaml
subscriptions:
  claude-main:
    agent: claude-code
    cap: 2
    env: {CLAUDE_CODE_OAUTH_TOKEN: sk-ant-oat01-...}
```

`ANTHROPIC_API_KEY` and `ANTHROPIC_BASE_URL` would win over the token, so they
never reach the sandbox, even if a Subscription names them (noted in `run.log`);
the sandbox gets no environment from the Sandbox host. Claude Code runs through
claude-agent-acp in `bypassPermissions` mode, which it allows as root because the
image sets `IS_SANDBOX=1`, and OpenHands approves any permission request that is
still asked, so a run never waits for input. A rejected token ends the run as
`needs-setup` within seconds.

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
sandbox is removed, the worker reads those lines back into `tickets_done`
(`spec`, `01`, `02`, ... in Task order) on the record and the final status. See
`agent_worker/local_tickets.py`.

**Push gateway.** The sandbox's only git credential is a random per-run token in
the URL of its only remote, `origin` = `http://weave-git:<port>/<token>/<repo>.git`
(`weave-git` maps to the Docker host). The worker runs a small smart-HTTP git
server (`agent_worker/push_gateway.py`, `git http-backend`) on the Docker
bridge gateway. Per run it keeps a bare mirror of each Repo whose
`pre-receive` hook accepts only `refs/heads/<Integration branch>` (no deletes,
tags or other branches, the Base branch included), and before accepting it
pushes the commit onward to the Repo's `source` (the Sandbox host's clone) and
from there to the Code host, both with the Sandbox host's own git credentials
(the sandbox never holds them). A push is either refused or lands on the Code
host's Integration branch; the token is revoked and the mirrors deleted when the
run ends.

**The Code host remote.** A Repo's `push_remote` (Product config) names the
remote of its `source` clone that leads to the Code host; by default `origin`,
when the clone has one. Before the pre-checks, and again when the run starts,
the worker fetches the Base branch from it, so `CONTEXT.md`, the rules files and
the Repo's own skills are read from, and the working copy starts at, the Code
host's Base branch (the clone's checked-out files are not touched). A clone with
no Code host remote (a local-only Repo, as in the tests) is used as it stands,
and pushes stop at it; `run.log` says which applies to each Repo. A configured
`push_remote` that the clone lacks refuses the run as `needs-setup`.
`WorkerSettings.push_gateway` lets several workers share one gateway (default:
each worker starts its own).

**No Tracker or Code host token.** Variables such as `GH_TOKEN`,
`GITHUB_TOKEN` and `GITLAB_TOKEN` are dropped from the sandbox environment even
if a Subscription's `env` names one (noted in `run.log`); the sandbox has no
git credential helper and no `gh`/`glab` config, so an issue, comment, label or
PR write has nothing to authenticate with.

**For the real Code host (GitHub).** The design holds as is: the gateway is the
only party with a GitHub credential, and the agent never sees it. Production
needs: a Repo `source` that is a clone of the GitHub repo (its `origin`, or the
remote named by `push_remote`), and a Sandbox host credential that can push to
it (a deploy key or fine-grained token with `contents: write` only, used by the
hook's onward push from that clone); ideally a GitHub ruleset restricting
that credential to the Integration branch pattern as a second line of defence;
and, for larger Repos, a persistent mirror per Repo instead of a fresh clone
per run. The gateway must listen only where sandboxes can reach it (the bridge
gateway, not `0.0.0.0`).

### Sandboxes on sysbox

Only a run with a touched Repo that has a Run recipe (below) gets a sandbox on sysbox: such a
sandbox starts with `docker run --runtime <WorkerSettings.sandbox_runtime>` (default
`sysbox-runc`; never `--privileged`, never the host's Docker socket; ADR 0004), and the image's
entrypoint then starts a Docker engine inside it (`WEAVE_START_DOCKERD=1`), so the Repo's software
runs in containers inside the sandbox. Every other run stays on the host's default runtime with no
engine. A Sandbox host whose Docker does not list the runtime fails the run as `infra-failure`
naming it, before a sandbox is started; so does a worker whose `sandbox_runtime` is `None` when a
run needs an Environment. Teardown waits until the sandbox is gone from the Sandbox host (polling
`docker inspect`, up to 60 s) before the run is reported ended; if it is not gone, that is noted in
`run.log`. Tests that need sysbox are skipped on a host without it.

### Environments: a Repo's Run recipe

A Repo's Run recipe is one committed compose file, `.weave/compose.yaml`, kept apart from any
developer `docker-compose.yml` (which is never used: the worker names the recipe with an explicit
`-f`). The workflow's own fields sit in one top-level `x-weave:` block, which Compose ignores, so the
file also runs by hand with `docker compose -f .weave/compose.yaml up`:

```yaml
services:
  web:
    image: python:3.12-slim
    command: python -m http.server 8000
x-weave:
  readiness:            # a check for every service, run inside its container; exit 0 = ready
    web:
      command: python -c "import urllib.request as u; u.urlopen('http://localhost:8000')"
      timeout: 60       # seconds after start (default 60)
      interval: 1       # seconds between attempts (default 1)
```

`command` is a string (run with `sh -c`) or a list (run as is). A recipe may have no services. It
may also name the Repos it needs: `x-weave: {depends_on: [svc]}`. The other `x-weave` fields of
Spec 2's external MCP servers are not read yet; `secrets` is (see "Test secrets"), and so are `seed` and
`databases` (see "Environment MCP servers"). `seed`:

```yaml
x-weave:
  seed:
    service: db                          # one of the recipe's own services
    command: psql -U app -f /seed.sql    # string (sh -c) or list; timeout: seconds (default 300)
```

Every Environment starts from empty databases (tracked Seed scripts are Spec 2b, ADR 0011); schema
creation is the recipe's or the app's job. Once every Repo's services are ready, each seed runs once,
Repos in dependency order, so a Repo is seeded after the Repos it depends on and its seed command
sees their data. It runs with `docker compose -p <repo> exec` in the named service of that Repo's
own project, with only the Test secrets that recipe names (also handed to the command with `-e`),
so a Repo's seed has no credential for a sibling's databases. This is placement and credential
scoping, not a firewall: the network is open, so a recipe that hard-codes a sibling's credentials
could still reach it. A failing seed ends the run before any agent starts (`needs-setup`, naming the
Repo, service and output, secret values redacted). A Repo with no seed omits the field. The run
record lists `environment_seeds` (Repo, service, command, `seeded_at`, `seconds`).

Between staging the agent's files and starting the agent, the worker brings each such Repo's
recipe up inside the sandbox as its own Compose project (`-p <repo>`), on a shared network
`weave-env` where each service has the alias `<service>.<repo>` (and no other: services are created,
joined to the network with `docker network connect --alias`, then started, because Compose would
also give each one its bare name there, which two Repos with a `db` would share), and waits until
every service has passed its readiness check. The same names are added to the sandbox's
`/etc/hosts`, so the agent's shell resolves them too.

Several Repos share one Environment. Every touched Repo with a recipe is brought up, and so is
every Repo named in a `depends_on` (transitively), dependencies first; a `depends_on` cycle or a
dependency without a recipe is refused (`needs-setup`). Each Repo runs from: the agent's working copy
for the Repo being worked on (`RunRequest.working_on`, default the first of `repos`), its Task branch
(Integration branch) for another Repo in `repos`, and its Base branch for a Repo the run does not
touch, so an Environment never holds another Story's unmerged work. The last two run from a
checkout under `/weave/env/src/<repo>` that the agent does not edit. A service that does not start or does not
become ready in time ends the run before any agent starts (no Subscription use), as `needs-setup`
with a reason naming the Repo, the service and its last log lines. (Telling that apart from a
Story's branches breaking the Environment, `environment-broken`, is #51.) The containers have open
internet access, and go with the sandbox: nothing is shared between runs.

The run record lists `environment_repos` (each Repo in the Environment, with `source`: working
copy, Task branch or Base branch, its `branch`, and whether the run `touched` it),
`environment_services` (Repo, service, address, `ready_at`, `seconds_to_ready`) and `environment_logs` (each service's log, saved to
`<runs_dir>/<run id>/environment/<repo>/<service>.log` before the sandbox is removed).

#### Environment MCP servers

The agent can read and change the data its code produced through MCP, without writing connection
code. A recipe names its databases, and a Product says which kinds it enables:

```yaml
# .weave/compose.yaml
x-weave:
  databases:
    - service: db                 # one of the recipe's own services
      kind: postgres              # postgres | neo4j | mysql | redis
      port: 5432                  # optional; the kind's default
      credentials:                # the throwaway database's own, so in the committed recipe (not Test secrets)
        user: {env: POSTGRES_USER}        # a value, or `env: NAME` read from the service's own `environment:`
        password: app-pw
        database: chat                    # Neo4j: optional, default `neo4j`
# config/products/<product>.yaml
database_mcp_kinds: [postgres, neo4j]     # kinds this Product enables; default none
```

Only a database a recipe names is considered (nothing is guessed from an image). Each one of an
enabled kind gets one read-write MCP server for the run, started by the agent's Claude Code from its
user-level configuration (`~/.claude.json` `mcpServers`, written by `stage_mcp_servers` in
`sandbox.py` after seeding and before the agent starts; never a working copy's `.mcp.json`), named
`<repo>-<service>` and given one host only: the database's address `<service>.<repo>` in that run's own
Environment. Servers are processes in the run's sandbox and go with it. A named database of a kind the
Product has not enabled gets no server, and neither does Redis (which is in the Environment only); the
run's `run.log` has a line for each, and the run record lists `environment_mcp_servers` (name, Repo,
service, kind, address, pinned server) and `environment_mcp_omitted` (Repo, service, kind, reason). The record
and the log never hold a credential. The Repo's own skills load as before. The built-in catalog
(`agent_worker/mcp.py`) pins:

| kind | server (PyPI) | settings it is given |
| --- | --- | --- |
| postgres | `postgres-mcp` 0.3.0 (`--access-mode=unrestricted`) | `DATABASE_URI` |
| neo4j | `mcp-neo4j-cypher` 0.6.0 (no `--read-only`) | `NEO4J_URI`, `_USERNAME`, `_PASSWORD`, `_DATABASE` |
| mysql | `mysql-mcp-server` 0.4.4 | `MYSQL_HOST`, `_PORT`, `_USER`, `_PASSWORD`, `_DATABASE` |

They are installed when the sandbox image is built, each in its own virtualenv `/opt/weave-mcp/<kind>`,
so a run needs no network to start one. `postgres-mcp` also pins `mcp[cli]==1.30.0`: its own
`mcp>=1.5` now resolves to mcp 2.x, where it fails at import.

#### Test secrets

A Product's Test secrets (credentials for test accounts and sandboxes of outside services, never
staging or production) are one file per Product on the Sandbox host, outside every repo:
`<test_secrets.location>/<product>.yaml`, a flat `NAME: value` mapping (format:
`config/test-secrets.example.yaml`). `test_secrets.location` in `weave.yaml` (default
`~/.config/weave/secrets`) sits next to `subscription_store.location`, and the worker is given the
folder as `WorkerSettings.test_secrets` (`load_configured_secret_store("weave.yaml")`). A human writes
the file; runs only read it; it never changes per ticket. It must be readable by its owner only
(`chmod 600`): a file others can read is refused. A Product's lookup opens only its own file.

A recipe names the secrets it needs by name only: `x-weave: {secrets: [KORONA_API_KEY]}`. Each named
secret reaches every service of that recipe as an environment variable of that name (Compose is told
the names in the generated override and takes the values from the environment of the `up` command, so
no file in the sandbox holds a value; the values go on both the `up --no-start` and the final `up -d`
commands of the Repo's bring-up, and on nothing else). Nothing else from the file reaches any service, and no secret
reaches the agent's sandbox environment. A throwaway database's own credentials live in the recipe,
not here.

A Repo the run does not touch but a recipe depends on gets its secrets the same way, read when its
recipe is loaded (it ends the run as `needs-setup` if the Product lacks one).

A named secret the Product lacks (or no file, or a file with the wrong mode) makes `start` return
`NeedsSetup` naming the Repo and the secret, before any lease or sandbox. The file is read again when
the run begins, and a secret that has gone since then ends the run as `needs-setup` before its
sandbox starts. The run record lists the names given per Repo (`test_secrets_given`) and `run.log`
says so; a value is never written to the record, `run.log`, the saved service logs or the event log
(values echoed by a failing service are replaced with `[redacted Test secret]`).

### Images

- `sandbox/Dockerfile`: the base sandbox image (agent-server, git, Python, and a Docker engine
  with Compose that starts only in a sandbox run on sysbox; see "Sandboxes on sysbox").
  Its base image is the build ARG `BASE_IMAGE` (default `ubuntu:24.04`). It also holds the
  catalog's pinned database MCP servers under `/opt/weave-mcp/<kind>` (build ARGs `*_MCP_VERSION`;
  see "Environment MCP servers"); a base image needs Python 3.12 or later for them.
- `sandbox/claude-code/Dockerfile`: the Claude Code Agent profile's image, on
  top of the sandbox image (build ARG `SANDBOX_IMAGE`). It pins Node.js
  22.22.0, the Claude Code CLI (`@anthropic-ai/claude-code` 2.1.287) and its ACP
  adapter (`@agentclientprotocol/claude-agent-acp` 0.86.0) as build ARGs, and
  points the adapter at that CLI (`CLAUDE_CODE_EXECUTABLE`). Node comes from the
  npm registry's `node-linux-<arch>` packages, checked against pinned integrity
  hashes, so the image builds from PyPI and npm alone. Upgrading a CLI is a
  deliberate change: bump the ARG and the expected version in
  `tests/test_claude_code_profile.py`.
- `tests/probe/Dockerfile`: the probe Agent profile used by the tests, built
  on top of the sandbox image (build ARG `SANDBOX_IMAGE`). The tests also build
  it on the Claude Code image, so the probe sees what Claude Code would.
- `tests/fake_anthropic/Dockerfile` (tests only): the Claude Code image with
  Claude Code pointed at a scripted fake Messages API inside the sandbox, so the
  real CLI runs at Seam A without a token.

The test suite builds both itself (as `weave/sandbox:test` and
`weave/probe-agent:test`). To build by hand:

```sh
docker build -t weave/sandbox:dev sandbox/
docker build --build-arg SANDBOX_IMAGE=weave/sandbox:dev -t weave/probe-agent:dev tests/probe/
docker build --build-arg SANDBOX_IMAGE=weave/sandbox:dev -t weave/claude-code:dev sandbox/claude-code/
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
| `WEAVE_CLAUDE_CODE_TOKEN` | A real `claude setup-token` token; enables the real-token Claude Code check (skipped without it) |

On a host with no image registry and a low open-files cap (such as the cloud
build host used for Spec 1), build from a local base image and lower the limit:

```sh
WEAVE_TEST_BASE_IMAGE=weave/ubuntu-base:noble WEAVE_TEST_BUILD_NETWORK=host \
WEAVE_TEST_SANDBOX_NOFILE=20000 uv run pytest
```

The image build needs PyPI and the npm registry. One test,
`test_an_invalid_token_is_needs_setup_within_seconds`, runs the real Claude Code
CLI in a sandbox against `api.anthropic.com` with a made-up token, so sandboxes
need to reach it; no real token is used. Every other Claude Code test runs the
real CLI against the scripted fake API, or the probe in the Claude Code image.

#### Checking Claude Code with a real token (outside CI)

CI never holds a Claude Code token. To check the profile end to end on your own
Subscription (one short conversation's worth of quota), create a token with
`claude setup-token` and run:

```sh
WEAVE_CLAUDE_CODE_TOKEN=sk-ant-oat01-... uv run pytest tests/test_claude_code_profile.py -k real_token
```

It starts the real Claude Code profile with a central skill and a Repo skill of
the same name, and checks that the run succeeds unattended (a shell command runs
without a prompt) and that the agent loads the central skill, not the Repo's.
