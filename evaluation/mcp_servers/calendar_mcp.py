#!/usr/bin/env python3
"""MCP server for Calendar — wraps Radicale CalDAV.

Tools: search_events, read_event, create_event

Live backend: Radicale (CalDAV on port 5232).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import uuid

sys.path.insert(0, os.path.dirname(__file__))
from base import http_put

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool
from urllib.request import Request, urlopen
from urllib.error import HTTPError

_radicale_url: str = ""  # e.g. http://the-agent-company.com:5232
_calendar_path: str = "/agent/default/"  # CalDAV collection path

server = Server("oas-calendar")


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="search_events",
            description="Search calendar events by query and optional time range.",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "start": {"type": "string", "description": "Start date/time (ISO 8601)"},
                    "end": {"type": "string", "description": "End date/time (ISO 8601)"},
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="read_event",
            description="Read a specific calendar event by ID.",
            inputSchema={
                "type": "object",
                "properties": {"event_id": {"type": "string", "description": "Event ID"}},
                "required": ["event_id"],
            },
        ),
        Tool(
            name="create_event",
            description="Create a new calendar event.",
            inputSchema={
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Event title"},
                    "description": {"type": "string", "description": "Event description"},
                    "start": {"type": "string", "description": "Start date/time (ISO 8601, e.g. 2026-03-20T10:00:00)"},
                    "end": {"type": "string", "description": "End date/time (ISO 8601, e.g. 2026-03-20T11:00:00)"},
                },
                "required": ["title", "start", "end"],
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    if name == "search_events":
        result = _search(arguments.get("query", ""), arguments.get("start", ""), arguments.get("end", ""))
    elif name == "read_event":
        result = _read(arguments["event_id"])
    elif name == "create_event":
        result = _create(
            title=arguments["title"],
            description=arguments.get("description", ""),
            start=arguments["start"],
            end=arguments["end"],
        )
    else:
        result = {"error": f"Unknown tool: {name}"}
    return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]


# ---- iCalendar helpers ----

def _ical_to_event(uid: str, ical_text: str) -> dict:
    """Parse a VEVENT from iCalendar text into our event dict format."""
    event: dict = {"event_id": uid, "title": "", "description": "", "start": "", "end": "", "labels": []}
    for line in ical_text.splitlines():
        line = line.strip()
        if line.startswith("SUMMARY:"):
            event["title"] = line[len("SUMMARY:"):]
        elif line.startswith("DESCRIPTION:"):
            event["description"] = line[len("DESCRIPTION:"):]
        elif line.startswith("DTSTART"):
            val = line.split(":", 1)[-1] if ":" in line else ""
            event["start"] = val
        elif line.startswith("DTEND"):
            val = line.split(":", 1)[-1] if ":" in line else ""
            event["end"] = val
    return event


def _event_to_ical(event_id: str, title: str, description: str, start: str, end: str) -> str:
    """Build a minimal iCalendar VEVENT."""
    def to_ical_dt(iso_str: str) -> str:
        cleaned = iso_str.replace("-", "").replace(":", "").replace(" ", "T")
        if "T" not in cleaned:
            cleaned += "T000000"
        if not cleaned.endswith("Z") and "+" not in cleaned:
            cleaned += "Z"
        return cleaned.split("+")[0].split(".")[0]

    return (
        "BEGIN:VCALENDAR\r\n"
        "VERSION:2.0\r\n"
        "PRODID:-//OAS//Calendar//EN\r\n"
        "BEGIN:VEVENT\r\n"
        f"UID:{event_id}\r\n"
        f"SUMMARY:{title}\r\n"
        f"DESCRIPTION:{description}\r\n"
        f"DTSTART:{to_ical_dt(start)}\r\n"
        f"DTEND:{to_ical_dt(end)}\r\n"
        "END:VEVENT\r\n"
        "END:VCALENDAR\r\n"
    )


# ---- Radicale CalDAV operations ----

def _list_events() -> list[dict]:
    """List all events from Radicale via PROPFIND."""
    url = f"{_radicale_url}{_calendar_path}"
    propfind_body = (
        '<?xml version="1.0" encoding="utf-8" ?>'
        '<D:propfind xmlns:D="DAV:">'
        '<D:prop><D:getetag/></D:prop>'
        '</D:propfind>'
    )
    req = Request(
        url, data=propfind_body.encode(),
        headers={"Content-Type": "application/xml", "Depth": "1"},
        method="PROPFIND",
    )
    events = []
    try:
        with urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8", errors="replace")
        hrefs = re.findall(r"<(?:D:)?href>([^<]+\.ics)</(?:D:)?href>", body)
        for href in hrefs:
            ics_url = f"{_radicale_url}{href}" if href.startswith("/") else f"{_radicale_url}/{href}"
            try:
                ics_req = Request(ics_url, method="GET")
                with urlopen(ics_req, timeout=15) as ics_resp:
                    ical_text = ics_resp.read().decode("utf-8", errors="replace")
                uid = href.rsplit("/", 1)[-1].replace(".ics", "")
                event = _ical_to_event(uid, ical_text)
                events.append(event)
            except Exception:
                continue
    except Exception:
        pass
    return events


def _search(query: str, start: str = "", end: str = "") -> dict:
    q = query.lower().strip()
    all_events = _list_events()
    filtered = []
    for event in all_events:
        haystack = f"{event['event_id']}\n{event['title']}\n{event['description']}".lower()
        if q and q not in haystack:
            continue
        filtered.append(event)
    return {"query": query, "start": start, "end": end, "events": filtered}


def _read(event_id: str) -> dict:
    url = f"{_radicale_url}{_calendar_path}{event_id}.ics"
    try:
        req = Request(url, method="GET")
        with urlopen(req, timeout=15) as resp:
            ical_text = resp.read().decode("utf-8", errors="replace")
        return {"event": _ical_to_event(event_id, ical_text)}
    except Exception:
        return {"error": f"Event not found: {event_id}"}


def _create(title: str, description: str, start: str, end: str) -> dict:
    event_id = str(uuid.uuid4())
    ical = _event_to_ical(event_id, title, description, start, end)
    url = f"{_radicale_url}{_calendar_path}{event_id}.ics"
    result = http_put(url, body=ical, content_type="text/calendar")
    if "_error" in result or "_http_error" in result:
        return {"error": f"Failed to create event: {result}"}
    return {"event": {"event_id": event_id, "title": title, "description": description,
                       "start": start, "end": end, "labels": []}}


def _ensure_calendar_collection() -> None:
    """Create the CalDAV collection if it doesn't exist."""
    url = f"{_radicale_url}{_calendar_path}"
    mkcol_body = (
        '<?xml version="1.0" encoding="UTF-8" ?>'
        '<mkcol xmlns="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">'
        '<set><prop>'
        '<resourcetype><collection/><C:calendar/></resourcetype>'
        '<displayname>OAS Calendar</displayname>'
        '</prop></set></mkcol>'
    )
    req = Request(
        url, data=mkcol_body.encode(),
        headers={"Content-Type": "application/xml"},
        method="MKCOL",
    )
    try:
        urlopen(req, timeout=15)
    except HTTPError:
        pass  # Already exists (405) is fine


async def main() -> None:
    global _radicale_url, _calendar_path
    parser = argparse.ArgumentParser(description="Calendar MCP server")
    parser.add_argument("--radicale-url", required=True, help="Radicale CalDAV URL (e.g. http://localhost:5232)")
    parser.add_argument("--calendar-path", default="/agent/default/", help="CalDAV collection path")
    args = parser.parse_args()

    _radicale_url = args.radicale_url.rstrip("/")
    _calendar_path = args.calendar_path

    _ensure_calendar_collection()

    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream)


if __name__ == "__main__":
    asyncio.run(main())
