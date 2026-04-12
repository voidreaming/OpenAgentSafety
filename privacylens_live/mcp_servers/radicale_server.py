# pyright: reportMissingImports=false, reportCallIssue=false, reportGeneralTypeIssues=false
#
# The ``caldav`` library has no type stubs, so pyright resolves every
# caldav symbol as ``object``. That trips ``reportCallIssue``
# (``DAVClient(...)`` is "not callable") and ``reportGeneralTypeIssues``
# (``caldav.Calendar`` annotations are "not a class"). The library is
# correct at runtime; suppressing these three rules at the file level
# is the project convention. ``reportMissingImports`` covers fresh
# checkouts where ``uv sync --dev`` hasn't run yet.
"""Radicale MCP server — calendar tools.

Provides event search and read via Radicale CalDAV.

Error handling
--------------
Radicale is the only server in this set that does not speak plain HTTP+JSON;
it speaks CalDAV via the ``caldav`` Python library, so the shared
``base.http_*`` helpers do not apply. Instead, every tool wraps its caldav
calls in :func:`_translate_caldav_error`, which converts:

- ``NotFoundError`` → ``HTTPToolError(status_code=404)`` (so business-logic
  callers can branch on it the same way other servers do)
- ``AuthorizationError`` → ``HTTPToolError(status_code=401)`` with a hint to
  check ``RADICALE_USER`` / ``RADICALE_PASSWORD``
- Any other ``DAVError`` → generic ``ToolError`` carrying the exception class
  and message
- Transport-level errors from underlying ``requests`` / ``urllib3`` → generic
  ``ToolError`` so the agent still sees an actionable failure rather than
  a raw stack trace

The :func:`search_events` tool keeps a deliberate fast-path → client-side
fallback: Radicale's CalDAV REPORT search is unreliable, so a search-specific
failure logs a warning and tries listing all events client-side. Auth /
connectivity failures (from ``_get_calendar``) are *not* tolerated; only the
``calendar.search()`` call itself is wrapped by the fallback.
"""

from __future__ import annotations

from typing import Annotated, Any, NoReturn

import caldav
from base import HTTPToolError, get_env, logger
from caldav.lib.error import (
    AuthorizationError,
    DAVError,
    NotFoundError,
)
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import Field


RADICALE_URL = get_env("RADICALE_URL", "http://radicale:5232")
RADICALE_USER = get_env("RADICALE_USER", "admin")
RADICALE_PASSWORD = get_env("RADICALE_PASSWORD", "admin")

mcp = FastMCP("radicale")


# ── Error translation ──


def _translate_caldav_error(operation: str, exc: BaseException) -> NoReturn:
    """Translate a caldav (or transport) exception into a ToolError. Always raises."""
    if isinstance(exc, NotFoundError):
        msg = f"{operation}: not found in Radicale: {exc}"
        logger.warning(msg)
        raise HTTPToolError(msg, status_code=404) from exc
    if isinstance(exc, AuthorizationError):
        msg = (
            f"{operation}: unauthorized — check RADICALE_USER / "
            f"RADICALE_PASSWORD ({exc})"
        )
        logger.warning(msg)
        raise HTTPToolError(msg, status_code=401) from exc
    if isinstance(exc, DAVError):
        msg = f"{operation} failed: {type(exc).__name__}: {exc}"
        logger.warning(msg)
        raise ToolError(msg) from exc
    # Transport / socket / DNS errors from the underlying requests stack.
    msg = f"{operation} failed: {type(exc).__name__}: {exc}"
    logger.warning(msg)
    raise ToolError(msg) from exc


# ── Calendar lookup ──


def _get_calendar() -> caldav.Calendar:
    """Connect to Radicale and return the default calendar.

    Calls into here may raise ``DAVError`` or transport errors; callers must
    wrap with :func:`_translate_caldav_error`.
    """
    client = caldav.DAVClient(
        url=RADICALE_URL,
        username=RADICALE_USER,
        password=RADICALE_PASSWORD,
    )
    principal = client.principal()
    calendars = principal.calendars()
    if calendars:
        return calendars[0]
    return principal.make_calendar(name="default")


def _calendar_or_raise() -> caldav.Calendar:
    """Get the default calendar; translates caldav errors to ToolError."""
    try:
        return _get_calendar()
    except Exception as exc:
        _translate_caldav_error("calendar lookup", exc)


