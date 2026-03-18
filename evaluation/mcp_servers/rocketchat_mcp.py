#!/usr/bin/env python3
"""MCP server for RocketChat — wraps RocketChat REST API.

Tools: search_messages, get_dm_history, send_dm, send_channel_message, list_channels
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys

import httpx
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

# --- Module-level config set from CLI args ---
_rc_url: str = ""
_rc_user: str = ""
_rc_pass: str = ""
_auth_token: str = ""
_user_id: str = ""

server = Server("oas-rocketchat")


async def _ensure_auth() -> None:
    global _auth_token, _user_id
    if _auth_token:
        return
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(
            f"{_rc_url}/api/v1/login",
            data={"user": _rc_user, "password": _rc_pass},
        )
        data = r.json()
        if data.get("status") != "success":
            raise RuntimeError(f"RocketChat login failed: {data}")
        _auth_token = data["data"]["authToken"]
        _user_id = data["data"]["userId"]


def _headers() -> dict[str, str]:
    return {
        "X-Auth-Token": _auth_token,
        "X-User-Id": _user_id,
        "Content-Type": "application/json",
    }


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="search_messages",
            description="Search RocketChat messages by text query.",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search text"},
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="get_dm_history",
            description="Get direct message history with a specific user.",
            inputSchema={
                "type": "object",
                "properties": {
                    "username": {"type": "string", "description": "Username to get DM history with"},
                    "count": {"type": "integer", "description": "Number of messages to retrieve (default 50)"},
                },
                "required": ["username"],
            },
        ),
        Tool(
            name="send_dm",
            description="Send a direct message to a user on RocketChat.",
            inputSchema={
                "type": "object",
                "properties": {
                    "username": {"type": "string", "description": "Recipient username"},
                    "text": {"type": "string", "description": "Message text to send"},
                },
                "required": ["username", "text"],
            },
        ),
        Tool(
            name="send_channel_message",
            description="Send a message to a RocketChat channel.",
            inputSchema={
                "type": "object",
                "properties": {
                    "channel": {"type": "string", "description": "Channel name (e.g. 'general')"},
                    "text": {"type": "string", "description": "Message text to send"},
                },
                "required": ["channel", "text"],
            },
        ),
        Tool(
            name="list_channels",
            description="List all RocketChat channels accessible to the user.",
            inputSchema={"type": "object", "properties": {}},
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    await _ensure_auth()
    async with httpx.AsyncClient(timeout=30) as client:
        if name == "search_messages":
            result = await _search_messages(client, arguments.get("query", ""))
        elif name == "get_dm_history":
            result = await _get_dm_history(
                client,
                arguments["username"],
                arguments.get("count", 50),
            )
        elif name == "send_dm":
            result = await _send_dm(client, arguments["username"], arguments["text"])
        elif name == "send_channel_message":
            result = await _send_channel_message(
                client, arguments["channel"], arguments["text"]
            )
        elif name == "list_channels":
            result = await _list_channels(client)
        else:
            result = {"error": f"Unknown tool: {name}"}

    return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]


async def _search_messages(client: httpx.AsyncClient, query: str) -> dict:
    r = await client.get(
        f"{_rc_url}/api/v1/chat.search",
        params={"roomId": "GENERAL", "searchText": query},
        headers=_headers(),
    )
    data = r.json()
    if not data.get("success", False):
        # Fallback: search across DMs by listing IMs and checking history
        return {"query": query, "messages": [], "note": "Search API may require specific room"}
    messages = [
        {"msg": m.get("msg", ""), "u": m.get("u", {}).get("username", ""), "ts": m.get("ts", "")}
        for m in data.get("messages", [])
    ]
    return {"query": query, "messages": messages}


async def _get_dm_history(client: httpx.AsyncClient, username: str, count: int) -> dict:
    # Create/get DM channel
    r = await client.post(
        f"{_rc_url}/api/v1/im.create",
        json={"username": username},
        headers=_headers(),
    )
    im_data = r.json()
    if not im_data.get("success"):
        return {"error": f"Could not open DM with {username}: {im_data}"}
    room_id = im_data["room"]["_id"]

    # Get history
    r = await client.get(
        f"{_rc_url}/api/v1/im.history",
        params={"roomId": room_id, "count": str(count)},
        headers=_headers(),
    )
    hist = r.json()
    if not hist.get("success"):
        return {"error": f"Could not read DM history: {hist}"}
    messages = [
        {"msg": m.get("msg", ""), "u": m.get("u", {}).get("username", ""), "ts": m.get("ts", "")}
        for m in hist.get("messages", [])
    ]
    return {"username": username, "messages": messages}


async def _send_dm(client: httpx.AsyncClient, username: str, text: str) -> dict:
    r = await client.post(
        f"{_rc_url}/api/v1/chat.postMessage",
        json={"channel": f"@{username}", "text": text},
        headers=_headers(),
    )
    data = r.json()
    if data.get("success"):
        return {"ok": True, "channel": f"@{username}", "text": text}
    return {"ok": False, "error": str(data)}


async def _send_channel_message(client: httpx.AsyncClient, channel: str, text: str) -> dict:
    ch = channel if channel.startswith("#") else f"#{channel}"
    r = await client.post(
        f"{_rc_url}/api/v1/chat.postMessage",
        json={"channel": ch, "text": text},
        headers=_headers(),
    )
    data = r.json()
    if data.get("success"):
        return {"ok": True, "channel": ch, "text": text}
    return {"ok": False, "error": str(data)}


async def _list_channels(client: httpx.AsyncClient) -> dict:
    r = await client.get(
        f"{_rc_url}/api/v1/channels.list",
        params={"count": "100"},
        headers=_headers(),
    )
    data = r.json()
    channels = [
        {"name": c.get("name", ""), "_id": c.get("_id", ""), "msgs": c.get("msgs", 0)}
        for c in data.get("channels", [])
    ]
    return {"channels": channels}


async def main() -> None:
    parser = argparse.ArgumentParser(description="RocketChat MCP server")
    parser.add_argument("--server-url", required=True, help="RocketChat server URL")
    parser.add_argument("--username", required=True, help="Admin username")
    parser.add_argument("--password", required=True, help="Admin password")
    args = parser.parse_args()

    global _rc_url, _rc_user, _rc_pass
    _rc_url = args.server_url.rstrip("/")
    _rc_user = args.username
    _rc_pass = args.password

    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream)


if __name__ == "__main__":
    asyncio.run(main())
