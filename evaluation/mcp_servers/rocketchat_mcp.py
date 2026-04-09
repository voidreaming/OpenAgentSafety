#!/usr/bin/env python3
"""MCP server for RocketChat — wraps RocketChat REST API.

Tools: rc_search, rc_dm_history, rc_send_dm, rc_send_channel, rc_list_channels

Uses httpx.AsyncClient for async HTTP + stateful auth + rate-limit retry.
"""
from __future__ import annotations

import asyncio
import os
import re
import sys
from typing import Any

import httpx

sys.path.insert(0, os.path.dirname(__file__))
from base import OASMCPServer, ToolDef


class RocketChatMCPServer(OASMCPServer):
    server_name = "oas-rocketchat"

    _MAX_RETRIES = 4
    _BASE_DELAY = 2.0  # seconds

    def __init__(self) -> None:
        super().__init__()
        self._url: str = ""
        self._user: str = ""
        self._pass: str = ""
        self._auth_token: str = ""
        self._user_id: str = ""

    # --- Lifecycle hooks ---

    def add_arguments(self, parser) -> None:
        parser.add_argument("--server-url", required=True, help="RocketChat server URL")
        parser.add_argument("--username", required=True, help="Admin username")
        parser.add_argument("--password", required=True, help="Admin password")

    def configure(self, args) -> None:
        self._url = args.server_url.rstrip("/")
        self._user = args.username
        self._pass = args.password

    async def startup(self) -> None:
        # Don't auth at startup — auth lazily on first tool call.
        # This prevents the stdio server from blocking during MCP initialize,
        # which would cause create_mcp_tools() to time out.
        pass

    async def health_check(self) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.get(
                    f"{self._url}/api/v1/me",
                    headers=self._headers(),
                )
                data = r.json()
                if data.get("_id"):
                    return {"ok": True, "detail": f"rc auth ok: {data.get('username', '')}"}
                return {"ok": False, "detail": f"rc /me failed: {data}"}
        except Exception as exc:
            return {"ok": False, "detail": str(exc)}

    # --- Tool definitions ---

    def define_tools(self) -> dict[str, ToolDef]:
        return {
            "rc_search": ToolDef(
                description="Search RocketChat messages by text query.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Search text"},
                    },
                    "required": ["query"],
                },
                classification="retrieve",
                handler=self.search_messages,
            ),
            "rc_dm_history": ToolDef(
                description="Get direct message history with a specific user.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "username": {"type": "string", "description": "Username to get DM history with"},
                        "count": {"type": "integer", "description": "Number of messages to retrieve (default 50)"},
                    },
                    "required": ["username"],
                },
                classification="retrieve",
                handler=self.get_dm_history,
            ),
            "rc_send_dm": ToolDef(
                description="Send a direct message to a user on RocketChat.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "username": {"type": "string", "description": "Recipient username"},
                        "text": {"type": "string", "description": "Message text to send"},
                    },
                    "required": ["username", "text"],
                },
                classification="send",
                handler=self.send_dm,
            ),
            "rc_send_channel": ToolDef(
                description="Send a message to a RocketChat channel.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "channel": {"type": "string", "description": "Channel name (e.g. 'general')"},
                        "text": {"type": "string", "description": "Message text to send"},
                    },
                    "required": ["channel", "text"],
                },
                classification="send",
                handler=self.send_channel,
            ),
            "rc_list_channels": ToolDef(
                description="List all RocketChat channels accessible to the user.",
                input_schema={"type": "object", "properties": {}},
                classification="retrieve",
                handler=self.list_channels,
            ),
        }

    # --- Handlers (all async — uses httpx) ---

    async def search_messages(self, arguments: dict) -> dict:
        query = arguments.get("query", "")
        return await self._rc_request(
            "GET", "/api/v1/chat.search",
            params={"roomId": "GENERAL", "searchText": query},
            transform=lambda data: {
                "query": query,
                "messages": [
                    {"msg": m.get("msg", ""), "u": m.get("u", {}).get("username", ""), "ts": m.get("ts", "")}
                    for m in data.get("messages", [])
                ] if data.get("success") else [],
                **({"note": "Search API may require specific room"} if not data.get("success") else {}),
            },
        )

    async def get_dm_history(self, arguments: dict) -> dict:
        username = arguments["username"]
        count = arguments.get("count", 50)

        # Create/get DM channel
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                f"{self._url}/api/v1/im.create",
                json={"username": username},
                headers=self._headers(),
            )
            im_data = r.json()
            if not im_data.get("success"):
                return {"error": f"Could not open DM with {username}: {im_data}"}
            room_id = im_data["room"]["_id"]

            r = await client.get(
                f"{self._url}/api/v1/im.history",
                params={"roomId": room_id, "count": str(count)},
                headers=self._headers(),
            )
            hist = r.json()
            if not hist.get("success"):
                return {"error": f"Could not read DM history: {hist}"}
            return {
                "username": username,
                "messages": [
                    {"msg": m.get("msg", ""), "u": m.get("u", {}).get("username", ""), "ts": m.get("ts", "")}
                    for m in hist.get("messages", [])
                ],
            }

    async def send_dm(self, arguments: dict) -> dict:
        username = arguments["username"]
        text = arguments["text"]
        return await self._rc_request(
            "POST", "/api/v1/chat.postMessage",
            json={"channel": f"@{username}", "text": text},
            transform=lambda data: (
                {"ok": True, "channel": f"@{username}", "text": text}
                if data.get("success")
                else {"ok": False, "error": str(data)}
            ),
        )

    async def send_channel(self, arguments: dict) -> dict:
        channel = arguments["channel"]
        text = arguments["text"]
        ch = channel if channel.startswith("#") else f"#{channel}"
        return await self._rc_request(
            "POST", "/api/v1/chat.postMessage",
            json={"channel": ch, "text": text},
            transform=lambda data: (
                {"ok": True, "channel": ch, "text": text}
                if data.get("success")
                else {"ok": False, "error": str(data)}
            ),
        )

    async def list_channels(self, arguments: dict) -> dict:
        return await self._rc_request(
            "GET", "/api/v1/channels.list",
            params={"count": "100"},
            transform=lambda data: {
                "channels": [
                    {"name": c.get("name", ""), "_id": c.get("_id", ""), "msgs": c.get("msgs", 0)}
                    for c in data.get("channels", [])
                ],
            },
        )

    # --- Internal: auth + HTTP with rate-limit retry ---

    def _headers(self) -> dict[str, str]:
        return {
            "X-Auth-Token": self._auth_token,
            "X-User-Id": self._user_id,
            "Content-Type": "application/json",
        }

    async def _ensure_auth(self) -> None:
        if self._auth_token:
            return
        for attempt in range(self._MAX_RETRIES):
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.post(
                    f"{self._url}/api/v1/login",
                    data={"user": self._user, "password": self._pass},
                )
                data = r.json()
                if data.get("status") == "success":
                    self._auth_token = data["data"]["authToken"]
                    self._user_id = data["data"]["userId"]
                    return
                error = data.get("error", "")
                if "too many requests" in str(error).lower() and attempt < self._MAX_RETRIES - 1:
                    wait = self._parse_retry_after(error) or self._BASE_DELAY * (2 ** attempt)
                    await asyncio.sleep(min(wait, 60))
                    continue
                raise RuntimeError(f"RocketChat login failed: {data}")

    async def _rc_request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json: dict | None = None,
        transform: Any = None,
    ) -> dict:
        """Execute an HTTP request with rate-limit retry.

        ``transform`` is an optional function that converts the raw JSON
        response dict to the tool's return dict.
        """
        for attempt in range(self._MAX_RETRIES):
            async with httpx.AsyncClient(timeout=30) as client:
                if method == "GET":
                    r = await client.get(f"{self._url}{path}", params=params, headers=self._headers())
                else:
                    r = await client.post(f"{self._url}{path}", json=json, headers=self._headers())
                data = r.json()

            # Check for rate-limit
            error_str = str(data.get("error", ""))
            if "too many requests" in error_str.lower() and attempt < self._MAX_RETRIES - 1:
                wait = self._parse_retry_after(error_str) or self._BASE_DELAY * (2 ** attempt)
                await asyncio.sleep(min(wait, 60))
                self._auth_token = ""
                try:
                    await self._ensure_auth()
                except RuntimeError:
                    pass
                continue

            if transform:
                return transform(data)
            return data

        # Final attempt exhausted
        return {"error": "Rate limit exceeded after max retries"}

    @staticmethod
    def _parse_retry_after(error_msg: str) -> float | None:
        m = re.search(r"wait (\d+) seconds", str(error_msg))
        return float(m.group(1)) if m else None


if __name__ == "__main__":
    RocketChatMCPServer.main()
