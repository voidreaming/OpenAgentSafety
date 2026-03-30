#!/usr/bin/env python3
"""MCP server for Plane — wraps Plane REST API v1.

Tools: list_issues, read_issue, search_issues, create_issue, update_issue, add_comment

Live backend: Plane (REST API on port 8091).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from base import utc_now_iso, http_get, http_post, make_tool_result

from mcp.server import Server
from mcp.server.models import InitializationOptions
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, TextContent, Tool, ServerCapabilities
from urllib.request import Request, urlopen
from urllib.error import HTTPError

_plane_url: str = ""               # e.g. http://the-agent-company.com:8091
_api_key: str = ""                 # x-api-key header
_workspace_slug: str = "tac"       # default workspace

server = Server("oas-plane")


def _headers() -> dict[str, str]:
    return {
        "x-api-key": _api_key,
        "Content-Type": "application/json",
    }


def _api_get(path: str, params: dict | None = None) -> dict | list:
    """GET to Plane API."""
    return http_get(f"{_plane_url}/api/v1/workspaces/{_workspace_slug}{path}",
                    headers=_headers(), params=params)


def _api_post(path: str, body: dict | None = None) -> dict:
    """POST to Plane API."""
    return http_post(f"{_plane_url}/api/v1/workspaces/{_workspace_slug}{path}",
                     body=body, headers=_headers())


def _api_patch(path: str, body: dict) -> dict:
    """PATCH to Plane API."""
    url = f"{_plane_url}/api/v1/workspaces/{_workspace_slug}{path}"
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = Request(url, data=data, headers=_headers(), method="PATCH")
    try:
        with urlopen(req, timeout=30) as resp:
            text = resp.read().decode("utf-8", errors="replace")
            return json.loads(text) if text.strip() else {"ok": True}
    except HTTPError as exc:
        resp_body = exc.read().decode("utf-8", errors="replace")
        try:
            return json.loads(resp_body)
        except Exception:
            return {"_http_error": exc.code, "_body": resp_body}
    except Exception as exc:
        return {"_error": str(exc)}


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="list_projects",
            description="List all projects in the Plane workspace.",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="list_issues",
            description="List all issues in a Plane project.",
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {"type": "string", "description": "Project name or ID"},
                },
                "required": ["project"],
            },
        ),
        Tool(
            name="read_issue",
            description="Read details of a specific issue.",
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {"type": "string", "description": "Project name or ID"},
                    "issue_id": {"type": "string", "description": "Issue ID (UUID)"},
                },
                "required": ["project", "issue_id"],
            },
        ),
        Tool(
            name="search_issues",
            description="Search issues by text query within a project.",
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {"type": "string", "description": "Project name or ID"},
                    "query": {"type": "string", "description": "Search query"},
                },
                "required": ["project", "query"],
            },
        ),
        Tool(
            name="create_issue",
            description="Create a new issue in a Plane project.",
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {"type": "string", "description": "Project name or ID"},
                    "name": {"type": "string", "description": "Issue title"},
                    "description": {"type": "string", "description": "Issue description (optional)"},
                    "priority": {"type": "string", "description": "Priority: none, low, medium, high, urgent (optional)"},
                    "state": {"type": "string", "description": "State name, e.g. 'Todo', 'In Progress' (optional)"},
                },
                "required": ["project", "name"],
            },
        ),
        Tool(
            name="update_issue",
            description="Update an existing issue (name, description, priority, state, assignees).",
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {"type": "string", "description": "Project name or ID"},
                    "issue_id": {"type": "string", "description": "Issue ID (UUID)"},
                    "name": {"type": "string", "description": "New issue title (optional)"},
                    "description": {"type": "string", "description": "New description (optional)"},
                    "priority": {"type": "string", "description": "New priority (optional)"},
                    "state": {"type": "string", "description": "New state name (optional)"},
                },
                "required": ["project", "issue_id"],
            },
        ),
        Tool(
            name="add_comment",
            description="Add a comment to an issue.",
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {"type": "string", "description": "Project name or ID"},
                    "issue_id": {"type": "string", "description": "Issue ID (UUID)"},
                    "comment": {"type": "string", "description": "Comment text"},
                },
                "required": ["project", "issue_id", "comment"],
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> CallToolResult:
    if name == "list_projects":
        result = _list_projects()
    elif name == "list_issues":
        result = _list_issues(arguments["project"])
    elif name == "read_issue":
        result = _read_issue(arguments["project"], arguments["issue_id"])
    elif name == "search_issues":
        result = _search_issues(arguments["project"], arguments["query"])
    elif name == "create_issue":
        result = _create_issue(
            arguments["project"],
            arguments["name"],
            arguments.get("description", ""),
            arguments.get("priority", ""),
            arguments.get("state", ""),
        )
    elif name == "update_issue":
        result = _update_issue(
            arguments["project"],
            arguments["issue_id"],
            name=arguments.get("name"),
            description=arguments.get("description"),
            priority=arguments.get("priority"),
            state=arguments.get("state"),
        )
    elif name == "add_comment":
        result = _add_comment(arguments["project"], arguments["issue_id"], arguments["comment"])
    else:
        result = {"error": f"Unknown tool: {name}"}
    return make_tool_result(result)


def _resolve_project_id(project: str) -> str | None:
    """Resolve project name to project ID (UUID)."""
    # If looks like a UUID, use directly
    if len(project) >= 32 and "-" in project:
        return project

    resp = _api_get("/projects/")
    projects = resp.get("results", []) if isinstance(resp, dict) else resp if isinstance(resp, list) else []
    for p in projects:
        if p.get("name", "").lower() == project.lower() or p.get("identifier", "").lower() == project.lower():
            return p["id"]
    # Fallback: first match
    if projects:
        return projects[0]["id"]
    return None


def _get_state_id(project_id: str, state_name: str) -> str | None:
    """Resolve state name to state ID."""
    resp = _api_get(f"/projects/{project_id}/states/")
    states = resp.get("results", []) if isinstance(resp, dict) else resp if isinstance(resp, list) else []
    for s in states:
        if s.get("name", "").lower() == state_name.lower():
            return s["id"]
    return None


def _format_issue(issue: dict) -> dict:
    """Format a Plane issue into a consistent structure."""
    return {
        "issue_id": issue.get("id", ""),
        "name": issue.get("name", ""),
        "description": issue.get("description_stripped", issue.get("description", "")),
        "priority": issue.get("priority", "none"),
        "state": issue.get("state_detail", {}).get("name", "") if isinstance(issue.get("state_detail"), dict) else "",
        "state_id": issue.get("state", ""),
        "assignees": issue.get("assignees", []),
        "labels": [l.get("name", "") if isinstance(l, dict) else l for l in issue.get("labels", [])],
        "created_at": issue.get("created_at", ""),
        "updated_at": issue.get("updated_at", ""),
    }


def _list_projects() -> dict:
    """List all projects in the workspace."""
    resp = _api_get("/projects/")
    projects = resp.get("results", []) if isinstance(resp, dict) else resp if isinstance(resp, list) else []
    return {
        "projects": [
            {
                "id": p.get("id", ""),
                "name": p.get("name", ""),
                "identifier": p.get("identifier", ""),
                "description": p.get("description", ""),
            }
            for p in projects
        ]
    }


def _list_issues(project: str) -> dict:
    """List all issues in a project."""
    project_id = _resolve_project_id(project)
    if not project_id:
        return {"error": f"Project not found: {project}"}

    resp = _api_get(f"/projects/{project_id}/issues/")
    issues = resp.get("results", []) if isinstance(resp, dict) else resp if isinstance(resp, list) else []
    return {
        "project": project,
        "issues": [_format_issue(i) for i in issues],
    }


def _read_issue(project: str, issue_id: str) -> dict:
    """Read a specific issue by ID."""
    project_id = _resolve_project_id(project)
    if not project_id:
        return {"error": f"Project not found: {project}"}

    resp = _api_get(f"/projects/{project_id}/issues/{issue_id}/")
    if isinstance(resp, dict) and resp.get("id"):
        return {"issue": _format_issue(resp)}
    return {"error": f"Issue not found: {issue_id}"}


def _search_issues(project: str, query: str) -> dict:
    """Search issues by text within a project."""
    project_id = _resolve_project_id(project)
    if not project_id:
        return {"error": f"Project not found: {project}"}

    resp = _api_get(f"/projects/{project_id}/issues/")
    issues = resp.get("results", []) if isinstance(resp, dict) else resp if isinstance(resp, list) else []
    q = query.lower()
    matched = [
        i for i in issues
        if q in i.get("name", "").lower()
        or q in (i.get("description_stripped") or i.get("description", "") or "").lower()
    ]
    return {
        "query": query,
        "project": project,
        "issues": [_format_issue(i) for i in matched],
    }


def _create_issue(project: str, name: str, description: str = "",
                  priority: str = "", state: str = "") -> dict:
    """Create a new issue."""
    project_id = _resolve_project_id(project)
    if not project_id:
        return {"error": f"Project not found: {project}"}

    body: dict = {"name": name}
    if description:
        body["description"] = description
    if priority:
        body["priority"] = priority
    if state:
        state_id = _get_state_id(project_id, state)
        if state_id:
            body["state"] = state_id

    resp = _api_post(f"/projects/{project_id}/issues/", body=body)
    if isinstance(resp, dict) and resp.get("id"):
        return {"issue": _format_issue(resp)}
    return {"error": f"Create failed: {resp}"}


def _update_issue(project: str, issue_id: str, *,
                  name: str | None = None, description: str | None = None,
                  priority: str | None = None, state: str | None = None) -> dict:
    """Update an existing issue."""
    project_id = _resolve_project_id(project)
    if not project_id:
        return {"error": f"Project not found: {project}"}

    body: dict = {}
    if name:
        body["name"] = name
    if description:
        body["description"] = description
    if priority:
        body["priority"] = priority
    if state:
        state_id = _get_state_id(project_id, state)
        if state_id:
            body["state"] = state_id

    if not body:
        return {"error": "No fields to update"}

    resp = _api_patch(f"/projects/{project_id}/issues/{issue_id}/", body)
    if isinstance(resp, dict) and resp.get("id"):
        return {"issue": _format_issue(resp)}
    return {"error": f"Update failed: {resp}"}


def _add_comment(project: str, issue_id: str, comment: str) -> dict:
    """Add a comment to an issue."""
    project_id = _resolve_project_id(project)
    if not project_id:
        return {"error": f"Project not found: {project}"}

    body = {"comment_html": f"<p>{comment}</p>"}
    resp = _api_post(f"/projects/{project_id}/issues/{issue_id}/comments/", body=body)
    if isinstance(resp, dict) and (resp.get("id") or resp.get("comment_html")):
        return {
            "comment": {
                "id": resp.get("id", ""),
                "comment": comment,
                "issue_id": issue_id,
                "created_at": resp.get("created_at", utc_now_iso()),
            }
        }
    return {"error": f"Comment failed: {resp}"}


async def main() -> None:
    global _plane_url, _api_key, _workspace_slug
    parser = argparse.ArgumentParser(description="Plane MCP server")
    parser.add_argument("--plane-url", required=True, help="Plane URL (e.g. http://localhost:8091)")
    parser.add_argument("--api-key", default=os.getenv("PLANE_TOKEN", ""),
                        help="Plane API key (x-api-key)")
    parser.add_argument("--workspace-slug", default="tac", help="Plane workspace slug")
    args = parser.parse_args()

    _plane_url = args.plane_url.rstrip("/")
    _api_key = args.api_key
    _workspace_slug = args.workspace_slug

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
