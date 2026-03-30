#!/usr/bin/env python3
"""MCP server for GitLab — wraps GitLab REST API v4.

Tools: list_files, read_file, search_code (read-only, repo content)

Live backend: GitLab CE (REST API on port 8929).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from urllib.parse import quote

sys.path.insert(0, os.path.dirname(__file__))
from base import http_get, make_tool_result

from mcp.server import Server
from mcp.server.models import InitializationOptions
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, TextContent, Tool, ServerCapabilities

_gitlab_url: str = ""          # e.g. http://the-agent-company.com:8929
_private_token: str = ""       # PRIVATE-TOKEN header value

server = Server("oas-gitlab")


def _api(path: str, params: dict | None = None) -> dict | list:
    """GET request to GitLab API v4."""
    headers = {"PRIVATE-TOKEN": _private_token} if _private_token else {}
    url = f"{_gitlab_url}/api/v4{path}"
    result = http_get(url, headers=headers, params=params)
    return result


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="list_projects",
            description="List all GitLab projects.",
            inputSchema={
                "type": "object",
                "properties": {
                    "search": {"type": "string", "description": "Filter projects by name (optional)"},
                },
            },
        ),
        Tool(
            name="list_files",
            description="List files and directories in a GitLab repository.",
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {"type": "string", "description": "Project name or ID"},
                    "path": {"type": "string", "description": "Directory path (default: root)"},
                    "ref": {"type": "string", "description": "Branch or tag (default: main)"},
                },
                "required": ["project"],
            },
        ),
        Tool(
            name="read_file",
            description="Read the content of a file from a GitLab repository.",
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {"type": "string", "description": "Project name or ID"},
                    "path": {"type": "string", "description": "File path in the repository"},
                    "ref": {"type": "string", "description": "Branch or tag (default: main)"},
                },
                "required": ["project", "path"],
            },
        ),
        Tool(
            name="search_code",
            description="Search for code across GitLab repositories.",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "project": {"type": "string", "description": "Limit search to a specific project (optional)"},
                },
                "required": ["query"],
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> CallToolResult:
    if name == "list_projects":
        result = _list_projects(arguments.get("search", ""))
    elif name == "list_files":
        result = _list_files(
            arguments["project"],
            arguments.get("path", ""),
            arguments.get("ref", ""),
        )
    elif name == "read_file":
        result = _read_file(
            arguments["project"],
            arguments["path"],
            arguments.get("ref", ""),
        )
    elif name == "search_code":
        result = _search_code(
            arguments["query"],
            arguments.get("project", ""),
        )
    else:
        result = {"error": f"Unknown tool: {name}"}
    return make_tool_result(result)


def _resolve_project_id(project: str) -> int | None:
    """Resolve a project name or ID to a numeric project ID."""
    # If already numeric, use directly
    try:
        return int(project)
    except ValueError:
        pass
    # Search by name
    projects = _api("/projects", params={"search": project, "per_page": "20"})
    if isinstance(projects, list):
        for p in projects:
            if p.get("name", "").lower() == project.lower() or p.get("path_with_namespace", "").lower() == project.lower():
                return p["id"]
        # Fallback: return first match
        if projects:
            return projects[0]["id"]
    return None


def _list_projects(search: str = "") -> dict:
    """List all projects, optionally filtered by name."""
    params: dict = {"per_page": "50"}
    if search:
        params["search"] = search
    projects = _api("/projects", params=params)
    if not isinstance(projects, list):
        return {"error": f"Unexpected response: {projects}"}
    return {
        "projects": [
            {
                "id": p.get("id"),
                "name": p.get("name", ""),
                "path": p.get("path_with_namespace", ""),
                "description": p.get("description", ""),
                "default_branch": p.get("default_branch", "main"),
            }
            for p in projects
        ]
    }


def _list_files(project: str, path: str = "", ref: str = "") -> dict:
    """List files in a repository directory."""
    project_id = _resolve_project_id(project)
    if project_id is None:
        return {"error": f"Project not found: {project}"}

    params: dict = {"per_page": "100"}
    if path:
        params["path"] = path
    if ref:
        params["ref"] = ref

    tree = _api(f"/projects/{project_id}/repository/tree", params=params)
    if not isinstance(tree, list):
        return {"error": f"Failed to list files: {tree}"}
    return {
        "project": project,
        "path": path or "/",
        "entries": [
            {
                "name": entry.get("name", ""),
                "type": entry.get("type", ""),  # "blob" or "tree"
                "path": entry.get("path", ""),
            }
            for entry in tree
        ],
    }


def _read_file(project: str, path: str, ref: str = "") -> dict:
    """Read file content from a repository."""
    project_id = _resolve_project_id(project)
    if project_id is None:
        return {"error": f"Project not found: {project}"}

    encoded_path = quote(path, safe="")
    params: dict = {}
    if ref:
        params["ref"] = ref

    # Get file metadata + content (base64 encoded)
    file_data = _api(f"/projects/{project_id}/repository/files/{encoded_path}", params=params)
    if isinstance(file_data, dict) and "content" in file_data:
        import base64
        try:
            content = base64.b64decode(file_data["content"]).decode("utf-8", errors="replace")
        except Exception:
            content = file_data["content"]
        return {
            "file": {
                "path": path,
                "content": content,
                "size": file_data.get("size", 0),
                "encoding": file_data.get("encoding", ""),
                "ref": file_data.get("ref", ref),
                "last_commit_id": file_data.get("last_commit_id", ""),
            }
        }
    return {"error": f"File not found: {path} in project {project}"}


def _search_code(query: str, project: str = "") -> dict:
    """Search code across repositories."""
    params: dict = {"scope": "blobs", "search": query, "per_page": "20"}
    if project:
        project_id = _resolve_project_id(project)
        if project_id is None:
            return {"error": f"Project not found: {project}"}
        # Project-level search
        results = _api(f"/projects/{project_id}/search", params=params)
    else:
        # Global search
        results = _api("/search", params=params)

    if not isinstance(results, list):
        return {"error": f"Search failed: {results}"}

    return {
        "query": query,
        "results": [
            {
                "project_id": r.get("project_id", ""),
                "path": r.get("path", r.get("filename", "")),
                "ref": r.get("ref", ""),
                "data": r.get("data", ""),
                "startline": r.get("startline", 0),
            }
            for r in results
        ],
    }


async def main() -> None:
    global _gitlab_url, _private_token
    parser = argparse.ArgumentParser(description="GitLab MCP server")
    parser.add_argument("--gitlab-url", required=True, help="GitLab URL (e.g. http://localhost:8929)")
    parser.add_argument("--private-token", default=os.getenv("GITLAB_TOKEN", ""), help="GitLab PRIVATE-TOKEN")
    args = parser.parse_args()

    _gitlab_url = args.gitlab_url.rstrip("/")
    _private_token = args.private_token

    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream, write_stream,
            InitializationOptions(
                server_name=server.name,
                server_version="1.0.0",
                capabilities=ServerCapabilities(tools={"listChanged": False}),
            ),
        )


if __name__ == "__main__":
    asyncio.run(main())
