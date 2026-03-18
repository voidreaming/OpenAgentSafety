#!/usr/bin/env python3
"""MCP server for Files/Drive — wraps ownCloud WebDAV.

Tools: list, read, write

Live backend: ownCloud WebDAV (port 8092).
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(__file__))
from base import utc_now_iso, http_put

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool
from urllib.request import Request, urlopen
from urllib.error import HTTPError

_owncloud_url: str = ""    # e.g. http://the-agent-company.com:8092
_oc_username: str = ""
_oc_password: str = ""

server = Server("oas-files")


def _webdav_base() -> str:
    return f"{_owncloud_url}/remote.php/dav/files/{_oc_username}"


def _auth_header() -> dict[str, str]:
    creds = base64.b64encode(f"{_oc_username}:{_oc_password}".encode()).decode()
    return {"Authorization": f"Basic {creds}"}


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="list",
            description="List files by path.",
            inputSchema={
                "type": "object",
                "properties": {"path": {"type": "string", "description": "Directory path to list"}},
                "required": ["path"],
            },
        ),
        Tool(
            name="read",
            description="Read a file by path.",
            inputSchema={
                "type": "object",
                "properties": {"path": {"type": "string", "description": "File path"}},
                "required": ["path"],
            },
        ),
        Tool(
            name="write",
            description="Write content to a file.",
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path"},
                    "content": {"type": "string", "description": "File content"},
                },
                "required": ["path", "content"],
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    if name == "list":
        result = _list(arguments["path"])
    elif name == "read":
        result = _read(arguments["path"])
    elif name == "write":
        result = _write(arguments["path"], arguments["content"])
    else:
        result = {"error": f"Unknown tool: {name}"}
    return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]


def _list(path: str) -> dict:
    """List files in ownCloud via PROPFIND."""
    webdav_path = path.rstrip("/") + "/"
    url = f"{_webdav_base()}{webdav_path}"
    propfind_body = (
        '<?xml version="1.0" encoding="utf-8" ?>'
        '<D:propfind xmlns:D="DAV:">'
        '<D:prop><D:displayname/><D:getcontentlength/><D:resourcetype/><D:getlastmodified/></D:prop>'
        '</D:propfind>'
    )
    headers = {**_auth_header(), "Content-Type": "application/xml", "Depth": "1"}
    req = Request(url, data=propfind_body.encode(), headers=headers, method="PROPFIND")
    entries = []
    try:
        with urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8", errors="replace")
        hrefs = re.findall(r"<(?:D:)?href>([^<]+)</(?:D:)?href>", body)
        for href in hrefs:
            cleaned = href.rstrip("/")
            name = cleaned.rsplit("/", 1)[-1] if "/" in cleaned else cleaned
            if not name:
                continue
            entries.append({"path": f"{webdav_path}{name}", "labels": []})
    except HTTPError as exc:
        return {"path": path, "entries": [], "error": f"PROPFIND failed: {exc.code}"}
    except Exception as exc:
        return {"path": path, "entries": [], "error": str(exc)}
    return {"path": path, "entries": entries}


def _read(path: str) -> dict:
    """Read a file from ownCloud via GET."""
    url = f"{_webdav_base()}{path}"
    req = Request(url, headers=_auth_header(), method="GET")
    try:
        with urlopen(req, timeout=30) as resp:
            content = resp.read().decode("utf-8", errors="replace")
        return {"file": {"path": path, "content": content, "labels": [], "updated_at": ""}}
    except HTTPError as exc:
        return {"error": f"File not found: {path} (HTTP {exc.code})"}
    except Exception as exc:
        return {"error": f"Read failed: {exc}"}


def _write(path: str, content: str) -> dict:
    """Write a file to ownCloud via PUT."""
    url = f"{_webdav_base()}{path}"
    _ensure_parent_dirs(path)
    result = http_put(
        url,
        body=content.encode("utf-8"),
        content_type="text/plain",
        headers=_auth_header(),
    )
    if "_error" in result or "_http_error" in result:
        error = result.get("_error") or f"HTTP {result.get('_http_error')}"
        return {"error": f"Write failed: {error}"}
    return {"file": {"path": path, "content": content, "labels": [],
                      "updated_at": utc_now_iso()}}


def _ensure_parent_dirs(path: str) -> None:
    """Create parent directories via MKCOL if they don't exist."""
    parts = path.strip("/").split("/")
    for i in range(len(parts) - 1):
        dir_path = "/" + "/".join(parts[: i + 1]) + "/"
        url = f"{_webdav_base()}{dir_path}"
        req = Request(url, headers=_auth_header(), method="MKCOL")
        try:
            urlopen(req, timeout=10)
        except Exception:
            pass  # Already exists (405 or 409)


async def main() -> None:
    global _owncloud_url, _oc_username, _oc_password
    parser = argparse.ArgumentParser(description="Files MCP server")
    parser.add_argument("--owncloud-url", required=True, help="ownCloud WebDAV URL")
    parser.add_argument("--username", required=True, help="ownCloud username")
    parser.add_argument("--password", required=True, help="ownCloud password")
    args = parser.parse_args()

    _owncloud_url = args.owncloud_url.rstrip("/")
    _oc_username = args.username
    _oc_password = args.password

    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream)


if __name__ == "__main__":
    asyncio.run(main())
