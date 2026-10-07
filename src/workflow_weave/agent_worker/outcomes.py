"""Turning how an agent's conversation ended into one outcome (ADR 0002, #13).

Classification uses the agent's own error kind, never OpenHands' exit or
error codes: claude-agent-acp fails a turn with a JSON-RPC error whose data is
`{"errorKind": "<kind>"}`, which OpenHands passes on in the conversation's
error event as `[<code>] <message>: {"errorKind": "<kind>"}`.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Mapping

from .model import Outcome

# The error kinds claude-agent-acp reports (the Claude Agent SDK's assistant
# message errors) that mean something other than a broken run. Any other kind,
# or none, is an infra-failure.
CLAUDE_AGENT_ACP_ERROR_KINDS: Mapping[str, Outcome] = {
    "authentication_failed": Outcome.NEEDS_SETUP,
    "rate_limit": Outcome.QUOTA_EXHAUSTED,
    "billing_error": Outcome.QUOTA_EXHAUSTED,
}

# ACP's own "authentication required" JSON-RPC code (claude-agent-acp sends it,
# without an error kind, when Claude Code asks to log in).
ACP_AUTH_REQUIRED = -32000

_PROBLEM = {
    Outcome.NEEDS_SETUP: "the agent's credential was rejected; renew the Subscription's credential",
    Outcome.QUOTA_EXHAUSTED: "the Subscription hit a usage limit",
    Outcome.INFRA_FAILURE: "the agent failed",
}

_ERROR_KIND = re.compile(r'"errorKind"\s*:\s*"([^"]+)"')
_CODE = re.compile(r"^\[(-?\d+)\]")

# The agent is asked to end its last reply with one of these lines (any line of it counts).
DONE_MARK = "RUN-OUTCOME: done"
GAVE_UP_MARK = "RUN-OUTCOME: gave-up"


def error_kind(detail: str) -> str | None:
    m = _ERROR_KIND.search(detail)
    return m.group(1) if m else None


def classify_error(detail: str, error_kinds: Mapping[str, Outcome]) -> tuple[Outcome, str]:
    """The outcome and reason for a conversation that ended with an agent error."""
    kind = error_kind(detail)
    if kind is not None:
        outcome = error_kinds.get(kind, Outcome.INFRA_FAILURE)
    else:
        m = _CODE.match(detail)
        outcome = Outcome.NEEDS_SETUP if m and int(m.group(1)) == ACP_AUTH_REQUIRED else Outcome.INFRA_FAILURE
    return outcome, f"{_PROBLEM[outcome]}: {detail}"


def classify_final_reply(reply: str) -> tuple[Outcome, str | None]:
    """The outcome of a conversation that finished: did the agent report its skill complete?

    The marker may be on any line of the final reply (surrounding spaces ignored); if
    there are several, the last one counts.
    """
    marks = [
        line for line in (raw.strip() for raw in reply.splitlines())
        if line == DONE_MARK or line.startswith(GAVE_UP_MARK)
    ]
    last = marks[-1] if marks else ""
    if last == DONE_MARK:
        return Outcome.SUCCEEDED, None
    if last:
        why = last[len(GAVE_UP_MARK):].lstrip(": ").strip()
        return Outcome.AGENT_GAVE_UP, f"agent gave up: {why or 'no reason given'}"
    return Outcome.AGENT_GAVE_UP, "agent gave up: it ended without reporting the skill complete"


def with_agent_words(detail: str, events: Iterable[Any]) -> str:
    """The error detail, plus what the agent said in the failed turn when the detail lacks it.

    claude-agent-acp reports a rejected credential as ACP's bare "Authentication
    required" (-32000) and streams Claude Code's own error (e.g. "API Error: 401 OAuth
    access token is invalid.") as the agent's reply, so the reason needs both.
    """
    said: list[str] = []
    for ev in events:
        kind = type(ev).__name__
        if kind == "MessageEvent" and getattr(ev, "source", None) == "user":
            said = []
        elif kind == "StreamingDeltaEvent" and getattr(ev, "source", None) == "agent":
            said.append(getattr(ev, "content", None) or "")
    words = "".join(said).strip()
    if not words or words in detail:
        return detail
    return f"{detail} (agent said: {words[:500]})"


def last_error_detail(events: Iterable[Any]) -> str | None:
    """The detail of the conversation's last error event, if any."""
    detail = None
    for ev in events:
        if type(ev).__name__ == "ConversationErrorEvent":
            detail = getattr(ev, "detail", None) or getattr(ev, "code", None) or "unknown error"
    return detail
