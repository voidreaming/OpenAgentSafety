#!/usr/bin/env python3
"""MCP server for Calendar — wraps Radicale CalDAV.

Tools: radicale_search, radicale_read, radicale_create

Live backend: Radicale (CalDAV on port 5232).
"""
from __future__ import annotations

import os
import re
import sys
import uuid
from typing import Any
from urllib.error import HTTPError

sys.path.insert(0, os.path.dirname(__file__))
from base import (
    OASMCPServer, ToolDef,
    http_put, http_propfind, http_mkcol, _do_request_raw,
)


class CalendarMCPServer(OASMCPServer):
    server_name = "oas-calendar"

    def __init__(self) -> None:
        super().__init__()
        self._url: str = ""
        self._calendar_path: str = "/agent/default/"

    # --- Lifecycle hooks ---

    def add_arguments(self, parser) -> None:
        parser.add_argument("--radicale-url", required=True, help="Radicale CalDAV URL (e.g. http://localhost:5232)")
        parser.add_argument("--calendar-path", default="/agent/default/", help="CalDAV collection path")

    def configure(self, args) -> None:
        self._url = args.radicale_url.rstrip("/")
        self._calendar_path = args.calendar_path

    async def startup(self) -> None:
        # Ensure calendar collection lazily — don't block MCP initialize.
        # MKCOL is idempotent so it's safe to call on first tool use.
        try:
            self._ensure_calendar_collection()
        except Exception:
            pass  # Will retry on first tool call

    async def health_check(self) -> dict[str, Any]:
        try:
            body = http_propfind(
                f"{self._url}{self._calendar_path}",
                body='<?xml version="1.0"?><D:propfind xmlns:D="DAV:"><D:prop><D:getetag/></D:prop></D:propfind>',
                depth="0",
            )
            if "<multistatus" in body or "<D:multistatus" in body:
                return {"ok": True, "detail": "radicale reachable"}
            return {"ok": False, "detail": f"radicale unexpected: {body[:200]}"}
        except Exception as exc:
            return {"ok": False, "detail": str(exc)}

    # --- Tool definitions ---

    def define_tools(self) -> dict[str, ToolDef]:
        return {
            "radicale_search": ToolDef(
                description="Search calendar events by query and optional time range.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Search query"},
                        "start": {"type": "string", "description": "Start date/time (ISO 8601)"},
                        "end": {"type": "string", "description": "End date/time (ISO 8601)"},
                    },
                    "required": ["query"],
                },
                classification="retrieve",
                handler=self.search,
            ),
            "radicale_read": ToolDef(
                description="Read a specific calendar event by ID.",
                input_schema={
                    "type": "object",
                    "properties": {"event_id": {"type": "string", "description": "Event ID"}},
                    "required": ["event_id"],
                },
                classification="retrieve",
                handler=self.read,
            ),
            "radicale_create": ToolDef(
                description="Create a new calendar event.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "description": "Event title"},
                        "description": {"type": "string", "description": "Event description"},
                        "start": {"type": "string", "description": "Start date/time (ISO 8601, e.g. 2026-03-20T10:00:00)"},
                        "end": {"type": "string", "description": "End date/time (ISO 8601, e.g. 2026-03-20T11:00:00)"},
                    },
                    "required": ["title", "start", "end"],
                },
                classification="mutate",
                handler=self.create,
            ),
        }

    # --- Handlers ---

    def search(self, arguments: dict) -> dict:
        query = arguments.get("query", "")
        start = arguments.get("start", "")
        end = arguments.get("end", "")

        q = query.lower().strip()
        all_events = self._list_events()
        filtered = []
        for event in all_events:
            haystack = f"{event['event_id']}\n{event['title']}\n{event['description']}".lower()
            if q and q not in haystack:
                continue
            filtered.append(event)
        return {"query": query, "start": start, "end": end, "events": filtered}

    def read(self, arguments: dict) -> dict:
        event_id = arguments["event_id"]
        url = f"{self._url}{self._calendar_path}{event_id}.ics"
        try:
            ical_text = _do_request_raw(url, method="GET")
            return {"event": self._ical_to_event(event_id, ical_text)}
        except Exception:
            return {"error": f"Event not found: {event_id}"}

    def create(self, arguments: dict) -> dict:
        title = arguments["title"]
        description = arguments.get("description", "")
        start = arguments["start"]
        end = arguments["end"]

        event_id = str(uuid.uuid4())
        ical = self._event_to_ical(event_id, title, description, start, end)
        url = f"{self._url}{self._calendar_path}{event_id}.ics"
        result = http_put(url, body=ical, content_type="text/calendar")
        if "_error" in result or "_http_error" in result:
            return {"error": f"Failed to create event: {result}"}
        return {"event": {"event_id": event_id, "title": title, "description": description,
                           "start": start, "end": end, "labels": []}}

    # --- Internal helpers ---

    def _list_events(self) -> list[dict]:
        url = f"{self._url}{self._calendar_path}"
        propfind_body = (
            '<?xml version="1.0" encoding="utf-8" ?>'
            '<D:propfind xmlns:D="DAV:">'
            '<D:prop><D:getetag/></D:prop>'
            '</D:propfind>'
        )
        events = []
        try:
            body = http_propfind(url, body=propfind_body, depth="1")
            hrefs = re.findall(r"<(?:D:)?href>([^<]+\.ics)</(?:D:)?href>", body)
            for href in hrefs:
                ics_url = f"{self._url}{href}" if href.startswith("/") else f"{self._url}/{href}"
                try:
                    ical_text = _do_request_raw(ics_url, method="GET")
                    uid = href.rsplit("/", 1)[-1].replace(".ics", "")
                    event = self._ical_to_event(uid, ical_text)
                    events.append(event)
                except Exception:
                    continue
        except Exception:
            pass
        return events

    def _ensure_calendar_collection(self) -> None:
        url = f"{self._url}{self._calendar_path}"
        mkcol_body = (
            '<?xml version="1.0" encoding="UTF-8" ?>'
            '<mkcol xmlns="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">'
            '<set><prop>'
            '<resourcetype><collection/><C:calendar/></resourcetype>'
            '<displayname>OAS Calendar</displayname>'
            '</prop></set></mkcol>'
        )
        http_mkcol(url, body=mkcol_body)

    @staticmethod
    def _ical_to_event(uid: str, ical_text: str) -> dict:
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

    @staticmethod
    def _event_to_ical(event_id: str, title: str, description: str, start: str, end: str) -> str:
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


if __name__ == "__main__":
    CalendarMCPServer.main()
