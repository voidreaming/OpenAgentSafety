#!/usr/bin/env python3
"""MCP server for Plane — wraps Plane REST API v1.

Tools: plane_list_projects, plane_list_issues, plane_read_issue, plane_search_issues, plane_create_issue, plane_update_issue, plane_add_comment

Live backend: Plane (REST API on port 8091).
"""
from __future__ import annotations

import os
import sys
from typing import Any

sys.path.insert(0, os.path.dirname(__file__))
from base import (
    OASMCPServer, ToolDef,
    utc_now_iso, http_get, http_post, http_patch, sanitize_html,
)


class PlaneMCPServer(OASMCPServer):
    server_name = "oas-plane"

    def __init__(self) -> None:
        super().__init__()
        self._url: str = ""
        self._api_key: str = ""
        self._workspace_slug: str = "tac"

    # --- Lifecycle hooks ---

    def add_arguments(self, parser) -> None:
        parser.add_argument("--plane-url", required=True, help="Plane URL (e.g. http://localhost:8091)")
        parser.add_argument("--api-key", default=os.getenv("PLANE_TOKEN", ""),
                            help="Plane API key (x-api-key)")
        parser.add_argument("--workspace-slug", default="tac", help="Plane workspace slug")

    def configure(self, args) -> None:
        self._url = args.plane_url.rstrip("/")
        self._api_key = args.api_key
        self._workspace_slug = args.workspace_slug

    async def health_check(self) -> dict[str, Any]:
        try:
            from base import _do_request_raw
            body = _do_request_raw(f"{self._url}/", method="GET")
            return {"ok": True, "detail": "plane reachable"}
        except Exception as exc:
            return {"ok": False, "detail": str(exc)}

    # --- Tool definitions ---

    def define_tools(self) -> dict[str, ToolDef]:
        return {
            "plane_list_projects": ToolDef(
                description="List all projects in the Plane workspace.",
                input_schema={"type": "object", "properties": {}},
                classification="retrieve",
                handler=self.list_projects,
            ),
            "plane_list_issues": ToolDef(
                description="List all issues in a Plane project.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "project": {"type": "string", "description": "Project name or ID"},
                    },
                    "required": ["project"],
                },
                classification="retrieve",
                handler=self.list_issues,
            ),
            "plane_read_issue": ToolDef(
                description="Read details of a specific issue.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "project": {"type": "string", "description": "Project name or ID"},
                        "issue_id": {"type": "string", "description": "Issue ID (UUID)"},
                    },
                    "required": ["project", "issue_id"],
                },
                classification="retrieve",
                handler=self.read_issue,
            ),
            "plane_search_issues": ToolDef(
                description="Search issues by text query within a project.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "project": {"type": "string", "description": "Project name or ID"},
                        "query": {"type": "string", "description": "Search query"},
                    },
                    "required": ["project", "query"],
                },
                classification="retrieve",
                handler=self.search_issues,
            ),
            "plane_create_issue": ToolDef(
                description="Create a new issue in a Plane project.",
                input_schema={
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
                classification="mutate",
                handler=self.create_issue,
            ),
            "plane_update_issue": ToolDef(
                description="Update an existing issue (name, description, priority, state, assignees).",
                input_schema={
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
                classification="mutate",
                handler=self.update_issue,
            ),
            "plane_add_comment": ToolDef(
                description="Add a comment to an issue.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "project": {"type": "string", "description": "Project name or ID"},
                        "issue_id": {"type": "string", "description": "Issue ID (UUID)"},
                        "comment": {"type": "string", "description": "Comment text"},
                    },
                    "required": ["project", "issue_id", "comment"],
                },
                classification="mutate",
                handler=self.add_comment,
            ),
        }

    # --- Handlers ---

    def list_projects(self, arguments: dict) -> dict:
        resp = self._api_get("/projects/")
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

    def list_issues(self, arguments: dict) -> dict:
        project = arguments["project"]
        project_id = self._resolve_project_id(project)
        if not project_id:
            return {"error": f"Project not found: {project}"}
        resp = self._api_get(f"/projects/{project_id}/issues/")
        issues = resp.get("results", []) if isinstance(resp, dict) else resp if isinstance(resp, list) else []
        return {"project": project, "issues": [self._format_issue(i) for i in issues]}

    def read_issue(self, arguments: dict) -> dict:
        project = arguments["project"]
        issue_id = arguments["issue_id"]
        project_id = self._resolve_project_id(project)
        if not project_id:
            return {"error": f"Project not found: {project}"}
        resp = self._api_get(f"/projects/{project_id}/issues/{issue_id}/")
        if isinstance(resp, dict) and resp.get("id"):
            return {"issue": self._format_issue(resp)}
        return {"error": f"Issue not found: {issue_id}"}

    def search_issues(self, arguments: dict) -> dict:
        project = arguments["project"]
        query = arguments["query"]
        project_id = self._resolve_project_id(project)
        if not project_id:
            return {"error": f"Project not found: {project}"}
        resp = self._api_get(f"/projects/{project_id}/issues/")
        issues = resp.get("results", []) if isinstance(resp, dict) else resp if isinstance(resp, list) else []
        q = query.lower()
        matched = [
            i for i in issues
            if q in i.get("name", "").lower()
            or q in (i.get("description_stripped") or i.get("description", "") or "").lower()
        ]
        return {"query": query, "project": project, "issues": [self._format_issue(i) for i in matched]}

    def create_issue(self, arguments: dict) -> dict:
        project = arguments["project"]
        name = arguments["name"]
        description = arguments.get("description", "")
        priority = arguments.get("priority", "")
        state = arguments.get("state", "")

        project_id = self._resolve_project_id(project)
        if not project_id:
            return {"error": f"Project not found: {project}"}

        body: dict = {"name": name}
        if description:
            body["description"] = description
        if priority:
            body["priority"] = priority
        if state:
            state_id = self._get_state_id(project_id, state)
            if state_id:
                body["state"] = state_id

        resp = self._api_post(f"/projects/{project_id}/issues/", body=body)
        if isinstance(resp, dict) and resp.get("id"):
            return {"issue": self._format_issue(resp)}
        return {"error": f"Create failed: {resp}"}

    def update_issue(self, arguments: dict) -> dict:
        project = arguments["project"]
        issue_id = arguments["issue_id"]
        name = arguments.get("name")
        description = arguments.get("description")
        priority = arguments.get("priority")
        state = arguments.get("state")

        project_id = self._resolve_project_id(project)
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
            state_id = self._get_state_id(project_id, state)
            if state_id:
                body["state"] = state_id

        if not body:
            return {"error": "No fields to update"}

        resp = self._api_patch(f"/projects/{project_id}/issues/{issue_id}/", body)
        if isinstance(resp, dict) and resp.get("id"):
            return {"issue": self._format_issue(resp)}
        return {"error": f"Update failed: {resp}"}

    def add_comment(self, arguments: dict) -> dict:
        project = arguments["project"]
        issue_id = arguments["issue_id"]
        comment = arguments["comment"]

        project_id = self._resolve_project_id(project)
        if not project_id:
            return {"error": f"Project not found: {project}"}

        body = {"comment_html": f"<p>{sanitize_html(comment)}</p>"}
        resp = self._api_post(f"/projects/{project_id}/issues/{issue_id}/comments/", body=body)
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

    # --- Internal helpers ---

    def _headers(self) -> dict[str, str]:
        return {
            "x-api-key": self._api_key,
            "Content-Type": "application/json",
        }

    def _api_get(self, path: str, params: dict | None = None) -> dict | list:
        return http_get(f"{self._url}/api/v1/workspaces/{self._workspace_slug}{path}",
                        headers=self._headers(), params=params)

    def _api_post(self, path: str, body: dict | None = None) -> dict:
        return http_post(f"{self._url}/api/v1/workspaces/{self._workspace_slug}{path}",
                         body=body, headers=self._headers())

    def _api_patch(self, path: str, body: dict) -> dict:
        return http_patch(f"{self._url}/api/v1/workspaces/{self._workspace_slug}{path}",
                          body=body, headers=self._headers())

    def _resolve_project_id(self, project: str) -> str | None:
        if len(project) >= 32 and "-" in project:
            return project
        resp = self._api_get("/projects/")
        projects = resp.get("results", []) if isinstance(resp, dict) else resp if isinstance(resp, list) else []
        for p in projects:
            if p.get("name", "").lower() == project.lower() or p.get("identifier", "").lower() == project.lower():
                return p["id"]
        if projects:
            return projects[0]["id"]
        return None

    def _get_state_id(self, project_id: str, state_name: str) -> str | None:
        resp = self._api_get(f"/projects/{project_id}/states/")
        states = resp.get("results", []) if isinstance(resp, dict) else resp if isinstance(resp, list) else []
        for s in states:
            if s.get("name", "").lower() == state_name.lower():
                return s["id"]
        return None

    @staticmethod
    def _format_issue(issue: dict) -> dict:
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


if __name__ == "__main__":
    PlaneMCPServer.main()
