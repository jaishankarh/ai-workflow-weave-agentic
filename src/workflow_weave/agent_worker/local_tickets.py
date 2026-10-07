"""The run inputs as local tickets, with a local-files tracker (ADR 0009).

The spec and each Task are written on the Sandbox host and mounted read-only
into the sandbox (`ORIGINALS_DIR`), so the agent can read but never change
them, not even as root. Beside them is `TRACKER_DOC`, the issue-tracker
description upstream skills read (what `setup-matt-pocock-skills` would write
to a Repo's `docs/agents/issue-tracker.md`, modelled on its local-markdown
template). The run's prompt names it, so nothing is written into a working
copy. The agent works on a writable copy of the tickets (`TRACKER_DIR`);
"closing" a ticket sets its `Status:` line there, and the worker reads those
lines back when the run ends.
"""

from __future__ import annotations

import re
from pathlib import Path

from .model import RunInputs

ORIGINALS_DIR = "/weave/tickets"
TRACKER_DIR = "/weave/tracker"
TRACKER_DOC = f"{ORIGINALS_DIR}/issue-tracker.md"
SPEC_ID = "spec"
OPEN = "open"
DONE_STATES = frozenset({"done", "closed", "resolved"})

_TRACKER_DOC = f"""# Issue tracker: local files

The spec and its tickets for this run live as markdown files. This run has no
access to any hosted issue tracker or code host: do not try to use one.

Tickets live in `{TRACKER_DIR}/`. That folder is yours to update.
The read-only originals are in `{ORIGINALS_DIR}/`; never try to change them.

## Conventions

- The spec is `{TRACKER_DIR}/spec.md`.
- Each ticket is one file, `{TRACKER_DIR}/issues/<NN>-<slug>.md`, numbered from `01` in order.
- Each file has a `Status:` line at the top: `{OPEN}` or `done`.
- Comments go at the bottom of the file under a `## Comments` heading.

## When a skill says "fetch the relevant ticket"

Read the file at `{TRACKER_DIR}/issues/<NN>-<slug>.md` (or `spec.md` for the spec).

## When a skill says "close" or "resolve" a ticket (or the spec)

Set its `Status:` line to `done` and save the file. That is the only way work is closed here;
there are no pull requests to open. The run's only other output is commits pushed to its
Integration branch(es).

## When a skill says "publish to the issue tracker"

Create a new file under `{TRACKER_DIR}/issues/`, numbered after the last one.
"""


def _slug(text: str) -> str:
    first = next((ln for ln in text.splitlines() if ln.strip()), "task")
    slug = re.sub(r"[^a-z0-9]+", "-", first.lower().lstrip("# ")).strip("-")
    return slug[:48].strip("-") or "task"


def ticket_files(inputs: RunInputs) -> dict[str, str]:
    """Relative path -> content of every ticket file, the tracker description included."""
    files = {
        "issue-tracker.md": _TRACKER_DOC,
        "spec.md": f"Status: {OPEN}\n\n{inputs.spec.strip()}\n",
    }
    for n, task in enumerate(inputs.tasks, start=1):
        files[f"issues/{n:02d}-{_slug(task)}.md"] = f"Status: {OPEN}\n\n{task.strip()}\n"
    return files


def write_originals(inputs: RunInputs, dest: Path) -> Path:
    """Write the originals on the Sandbox host, to be mounted read-only at `ORIGINALS_DIR`."""
    for rel, text in ticket_files(inputs).items():
        f = dest / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)
    (dest / "issues").mkdir(parents=True, exist_ok=True)
    return dest


# Run in the sandbox once the originals are mounted: the agent's writable copy.
MAKE_TRACKER = f"mkdir -p {TRACKER_DIR} && cp -r {ORIGINALS_DIR}/spec.md {ORIGINALS_DIR}/issues {TRACKER_DIR}/ && chmod -R u+w {TRACKER_DIR}"
# Run in the sandbox when the run ends: one `<path>:Status: <state>` line per ticket.
READ_STATUSES = f"cd {TRACKER_DIR} && grep -H -m1 -i '^Status:' spec.md issues/*.md 2>/dev/null; true"


def done_tickets(status_lines: str) -> list[str]:
    """Ticket ids (`spec`, `01`, `02`, ...) whose `Status:` the agent set to done."""
    done = []
    for line in status_lines.splitlines():
        path, _, status = line.partition(":Status:")
        if not status:
            path, _, status = line.partition(":status:")
        if status.strip().lower() not in DONE_STATES:
            continue
        name = Path(path.strip()).name
        done.append(SPEC_ID if name == "spec.md" else name.split("-", 1)[0])
    return sorted(done, key=lambda t: (t != SPEC_ID, t))


def prompt_pointer() -> str:
    """What the run's prompt says so skills find the tracker."""
    return (
        f"The issue tracker is described in `{TRACKER_DOC}` (use it wherever a skill refers to "
        f"`docs/agents/issue-tracker.md`). The spec and Tasks below are tickets in `{TRACKER_DIR}/`."
    )
