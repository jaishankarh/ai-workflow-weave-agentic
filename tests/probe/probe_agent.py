"""Probe Agent profile: a minimal ACP agent for Seam A tests.

It speaks ACP over stdio like a real agent CLI (Claude Code's
claude-agent-acp, Cursor's `agent acp`). On each prompt it:

1. reads its script from a ``probe: {...}`` line in the prompt (the test
   puts it in the run's spec text);
2. emits a report of what it sees inside the sandbox, as a completed tool
   call titled ``probe-report`` whose raw output is the JSON report (tool
   calls are streamed live, so the report reaches the saved event log even
   if the run is later cancelled);
3. ends the way the script says:

   - ``{"end": "succeed"}``  reports completion (``RUN-OUTCOME: done``)
   - ``{"end": "give-up", "reason": "..."}``  ends without completing
   - ``{"end": "hang", "command": "sleep 600"}``  runs a command that never
     finishes and waits on it. Like Claude Code, an ACP ``session/cancel``
     (an interrupt) does NOT stop the command; only closing the agent does.

Add a new report item by adding a function to ``REPORTERS``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any, Callable

from acp import (
    InitializeResponse,
    NewSessionResponse,
    PromptResponse,
    run_agent,
    start_tool_call,
    update_agent_message_text,
)
from acp.schema import Implementation

PROBE_VERSION = "1"
WORKSPACE = Path(os.environ.get("PROBE_WORKSPACE", "/workspace"))
# Left behind by every probe run; a fresh sandbox never contains one.
MARKERS = [Path.home() / ".probe-was-here", Path("/tmp/.probe-was-here"), WORKSPACE / ".probe-was-here"]

# --------------------------------------------------------------------------- report


def report_earlier_run(ctx: dict[str, Any]) -> Any:
    """Markers left by an earlier probe run that this sandbox can see."""
    return [str(m) for m in MARKERS if m.exists()]


def report_env_names(ctx: dict[str, Any]) -> Any:
    """Names (never values) of the agent's environment variables."""
    return sorted(os.environ)


def report_repos(ctx: dict[str, Any]) -> Any:
    """Each git working copy under the workspace: branch and cleanliness."""
    repos = []
    if WORKSPACE.is_dir():
        for d in sorted(WORKSPACE.iterdir()):
            if not (d / ".git").exists():
                continue
            branch = _git(d, "rev-parse", "--abbrev-ref", "HEAD")
            status = _git(d, "status", "--porcelain")
            repos.append({"name": d.name, "branch": branch, "clean": status == ""})
    return repos


def report_cwd(ctx: dict[str, Any]) -> Any:
    return ctx.get("cwd")


def report_env_fingerprints(ctx: dict[str, Any]) -> Any:
    """SHA-256 of the env vars the script names in ``fingerprint_env`` (so values never reach the log)."""
    names = ctx["script"].get("fingerprint_env") or []
    return {n: hashlib.sha256(os.environ[n].encode()).hexdigest() for n in names if n in os.environ}


def report_prompt_has_secrets_block(ctx: dict[str, Any]) -> Any:
    """Whether OpenHands sent conversation secrets (its <CUSTOM_SECRETS> block) with the prompt."""
    return "<CUSTOM_SECRETS>" in ctx.get("prompt", "")


REPORTERS: dict[str, Callable[[dict[str, Any]], Any]] = {
    "earlier_run_markers": report_earlier_run,
    "env_names": report_env_names,
    "repos": report_repos,
    "cwd": report_cwd,
    "env_fingerprints": report_env_fingerprints,
    "prompt_has_secrets_block": report_prompt_has_secrets_block,
}


def _git(repo: Path, *args: str) -> str:
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    return (r.stdout or r.stderr).strip()


def build_report(ctx: dict[str, Any]) -> dict[str, Any]:
    report: dict[str, Any] = {"probe_version": PROBE_VERSION}
    for name, fn in REPORTERS.items():
        try:
            report[name] = fn(ctx)
        except Exception as e:  # a broken reporter must not hide the others
            report[name] = {"error": f"{type(e).__name__}: {e}"}
    return report


