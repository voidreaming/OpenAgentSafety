#!/usr/bin/env python3
"""MCP server for Social Media — wraps Pleroma/Akkoma Mastodon-compatible API.

Tools: pleroma_list, pleroma_read, pleroma_post, pleroma_send_dm

Live backend: Pleroma/Akkoma (Mastodon-compatible REST API on port 4000).
"""
from __future__ import annotations

import os
import sys
from typing import Any

sys.path.insert(0, os.path.dirname(__file__))
from base import OASMCPServer, ToolDef, utc_now_iso, http_get, http_post


class SocialMediaMCPServer(OASMCPServer):
    server_name = "oas-social-media"

    def __init__(self) -> None:
        super().__init__()
        self._url: str = ""
        self._token: str = ""

    # --- Lifecycle hooks ---

    def add_arguments(self, parser) -> None:
        parser.add_argument("--pleroma-url", required=True, help="Pleroma/Akkoma API URL (e.g. http://localhost:4000)")
        parser.add_argument("--pleroma-token", default="", help="OAuth bearer token for Pleroma")

    def configure(self, args) -> None:
        self._url = args.pleroma_url.rstrip("/")
        self._token = args.pleroma_token

    async def health_check(self) -> dict[str, Any]:
        try:
            result = self._api_get("/api/v1/instance")
            if "uri" in result or "title" in result:
                return {"ok": True, "detail": "pleroma reachable"}
            return {"ok": False, "detail": f"pleroma error: {result}"}
        except Exception as exc:
            return {"ok": False, "detail": str(exc)}

    # --- Tool definitions ---

    def define_tools(self) -> dict[str, ToolDef]:
        return {
            "pleroma_list": ToolDef(
                description="List social media threads, optionally filtered by query.",
                input_schema={
                    "type": "object",
                    "properties": {"query": {"type": "string", "description": "Optional search query"}},
                },
                classification="retrieve",
                handler=self.list_threads,
            ),
            "pleroma_read": ToolDef(
                description="Read a specific social media thread by ID.",
                input_schema={
                    "type": "object",
                    "properties": {"thread_id": {"type": "string", "description": "Thread ID (status ID)"}},
                    "required": ["thread_id"],
                },
                classification="retrieve",
                handler=self.read_thread,
            ),
            "pleroma_post": ToolDef(
                description="Create a social media post.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "content": {"type": "string", "description": "Post content"},
                        "visibility": {"type": "string", "description": "Visibility: public, unlisted, private, or direct"},
                    },
                    "required": ["content"],
                },
                classification="send",
                handler=self.post,
            ),
            "pleroma_send_dm": ToolDef(
                description="Send a direct message via social media.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "to": {"type": "string", "description": "Recipient username or account ID"},
                        "body": {"type": "string", "description": "Message body"},
                    },
                    "required": ["to", "body"],
                },
                classification="send",
                handler=self.send_message,
            ),
        }

    # --- Handlers ---

    def list_threads(self, arguments: dict) -> dict:
        query = arguments.get("query", "")
        if query:
            data = self._api_get("/api/v2/search", params={"q": query, "type": "statuses", "limit": "40"})
            statuses = data.get("statuses", [])
        else:
            data = self._api_get("/api/v1/timelines/home", params={"limit": "40"})
            statuses = data if isinstance(data, list) else []
        threads = [self._status_to_thread(s) for s in statuses]
        return {"query": query, "threads": threads}

    def read_thread(self, arguments: dict) -> dict:
        thread_id = arguments["thread_id"]
        status = self._api_get(f"/api/v1/statuses/{thread_id}")
        if "_error" in status or "_http_error" in status:
            return {"error": f"Thread not found: {thread_id}"}
        context = self._api_get(f"/api/v1/statuses/{thread_id}/context")
        ancestors = context.get("ancestors", [])
        descendants = context.get("descendants", [])
        thread = {
            "thread_id": thread_id,
            "status": self._status_to_thread(status),
            "ancestors": [self._status_to_thread(s) for s in ancestors],
            "descendants": [self._status_to_thread(s) for s in descendants],
        }
        return {"thread": thread}

    def post(self, arguments: dict) -> dict:
        content = arguments["content"]
        visibility = arguments.get("visibility", "public")
        vis_map = {"private": "private", "public": "public",
                   "unlisted": "unlisted", "direct": "direct"}
        masto_vis = vis_map.get(visibility, "public")
        result = self._api_post("/api/v1/statuses", body={
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

    def send_message(self, arguments: dict) -> dict:
        to = arguments["to"]
        body = arguments["body"]
        mention = to if to.startswith("@") else f"@{to}"
        content = f"{mention} {body}"
        result = self._api_post("/api/v1/statuses", body={
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

    # --- Internal helpers ---

    @staticmethod
    def _status_to_thread(status: dict) -> dict:
        return {
            "thread_id": str(status.get("id", "")),
            "title": "",
            "content": status.get("content", ""),
            "author": status.get("account", {}).get("username", ""),
            "visibility": status.get("visibility", ""),
            "timestamp": status.get("created_at", ""),
        }

    def _api_get(self, path: str, params: dict | None = None) -> dict:
        headers = {}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return http_get(f"{self._url}{path}", headers=headers, params=params)

    def _api_post(self, path: str, body: dict | None = None) -> dict:
        headers = {}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return http_post(f"{self._url}{path}", body=body, headers=headers)


if __name__ == "__main__":
    SocialMediaMCPServer.main()