def _parse_event(vevent: Any) -> dict[str, Any]:
    """Extract event details from a vobject vevent."""
    attendees: list[str] = []
    if hasattr(vevent, "attendee"):
        att = vevent.attendee
        if not isinstance(att, list):
            att = [att]
        attendees = [str(a).replace("mailto:", "") for a in att]

    return {
        "event_id": str(vevent.uid.value),
        "name": str(getattr(vevent, "summary", "")),
        "description": str(getattr(vevent, "description", "")),
        "start": (str(vevent.dtstart.value) if hasattr(vevent, "dtstart") else ""),
        "end": (str(vevent.dtend.value) if hasattr(vevent, "dtend") else ""),
        "location": str(getattr(vevent, "location", "")),
        "attendees": attendees,
    }


def _summary_only(event: Any) -> dict[str, Any]:
    vevent = event.vobject_instance.vevent
    return {
        "event_id": str(vevent.uid.value),
        "name": str(getattr(vevent, "summary", "")),
    }


# ── Tools ──


@mcp.tool()
async def search_events(
    query: Annotated[
        str,
        Field(
            description=(
                "Keyword to match against event titles and descriptions. "
                "Parameter name is `query` (not `keyword`)."
            )
        ),
    ],
) -> dict:
    """Search calendar events by keyword.

    The search-term parameter is named ``query`` (not ``keyword``).
    Use event_id to call get_event for details.
    """
    calendar = _calendar_or_raise()
    # Fast path: ask Radicale to search via CalDAV REPORT.
    try:
        events = calendar.search(query)
    except Exception as exc:
        # Radicale's REPORT search is unreliable; fall back to client-side
        # filtering. This branch is the *only* tolerated caldav failure in
        # this module — it always involves a follow-up call that, if it
        # fails, will surface as a hard error.
        logger.warning(
            f"search_events: fast-path search failed "
            f"({type(exc).__name__}: {exc}); "
            f"falling back to client-side filter over all events"
        )
        try:
            all_events = calendar.events()
        except Exception as exc2:
            _translate_caldav_error("search_events fallback list", exc2)
        q = query.lower()
        results: list[dict[str, Any]] = []
        for event in all_events:
            vevent = event.vobject_instance.vevent
            summary = str(getattr(vevent, "summary", ""))
            desc = str(getattr(vevent, "description", ""))
            if q in summary.lower() or q in desc.lower():
                results.append({"event_id": str(vevent.uid.value), "name": summary})
        return {"events": results}

    # Fast path succeeded.
    return {"events": [_summary_only(e) for e in events]}


@mcp.tool()
async def get_event(
    event_id: Annotated[
        str,
        Field(
            description=(
                "iCalendar UID, as returned by search_events or list_events "
                "(the `event_id` field)."
            )
        ),
    ],
) -> dict:
    """Get detailed information for a calendar event.

    Pass the event_id from search_events or list_events.

    `start` and `end` are Python datetime string representations
    (e.g. '2026-04-15 10:00:00+00:00'), not strict ISO 8601 strings.
    `attendees` is a list of email addresses with the 'mailto:' prefix
    stripped.
    """
    calendar = _calendar_or_raise()
    try:
        event = calendar.event_by_uid(event_id)
    except NotFoundError as exc:
        raise ToolError(
            f"Event '{event_id}' not found in Radicale. "
            "Use list_events or search_events to discover valid event IDs."
        ) from exc
    except Exception as exc:
        _translate_caldav_error(f"get_event({event_id})", exc)
    return _parse_event(event.vobject_instance.vevent)


@mcp.tool()
async def list_events() -> dict:
    """List all calendar events.

    Use event_id to call get_event for details.

    `start` is a Python datetime string representation
    (e.g. '2026-04-15 10:00:00+00:00'), not a strict ISO 8601 string.
    """
    calendar = _calendar_or_raise()
    try:
        all_events = calendar.events()
    except Exception as exc:
        _translate_caldav_error("list_events", exc)
    events = [
        {
            "event_id": str(e.vobject_instance.vevent.uid.value),
            "name": str(getattr(e.vobject_instance.vevent, "summary", "")),
            "start": (
                str(e.vobject_instance.vevent.dtstart.value)
                if hasattr(e.vobject_instance.vevent, "dtstart")
                else ""
            ),
        }
        for e in all_events
    ]
    return {"events": events}


if __name__ == "__main__":
    logger.info(f"Starting Radicale MCP server (CalDAV at {RADICALE_URL})")
    mcp.run(transport="streamable-http", host="0.0.0.0", port=8080)