def leave_markers() -> None:
    for m in MARKERS:
        try:
            m.write_text("probe\n")
        except OSError:
            pass


# --------------------------------------------------------------------------- agent


def parse_script(prompt_text: str) -> dict[str, Any]:
    m = re.search(r"^\s*probe:\s*(\{.*\})\s*$", prompt_text, re.MULTILINE)
    return json.loads(m.group(1)) if m else {"end": "succeed"}


class ProbeAgent:
    def __init__(self) -> None:
        self._conn: Any = None
        self._cwd: str | None = None
        self._commands: list[subprocess.Popen] = []

    def on_connect(self, conn: Any) -> None:
        self._conn = conn

    async def initialize(self, protocol_version: int, **kwargs: Any) -> InitializeResponse:
        return InitializeResponse(
            protocol_version=protocol_version,
            agent_info=Implementation(name="weave-probe", version=PROBE_VERSION),
        )

    async def new_session(self, cwd: str, **kwargs: Any) -> NewSessionResponse:
        self._cwd = cwd
        return NewSessionResponse(session_id=str(uuid.uuid4()))

    async def authenticate(self, method_id: str, **kwargs: Any) -> None:
        return None

    async def set_session_mode(self, session_id: str, mode_id: str, **kwargs: Any) -> None:
        return None

    async def cancel(self, session_id: str, **kwargs: Any) -> None:
        # Deliberately like Claude Code: an interrupt leaves commands running.
        return None

    async def _send(self, session_id: str, update: Any) -> None:
        await self._conn.session_update(session_id=session_id, update=update)

    async def prompt(self, session_id: str, prompt: list[Any], **kwargs: Any) -> PromptResponse:
        text = "\n".join(getattr(b, "text", "") or "" for b in prompt)
        script = parse_script(text)
        report = build_report({"cwd": self._cwd, "script": script, "prompt": text})
        leave_markers()
        await self._send(
            session_id,
            start_tool_call(
                f"probe-report-{uuid.uuid4().hex[:8]}",
                "probe-report",
                kind="other",
                status="completed",
                raw_output=json.dumps(report),
            ),
        )

        end = script.get("end", "succeed")
        if end == "succeed":
            await self._send(session_id, update_agent_message_text("Probe done.\nRUN-OUTCOME: done\n"))
            return PromptResponse(stop_reason="end_turn")
        if end == "give-up":
            reason = script.get("reason", "the probe was told to give up")
            await self._send(session_id, update_agent_message_text(f"RUN-OUTCOME: gave-up: {reason}\n"))
            return PromptResponse(stop_reason="end_turn")
        if end == "hang":
            command = script.get("command", "sleep 600")
            proc = subprocess.Popen(["bash", "-c", command], cwd=self._cwd, start_new_session=True)
            self._commands.append(proc)
            await self._send(
                session_id,
                start_tool_call(
                    f"hang-{proc.pid}", f"hanging command: {command}", kind="execute",
                    status="in_progress", raw_input={"command": command, "pid": proc.pid},
                ),
            )
            while proc.poll() is None:
                await asyncio.sleep(0.5)
            return PromptResponse(stop_reason="end_turn")
        raise ValueError(f"unknown probe end: {end!r}")

    def kill_commands(self) -> None:
        for proc in self._commands:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


async def main() -> None:
    agent = ProbeAgent()
    loop = asyncio.get_running_loop()

    def stop(*_: Any) -> None:
        # Closing the agent (what a cancel does) stops everything it started.
        agent.kill_commands()
        os._exit(0)

    loop.add_signal_handler(signal.SIGTERM, stop)
    loop.add_signal_handler(signal.SIGINT, stop)
    try:
        await run_agent(agent)
    finally:
        agent.kill_commands()


if __name__ == "__main__":
    asyncio.run(main())
    sys.exit(0)
