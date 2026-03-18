#!/usr/bin/env python3
"""MCP server for Email — wraps Mailpit REST API + SMTP.

Tools: search_threads, read_thread, send_email

Live backend: Mailpit (SMTP ingest on port 1025, REST API on port 8025).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import smtplib
import sys
import os
from email.mime.text import MIMEText

sys.path.insert(0, os.path.dirname(__file__))
from base import normalize_email, utc_now_iso, http_get

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

_mailpit_url: str = ""       # e.g. http://the-agent-company.com:8025
_smtp_host: str = ""          # e.g. the-agent-company.com
_smtp_port: int = 1025
_from_address: str = "agent@the-agent-company.com"

server = Server("oas-email")


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="search_threads",
            description="Search email threads by text query.",
            inputSchema={
                "type": "object",
                "properties": {"query": {"type": "string", "description": "Search query"}},
                "required": ["query"],
            },
        ),
        Tool(
            name="read_thread",
            description="Read a specific email thread by ID.",
            inputSchema={
                "type": "object",
                "properties": {"thread_id": {"type": "string", "description": "Thread ID"}},
                "required": ["thread_id"],
            },
        ),
        Tool(
            name="send_email",
            description="Send an email message.",
            inputSchema={
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
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    if name == "search_threads":
        result = _search(arguments.get("query", ""))
    elif name == "read_thread":
        result = _read_thread(arguments["thread_id"])
    elif name == "send_email":
        result = _send_email(
            to=arguments["to"],
            subject=arguments["subject"],
            body=arguments["body"],
            cc=arguments.get("cc", []),
            bcc=arguments.get("bcc", []),
        )
    else:
        result = {"error": f"Unknown tool: {name}"}
    return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]


def _search(query: str) -> dict:
    """Search messages in Mailpit via REST API."""
    params = {"query": query} if query else {}
    data = http_get(f"{_mailpit_url}/api/v1/search", params=params)
    messages = data.get("messages", [])
    # Group by subject as thread
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


def _read_thread(thread_id: str) -> dict:
    """Read a specific message from Mailpit by ID."""
    data = http_get(f"{_mailpit_url}/api/v1/message/{thread_id}")
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


def _send_email(to: list[str], subject: str, body: str,
                cc: list[str] | None = None,
                bcc: list[str] | None = None) -> dict:
    """Send email via SMTP to Mailpit."""
    cc = cc or []
    bcc = bcc or []
    mime_msg = MIMEText(body)
    mime_msg["Subject"] = subject
    mime_msg["From"] = _from_address
    mime_msg["To"] = ", ".join(to)
    if cc:
        mime_msg["Cc"] = ", ".join(cc)
    all_recipients = to + cc + bcc
    try:
        with smtplib.SMTP(_smtp_host, _smtp_port, timeout=15) as smtp:
            smtp.sendmail(_from_address, all_recipients, mime_msg.as_string())
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


async def main() -> None:
    global _mailpit_url, _smtp_host, _smtp_port, _from_address
    parser = argparse.ArgumentParser(description="Email MCP server")
    parser.add_argument("--mailpit-url", required=True, help="Mailpit REST API URL (e.g. http://localhost:8025)")
    parser.add_argument("--smtp-host", default="", help="SMTP host for sending (defaults to mailpit-url host)")
    parser.add_argument("--smtp-port", type=int, default=1025, help="SMTP port")
    parser.add_argument("--from-address", default="agent@the-agent-company.com", help="Sender email address")
    args = parser.parse_args()

    _mailpit_url = args.mailpit_url.rstrip("/")
    _from_address = args.from_address
    # Extract host from URL for SMTP
    from urllib.parse import urlparse
    parsed = urlparse(_mailpit_url)
    _smtp_host = args.smtp_host or parsed.hostname or "localhost"
    _smtp_port = args.smtp_port

    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream)


if __name__ == "__main__":
    asyncio.run(main())
