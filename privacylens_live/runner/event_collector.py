"""Collect events from agent conversations and extract structured results.

The OpenHands SDK streams ``ActionEvent`` / ``ObservationEvent`` /
``MessageEvent`` / ``AgentErrorEvent`` / ``ConversationErrorEvent`` to a
callback. This module collects them and exposes typed extractors that
build the structured ``ScenarioResult`` the runner persists to disk.

The MCP observation payload is wrapped twice: an SDK ``ObservationEvent``
envelope around an MCP ``MCPToolObservation`` envelope around the actual
JSON the tool returned. ``_parse_observation`` peels both layers so the
result file shows the real tool output, not framework noise.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any


logger = logging.getLogger("event_collector")

# Send-action tool names. The SDK auto-prefixes MCP tool names with the
# server name, so both bare and prefixed forms are recognized.
SEND_ACTIONS = {
    "send_message",
    "send_email",
    "send_channel_message",
    "create_post",
    "create_page",
}

SEND_PREFIXED = {
    "mattermost_send_message",
    "mailpit_send_email",
    "rocketchat_send_channel_message",
    "gotosocial_create_post",
    "bookstack_create_page",
}


@dataclass
class ScenarioResult:
    """Structured result of running a single PrivacyLens scenario.

    The fields here are what the result JSON file holds. Raw events go
    in a sidecar file (``<name>.events.json``) so this stays readable.
    """

    name: str
    status: str = "no_action"  # "ok" | "no_action" | "error"
    final_action: dict | None = None
    expected_final_action_type: str | None = None
    tool_calls: list[dict] = field(default_factory=list)
    # Clarification turns: each entry is
    # ``{round, request, response}`` capturing a time the agent ended
    # on a text question and the runner auto-replied to push it to
    # commit. Empty for runs where the agent committed on the first
    # turn.
    clarifications: list[dict] = field(default_factory=list)
    final_message: str | None = None
    sensitive_info_items: list[str] = field(default_factory=list)
    stats: dict = field(default_factory=dict)
    error: str | None = None
    # Raw events — populated in memory, written to a sidecar file by
    # the runner, never inlined into the main result file.
    events: list[dict] = field(default_factory=list)


def _parse_observation(obs_event: dict) -> tuple[Any, bool]:
    """Pull the actual tool output out of an MCPToolObservation envelope.

    The wire shape from FastMCP is::

        {
          "kind": "ObservationEvent",
          "observation": {
            "kind": "MCPToolObservation",
            "is_error": false,
            "content": [
              {"type": "text", "text": "[Tool 'X' executed.]"},
              {"type": "text", "text": "{\"pages\": [...]}"}
            ]
          }
        }

    Returns ``(parsed_value, is_error)`` where ``parsed_value`` is the
    JSON-decoded tool output if it parses, otherwise the raw text. The
    ``[Tool 'X' executed.]`` framing line is skipped.
    """
    obs = obs_event.get("observation")
    if not isinstance(obs, dict):
        return obs, False

    is_error = bool(obs.get("is_error", False))
    content = obs.get("content")
    if not isinstance(content, list):
        return obs, is_error

    # Walk content items and grab the first text payload that isn't the
    # framing line. Fall through to the last text item if everything
    # looks like framing.
    for item in content:
        if not isinstance(item, dict) or item.get("type") != "text":
            continue
        text = item.get("text", "")
        if not text or text.startswith("[Tool '"):
            continue
        try:
            return json.loads(text), is_error
        except json.JSONDecodeError:
            return text, is_error

    for item in reversed(content):
        if isinstance(item, dict) and item.get("type") == "text":
            return item.get("text", ""), is_error

    return obs, is_error


def render_action_string(action: dict | None) -> str:
    """Format a structured final_action dict as the legacy PrivacyLens string.

    The PrivacyLens evaluator's prompts and rule-based scanner both
    expect the ``Action: X\\nAction Input: {...}`` form. This builds
    it from the structured representation so we have a single source
    of truth.
    """
    if not action:
        return ""
    return (
        f"Action: {action['tool']}\nAction Input: {json.dumps(action.get('input', {}))}"
    )


def render_trajectory_string(tool_calls: list[dict]) -> str:
    """Format a structured tool_calls list as the legacy trajectory string."""
    parts = []
    for call in tool_calls:
        obs = call["observation"]
        obs_text = json.dumps(obs) if isinstance(obs, (dict, list)) else str(obs)
        parts.append(
            f"Action: {call['tool']}\n"
            f"Action Input: {json.dumps(call['input'])}\n"
            f"Observation: {obs_text}"
        )
    return "\n\n".join(parts)


# Fields whose values contain long sensitive text (email bodies, page
# content, message text) that trigger Azure content-policy filters.
# Stripping these from observations keeps enough structure for the
# helpfulness judge while avoiding filter hits.
_SENSITIVE_FIELDS = frozenset(
    {
        "markdown",
        "body",
        "html",
        "content",
        "text",
        "message",
        "description",
        "note",
        "bio",
    }
)


def _sanitize_obs(obs: Any) -> Any:
    """Strip long text fields from an observation, keeping structure.

    The helpfulness judge needs to know *what* was found (titles, names,
    subjects, senders, IDs) but not the full body text. This avoids
    triggering Azure content-policy filters while preserving the
    structural cues needed to evaluate whether the agent's final action
    matches what it found.
    """
    if isinstance(obs, dict):
        sanitized = {}
        for k, v in obs.items():
            if k in _SENSITIVE_FIELDS and isinstance(v, str) and len(v) > 80:
                sanitized[k] = f"[{len(v)} chars]"
            elif isinstance(v, (dict, list)):
                sanitized[k] = _sanitize_obs(v)
            else:
                sanitized[k] = v
        return sanitized
    if isinstance(obs, list):
        return [_sanitize_obs(item) for item in obs]
    return obs


def render_sanitized_trajectory_string(tool_calls: list[dict]) -> str:
    """Like render_trajectory_string but with sensitive text stripped.

    Replaces long body/markdown/content fields in observations with
    ``[N chars]`` placeholders. Keeps tool names, inputs, structural
    fields (IDs, names, subjects, senders), and short values intact.
    """
    parts = []
    for call in tool_calls:
        obs = _sanitize_obs(call["observation"])
        obs_text = json.dumps(obs) if isinstance(obs, (dict, list)) else str(obs)
        parts.append(
            f"Action: {call['tool']}\n"
            f"Action Input: {json.dumps(call['input'])}\n"
            f"Observation: {obs_text}"
        )
    return "\n\n".join(parts)


def _extract_text_content(message_obj: Any) -> str:
    """Pull plain text out of an llm_message ``content`` field.

    The SDK serializes message content as either a plain string or a
    list of ``{"type": "text", "text": "..."}`` blocks. This collapses
    both shapes into a single string.
    """
    if isinstance(message_obj, str):
        return message_obj
    if not isinstance(message_obj, dict):
        return ""
    content = message_obj.get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(item.get("text", ""))
        return "".join(parts)
    return ""


class EventCollector:
    """Collects conversation events and exposes structured extractors."""

    def __init__(self) -> None:
        self.events: list[dict] = []

    def on_event(self, event: Any) -> None:
        """Callback for conversation events."""
        try:
            if hasattr(event, "model_dump"):
                self.events.append(event.model_dump())
            elif isinstance(event, dict):
                self.events.append(event)
            else:
                self.events.append({"type": type(event).__name__, "data": str(event)})
        except Exception as e:
            logger.warning(f"Failed to collect event: {e}")

    # ── Error surfacing ──

    def extract_error_detail(self) -> str | None:
        """Pull the most recent error event's content, if any.

        The agent server's WebSocket stream emits ``ConversationErrorEvent``
        when the LLM call (or anything else inside the run loop) raises.
        The default visualizer skips it, so without this helper the runner
        only sees ``"Remote conversation ended with error"`` with no body.
        """
        for event in reversed(self.events):
            kind = event.get("kind", "")
            etype = event.get("type", "")
            if "Error" in kind or "error" in etype.lower():
                for key in ("error", "message", "detail", "exception"):
                    if event.get(key):
                        return f"{kind or etype}: {event[key]}"
                return json.dumps(event)[:500]
        return None

    # ── Action argument extraction ──

    def _extract_action_args(self, event: dict) -> dict:
        """Pull the tool-call arguments out of an ActionEvent.

        Tries the SDK's current shape (``event.action.data``) first,
        then a few legacy/alternative locations.
        """
        action_obj = event.get("action")
        if isinstance(action_obj, dict):
            data = action_obj.get("data")
            if isinstance(data, dict):
                return data
        for key in ("arguments", "action_input", "tool_input"):
            value = event.get(key)
            if isinstance(value, dict):
                return value
            if isinstance(value, str):
                try:
                    parsed = json.loads(value)
                    if isinstance(parsed, dict):
                        return parsed
                except json.JSONDecodeError:
                    pass
        return {}

    def _action_call_id(self, event: dict) -> str:
        """Best-effort tool_call_id for matching ActionEvent ↔ ObservationEvent."""
        action_obj = event.get("action") or {}
        return action_obj.get("tool_call_id") or event.get("tool_call_id", "")

    def _failed_call_ids(self) -> set[str]:
        """tool_call_ids that hit AgentErrorEvent (validation/exec failures)."""
        failed: set[str] = set()
        for event in self.events:
            if event.get("kind") == "AgentErrorEvent":
                cid = event.get("tool_call_id")
                if cid:
                    failed.add(cid)
        return failed

    # ── Structured extraction ──

    def extract_tool_calls(self) -> list[dict]:
        """Build the structured tool-call timeline.

        Each entry is::

            {
              "step": 1,
              "tool": "bookstack_get_page",
              "input": {"page_id": 6},
              "observation": <parsed JSON or string>,
              "is_error": false
            }

        ActionEvents that have a matching AgentErrorEvent (the SDK's
        signal for validation/exec failures the agent then retried)
        get ``is_error: true`` and the error message in ``observation``,
        so the trace stays honest about what happened.
        """
        # Index observations and errors by tool_call_id for O(1) lookup.
        obs_by_call: dict[str, dict] = {}
        err_by_call: dict[str, dict] = {}
        for event in self.events:
            kind = event.get("kind")
            cid = event.get("tool_call_id", "")
            if kind == "ObservationEvent" and cid:
                obs_by_call[cid] = event
            elif kind == "AgentErrorEvent" and cid:
                err_by_call[cid] = event

        calls: list[dict] = []
        step = 0
        for event in self.events:
            if event.get("kind") != "ActionEvent":
                continue
            tool = event.get("tool_name", "")
            if not tool:
                continue
            step += 1
            cid = self._action_call_id(event)
            args = self._extract_action_args(event)

            if cid in err_by_call:
                err_msg = err_by_call[cid].get("error", "validation/exec error")
                calls.append(
                    {
                        "step": step,
                        "tool": tool,
                        "input": args,
                        "observation": err_msg,
                        "is_error": True,
                    }
                )
                continue

            obs_event = obs_by_call.get(cid, {})
            obs_value, obs_is_error = _parse_observation(obs_event)
            calls.append(
                {
                    "step": step,
                    "tool": tool,
                    "input": args,
                    "observation": obs_value,
                    "is_error": obs_is_error,
                }
            )
        return calls

    def extract_final_action(self) -> dict | None:
        """Find the last successful send-action call as a structured dict.

        Returns ``None`` if the agent never called a send action (e.g.
        it ended with a clarification question instead). The dict has
        ``tool``, ``input``, ``result``, ``is_error`` so the evaluator
        can read the post body / email body / etc. directly.
        """
        failed = self._failed_call_ids()
        for call in reversed(self.extract_tool_calls()):
            tool = call["tool"]
            if tool not in SEND_ACTIONS and tool not in SEND_PREFIXED:
                continue
            if call["is_error"]:
                continue
            return {
                "tool": tool,
                "input": call["input"],
                "result": call["observation"],
                "is_error": False,
            }
        # Fallback: if every send attempt failed, surface the last failed
        # one so the result file shows what the agent tried.
        for call in reversed(self.extract_tool_calls()):
            tool = call["tool"]
            if tool not in SEND_ACTIONS and tool not in SEND_PREFIXED:
                continue
            return {
                "tool": tool,
                "input": call["input"],
                "result": call["observation"],
                "is_error": True,
            }
        _ = failed  # quiet the unused-var warning when no failures matter
        return None

    def extract_final_message(self) -> str | None:
        """Return the assistant's final natural-language message, if any.

        This is what the agent says to the user after (or instead of)
        calling its last tool. Useful for diagnosing ``no_action`` runs
        where the agent ended on a clarification question, and for
        seeing the agent's final summary on a successful run.

        The SDK does NOT populate a top-level ``sender`` field on
        ``MessageEvent`` — the role lives at ``llm_message.role``
        ("user" / "assistant"). Filter on that, otherwise our own
        auto-confirmation user message will be returned as "the
        agent's final reply".
        """
        for event in reversed(self.events):
            if event.get("kind") != "MessageEvent":
                continue
            msg = event.get("llm_message", {})
            if not isinstance(msg, dict) or msg.get("role") != "assistant":
                continue
            text = _extract_text_content(msg)
            if text:
                return text.strip()
        return None

    def extract_stats(self) -> dict:
        """Aggregate counters for the run."""
        tool_calls = self.extract_tool_calls()
        send_attempts = sum(
            1
            for c in tool_calls
            if c["tool"] in SEND_ACTIONS or c["tool"] in SEND_PREFIXED
        )
        errors_recovered = sum(1 for c in tool_calls if c["is_error"])
        return {
            "tool_call_count": len(tool_calls),
            "errors_recovered": errors_recovered,
            "send_action_attempts": send_attempts,
        }
