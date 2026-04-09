#!/usr/bin/env python3
"""MCP server for Files/Drive — wraps ownCloud WebDAV.

Tools: owncloud_list, owncloud_read, owncloud_write

Live backend: ownCloud WebDAV (port 8092).
"""
from __future__ import annotations

import base64 as b64mod
import os
import re
import sys
from typing import Any
from urllib.error import HTTPError

sys.path.insert(0, os.path.dirname(__file__))
from base import (
    OASMCPServer, ToolDef,
    utc_now_iso, http_put, sanitize_path,
    http_propfind, http_mkcol, _do_request_raw,
)


class FilesMCPServer(OASMCPServer):
    server_name = "oas-files"

    def __init__(self) -> None:
        super().__init__()
        self._url: str = ""
        self._username: str = ""
        self._password: str = ""

    # --- Lifecycle hooks ---

    def add_arguments(self, parser) -> None:
        parser.add_argument("--owncloud-url", required=True, help="ownCloud WebDAV URL")
        parser.add_argument("--username", required=True, help="ownCloud username")
        parser.add_argument("--password", required=True, help="ownCloud password")

    def configure(self, args) -> None:
        self._url = args.owncloud_url.rstrip("/")
        self._username = args.username
        self._password = args.password

    async def health_check(self) -> dict[str, Any]:
        try:
            from base import http_get
            result = http_get(f"{self._url}/status.php")
            if isinstance(result, dict) and result.get("installed"):
                return {"ok": True, "detail": "owncloud reachable"}
            return {"ok": False, "detail": f"owncloud status: {result}"}
        except Exception as exc:
            return {"ok": False, "detail": str(exc)}

    # --- Tool definitions ---

    def define_tools(self) -> dict[str, ToolDef]:
        return {
            "owncloud_list": ToolDef(
                description="List files in ownCloud/Drive by path.",
                input_schema={
                    "type": "object",
                    "properties": {"path": {"type": "string", "description": "Directory path to list"}},
                    "required": ["path"],
                },
                classification="retrieve",
                handler=self.list_files,
            ),
            "owncloud_read": ToolDef(
                description="Read a file from ownCloud/Drive by path.",
                input_schema={
                    "type": "object",
                    "properties": {"path": {"type": "string", "description": "File path"}},
                    "required": ["path"],
                },
                classification="retrieve",
                handler=self.read_file,
            ),
            "owncloud_write": ToolDef(
                description="Write content to a file in ownCloud/Drive.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "File path"},
                        "content": {"type": "string", "description": "File content"},
                    },
                    "required": ["path", "content"],
                },
                classification="mutate",
                handler=self.write_file,
            ),
        }

    # --- Handlers ---

    def list_files(self, arguments: dict) -> dict:
        path = sanitize_path(arguments.get("path", "/"))
        webdav_path = path.rstrip("/") + "/"
        url = f"{self._webdav_base()}{webdav_path}"
        propfind_body = (
            '<?xml version="1.0" encoding="utf-8" ?>'
            '<D:propfind xmlns:D="DAV:">'
            '<D:prop><D:displayname/><D:getcontentlength/><D:resourcetype/><D:getlastmodified/></D:prop>'
            '</D:propfind>'
        )
        entries = []
        try:
            body = http_propfind(url, body=propfind_body, headers=self._auth_header(), depth="1")
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

    def read_file(self, arguments: dict) -> dict:
        path = sanitize_path(arguments.get("path", "/"))
        url = f"{self._webdav_base()}{path}"
        try:
            content = _do_request_raw(url, method="GET", headers=self._auth_header())
            return {"file": {"path": path, "content": content, "labels": [], "updated_at": ""}}
        except HTTPError as exc:
            return {"error": f"File not found: {path} (HTTP {exc.code})"}
        except Exception as exc:
            return {"error": f"Read failed: {exc}"}

    def write_file(self, arguments: dict) -> dict:
        path = sanitize_path(arguments.get("path", "/"))
        content = arguments.get("content", "")
        url = f"{self._webdav_base()}{path}"
        self._ensure_parent_dirs(path)
        result = http_put(
            url,
            body=content.encode("utf-8"),
            content_type="text/plain",
            headers=self._auth_header(),
        )
        if "_error" in result or "_http_error" in result:
            error = result.get("_error") or f"HTTP {result.get('_http_error')}"
            return {"error": f"Write failed: {error}"}
        return {"file": {"path": path, "content": content, "labels": [],
                          "updated_at": utc_now_iso()}}

    # --- Internal helpers ---

    def _webdav_base(self) -> str:
        return f"{self._url}/remote.php/dav/files/{self._username}"

    def _auth_header(self) -> dict[str, str]:
        creds = b64mod.b64encode(f"{self._username}:{self._password}".encode()).decode()
        return {"Authorization": f"Basic {creds}"}

    def _ensure_parent_dirs(self, path: str) -> None:
        parts = path.strip("/").split("/")
        for i in range(len(parts) - 1):
            dir_path = "/" + "/".join(parts[: i + 1]) + "/"
            url = f"{self._webdav_base()}{dir_path}"
            http_mkcol(url, headers=self._auth_header())


if __name__ == "__main__":
    FilesMCPServer.main()
