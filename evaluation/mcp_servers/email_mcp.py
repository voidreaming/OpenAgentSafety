#!/usr/bin/env python3
"""MCP server for Email — wraps Mailpit REST API + SMTP.

Tools: mailpit_search, mailpit_read, mailpit_send

Live backend: Mailpit (SMTP ingest on port 1025, REST API on port 8025).
"""
from __future__ import annotations

import os
import smtplib
import sys
from email.mime.text import MIMEText
from typing import Any
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(__file__))
from base import (
    OASMCPServer, ToolDef,
    normalize_email, utc_now_iso, sanitize_header_value,
    http_get,
)


class EmailMCPServer(OASMCPServer):
    server_name = "oas-email"

    def __init__(self) -> None:
        super().__init__()
        self._mailpit_url: str = ""
        self._smtp_host: str = ""
        self._smtp_port: int = 1025
        self._from_address: str = ""

    # --- Lifecycle hooks ---

    def add_arguments(self, parser) -> None:
        parser.add_argument("--mailpit-url", required=True, help="Mailpit REST API URL (e.g. http://localhost:8025)")
        parser.add_argument("--smtp-host", default="", help="SMTP host for sending (defaults to mailpit-url host)")
        parser.add_argument("--smtp-port", type=int, default=1025, help="SMTP port")
        # Default from-address: try to read from oas_platform.toml, fallback to hardcoded.
        default_from = "agent@the-agent-company.com"
        try:
            _cfg_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            sys.path.insert(0, _cfg_dir)
            import platform_config as _pcfg
            default_from = f"agent@{_pcfg.email_domain()}"
        except Exception:
            pass
        parser.add_argument("--from-address", default=default_from, help="Sender email address")

    def configure(self, args) -> None:
        self._mailpit_url = args.mailpit_url.rstrip("/")
        self._from_address = args.from_address
        parsed = urlparse(self._mailpit_url)
        self._smtp_host = args.smtp_host or parsed.hostname or "localhost"
        self._smtp_port = args.smtp_port

    async def health_check(self) -> dict[str, Any]:
        try:
            result = http_get(f"{self._mailpit_url}/api/v1/messages", params={"limit": "1"})
            if "_error" not in result and "_http_error" not in result:
                return {"ok": True, "detail": "mailpit reachable"}
            return {"ok": False, "detail": f"mailpit error: {result}"}
        except Exception as exc:
            return {"ok": False, "detail": str(exc)}

    # --- Tool definitions ---

    def define_tools(self) -> dict[str, ToolDef]:
        return {
            "mailpit_search": ToolDef(
                description="Search email threads by text query.",
                input_schema={
                    "type": "object",
                    "properties": {"query": {"type": "string", "description": "Search query"}},
                    "required": ["query"],
                },
                classification="retrieve",
                handler=self.search,
            ),
            "mailpit_read": ToolDef(
                description="Read a specific email thread by ID.",
                input_schema={
                    "type": "object",
                    "properties": {"thread_id": {"type": "string", "description": "Thread ID"}},
                    "required": ["thread_id"],
                },
                classification="retrieve",
                handler=self.read_thread,
            ),
            "mailpit_send": ToolDef(
                description="Send an email message.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "to": {"type": "array", "items": {"type": "string"}, "description": "Recipient email addresses"},
                        "subject": {"type": "string", "description": "Email subject"},
                        "body": {"type": "string", "description": "Email body"},
                        "cc": {"type": "array", "items": {"type": "string"}, "description": "CC addresses"},
                        "bcc": {"type": "array", "items": {"type": "string"}, "description": "BCC addresses"},
                    },
                    "required": ["to", "subject", "body"],
                },
                classification="send",
                handler=self.send_email,
            ),
        }

    # --- Handlers ---

    def search(self, arguments: dict) -> dict:
        query = arguments.get("query", "")
        params = {"query": query} if query else {}
        data = http_get(f"{self._mailpit_url}/api/v1/search", params=params)
        messages = data.get("messages", [])
        threads: dict[str, dict] = {}
        for msg in messages:
            mid = msg.get("ID", "")
            subject = msg.get("Subject", "")
            thread_key = subject
            if thread_key not in threads:
                threads[thread_key] = {
                    "thread_id": mid,
                    "subject": subject,
                    "participants": [],
                    "message_count": 0,
                }
            threads[thread_key]["message_count"] += 1
            for addr in msg.get("To", []):
                email = addr.get("Address", "")
                if email and email not in threads[thread_key]["participants"]:
                    threads[thread_key]["participants"].append(email)
            from_addr = msg.get("From", {}).get("Address", "")
            if from_addr and from_addr not in threads[thread_key]["participants"]:
                threads[thread_key]["participants"].append(from_addr)
        return {"query": query, "threads": list(threads.values())}

    def read_thread(self, arguments: dict) -> dict:
        thread_id = arguments["thread_id"]
        data = http_get(f"{self._mailpit_url}/api/v1/message/{thread_id}")
        if "_http_error" in data or "_error" in data:
            return {"error": f"Message not found: {thread_id}"}
        msg = {
            "message_id": data.get("ID", thread_id),
            "from": data.get("From", {}).get("Address", ""),
            "to": [a.get("Address", "") for a in data.get("To", [])],
            "cc": [a.get("Address", "") for a in data.get("Cc", [])],
            "bcc": [a.get("Address", "") for a in data.get("Bcc", [])],
            "subject": data.get("Subject", ""),
            "body": data.get("Text", ""),
            "timestamp": data.get("Date", ""),
        }
        return {"thread": {"thread_id": thread_id, "messages": [msg]}}

    def send_email(self, arguments: dict) -> dict:
        to = arguments["to"]
        subject = arguments["subject"]
        body = arguments["body"]
        cc = arguments.get("cc", [])
        bcc = arguments.get("bcc", [])

        mime_msg = MIMEText(body)
        mime_msg["Subject"] = sanitize_header_value(subject)
        mime_msg["From"] = self._from_address
        mime_msg["To"] = ", ".join(to)
        if cc:
            mime_msg["Cc"] = ", ".join(cc)
        all_recipients = to + cc + bcc
        try:
            with smtplib.SMTP(self._smtp_host, self._smtp_port, timeout=15) as smtp:
                smtp.sendmail(self._from_address, all_recipients, mime_msg.as_string())
            return {
                "ok": True,
                "message": {
                    "to": [normalize_email(x) for x in to],
                    "cc": [normalize_email(x) for x in cc],
                    "bcc": [normalize_email(x) for x in bcc],
                    "subject": subject,
                    "body": body,
                    "timestamp": utc_now_iso(),
                },
            }
        except Exception as exc:
            return {"ok": False, "error": f"SMTP send failed: {exc}"}


if __name__ == "__main__":
    EmailMCPServer.main()
