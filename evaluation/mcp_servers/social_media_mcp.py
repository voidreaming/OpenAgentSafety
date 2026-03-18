#!/usr/bin/env python3
"""MCP server for Social Media — wraps Pleroma/Akkoma Mastodon-compatible API.

Tools: list_threads, read_thread, post, send_message

Live backend: Pleroma/Akkoma (Mastodon-compatible REST API on port 4000).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from base import utc_now_iso, http_get, http_post

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

_pleroma_url: str = ""       # e.g. http://the-agent-company.com:4000
_pleroma_token: str = ""     # OAuth bearer token

server = Server("oas-social-media")


def _api_get(path: str, params: dict | None = None) -> dict:
    headers = {}
    if _pleroma_token:
        headers["Authorization"] = f"Bearer {_pleroma_token}"
    return http_get(f"{_pleroma_url}{path}", headers=headers, params=params)


def _api_post(path: str, body: dict | None = None) -> dict:
    headers = {}
    if _pleroma_token:
        headers["Authorization"] = f"Bearer {_pleroma_token}"
    return http_post(f"{_pleroma_url}{path}", body=body, headers=headers)


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="list_threads",
            description="List social media threads, optionally filtered by query.",
            inputSchema={
                "type": "object",
                "properties": {"query": {"type": "string", "description": "Optional search query"}},
            },
        ),
        Tool(
            name="read_thread",
            description="Read a specific social media thread by ID.",
            inputSchema={
                "type": "object",
                "properties": {"thread_id": {"type": "string", "description": "Thread ID (status ID)"}},
                "required": ["thread_id"],
            },
        ),
        Tool(
            name="post",
            description="Create a social media post.",
            inputSchema={
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "Post content"},
                    "visibility": {"type": "string", "description": "Visibility: public, unlisted, private, or direct"},
                },
                "required": ["content"],
            },
        ),
        Tool(
            name="send_message",
            description="Send a direct message via social media.",
            inputSchema={
                "type": "object",
                "properties": {
                    "to": {"type": "string", "description": "Recipient username or account ID"},
                    "body": {"type": "string", "description": "Message body"},
                },
                "required": ["to", "body"],
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    if name == "list_threads":
        result = _list_threads(arguments.get("query", ""))
    elif name == "read_thread":
        result = _read_thread(arguments["thread_id"])
    elif name == "post":
        result = _post(arguments["content"], arguments.get("visibility", "public"))
    elif name == "send_message":
        result = _send_message(arguments["to"], arguments["body"])
    else:
        result = {"error": f"Unknown tool: {name}"}
    return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]


def _status_to_thread(status: dict) -> dict:
    """Convert a Mastodon API status to our thread format."""
    return {
        "thread_id": str(status.get("id", "")),
        "title": "",
        "content": status.get("content", ""),
        "author": status.get("account", {}).get("username", ""),
        "visibility": status.get("visibility", ""),
        "timestamp": status.get("created_at", ""),
    }


def _list_threads(query: str = "") -> dict:
    """List statuses from the home timeline, optionally searching."""
    if query:
        data = _api_get("/api/v2/search", params={"q": query, "type": "statuses", "limit": "40"})
        statuses = data.get("statuses", [])
    else:
        data = _api_get("/api/v1/timelines/home", params={"limit": "40"})
        statuses = data if isinstance(data, list) else []
    threads = [_status_to_thread(s) for s in statuses]
    return {"query": query, "threads": threads}


def _read_thread(thread_id: str) -> dict:
    """Read a status and its context (thread) by ID."""
    status = _api_get(f"/api/v1/statuses/{thread_id}")
    if "_error" in status or "_http_error" in status:
        return {"error": f"Thread not found: {thread_id}"}
    context = _api_get(f"/api/v1/statuses/{thread_id}/context")
    ancestors = context.get("ancestors", [])
    descendants = context.get("descendants", [])
    thread = {
        "thread_id": thread_id,
        "status": _status_to_thread(status),
        "ancestors": [_status_to_thread(s) for s in ancestors],
        "descendants": [_status_to_thread(s) for s in descendants],
    }
    return {"thread": thread}


def _post(content: str, visibility: str = "public") -> dict:
    """Create a new status (post) via Mastodon API."""
    vis_map = {"private": "private", "public": "public",
               "unlisted": "unlisted", "direct": "direct"}
    masto_vis = vis_map.get(visibility, "public")
    result = _api_post("/api/v1/statuses", body={
        "status": content,
        "visibility": masto_vis,
    })
    if "_error" in result or "_http_error" in result:
        return {"ok": False, "error": f"Post failed: {result}"}
    return {"post": {
        "post_id": str(result.get("id", "")),
        "content": content,
        "visibility": masto_vis,
        "timestamp": result.get("created_at", utc_now_iso()),
    }}


def _send_message(to: str, body: str) -> dict:
    """Send a direct message by posting with visibility=direct and @mention."""
    mention = to if to.startswith("@") else f"@{to}"
    content = f"{mention} {body}"
    result = _api_post("/api/v1/statuses", body={
        "status": content,
        "visibility": "direct",
    })
    if "_error" in result or "_http_error" in result:
        return {"ok": False, "error": f"DM failed: {result}"}
    return {"message": {
        "message_id": str(result.get("id", "")),
        "to": to,
        "body": body,
        "timestamp": result.get("created_at", utc_now_iso()),
    }}


async def main() -> None:
    global _pleroma_url, _pleroma_token
    parser = argparse.ArgumentParser(description="Social Media MCP server")
    parser.add_argument("--pleroma-url", required=True, help="Pleroma/Akkoma API URL (e.g. http://localhost:4000)")
    parser.add_argument("--pleroma-token", default="", help="OAuth bearer token for Pleroma")
    args = parser.parse_args()

    _pleroma_url = args.pleroma_url.rstrip("/")
    _pleroma_token = args.pleroma_token

    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream)


if __name__ == "__main__":
    asyncio.run(main())
