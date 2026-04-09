#!/usr/bin/env python3
"""MCP server for GitLab — wraps GitLab REST API v4.

Tools: gitlab_list_projects, gitlab_list_files, gitlab_read_file, gitlab_search_code

Live backend: GitLab CE (REST API on port 8929).
"""
from __future__ import annotations

import base64
import os
import sys
from typing import Any
from urllib.parse import quote

sys.path.insert(0, os.path.dirname(__file__))
from base import OASMCPServer, ToolDef, http_get


class GitLabMCPServer(OASMCPServer):
    server_name = "oas-gitlab"

    def __init__(self) -> None:
        super().__init__()
        self._url: str = ""
        self._token: str = ""

    # --- Lifecycle hooks ---

    def add_arguments(self, parser) -> None:
        parser.add_argument("--gitlab-url", required=True, help="GitLab URL (e.g. http://localhost:8929)")
        parser.add_argument("--private-token", default=os.getenv("GITLAB_TOKEN", ""), help="GitLab PRIVATE-TOKEN")

    def configure(self, args) -> None:
        self._url = args.gitlab_url.rstrip("/")
        self._token = args.private_token

    async def health_check(self) -> dict[str, Any]:
        try:
            from base import _do_request_raw
            body = _do_request_raw(f"{self._url}/-/health", method="GET")
            if "ok" in body.lower() or "alive" in body.lower():
                return {"ok": True, "detail": "gitlab reachable"}
            return {"ok": False, "detail": f"gitlab health: {body[:200]}"}
        except Exception as exc:
            return {"ok": False, "detail": str(exc)}

    # --- Tool definitions ---

    def define_tools(self) -> dict[str, ToolDef]:
        return {
            "gitlab_list_projects": ToolDef(
                description="List all GitLab projects.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "search": {"type": "string", "description": "Filter projects by name (optional)"},
                    },
                },
                classification="retrieve",
                handler=self.list_projects,
            ),
            "gitlab_list_files": ToolDef(
                description="List files and directories in a GitLab repository.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "project": {"type": "string", "description": "Project name or ID"},
                        "path": {"type": "string", "description": "Directory path (default: root)"},
                        "ref": {"type": "string", "description": "Branch or tag (default: main)"},
                    },
                    "required": ["project"],
                },
                classification="retrieve",
                handler=self.list_files,
            ),
            "gitlab_read_file": ToolDef(
                description="Read the content of a file from a GitLab repository.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "project": {"type": "string", "description": "Project name or ID"},
                        "path": {"type": "string", "description": "File path in the repository"},
                        "ref": {"type": "string", "description": "Branch or tag (default: main)"},
                    },
                    "required": ["project", "path"],
                },
                classification="retrieve",
                handler=self.read_file,
            ),
            "gitlab_search_code": ToolDef(
                description="Search for code across GitLab repositories.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Search query"},
                        "project": {"type": "string", "description": "Limit search to a specific project (optional)"},
                    },
                    "required": ["query"],
                },
                classification="retrieve",
                handler=self.search_code,
            ),
        }

    # --- Handlers ---

    def list_projects(self, arguments: dict) -> dict:
        search = arguments.get("search", "")
        params: dict = {"per_page": "50"}
        if search:
            params["search"] = search
        projects = self._api("/projects", params=params)
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

    def list_files(self, arguments: dict) -> dict:
        project = arguments["project"]
        path = arguments.get("path", "")
        ref = arguments.get("ref", "")

        project_id = self._resolve_project_id(project)
        if project_id is None:
            return {"error": f"Project not found: {project}"}

        params: dict = {"per_page": "100"}
        if path:
            params["path"] = path
        if ref:
            params["ref"] = ref

        tree = self._api(f"/projects/{project_id}/repository/tree", params=params)
        if not isinstance(tree, list):
            return {"error": f"Failed to list files: {tree}"}
        return {
            "project": project,
            "path": path or "/",
            "entries": [
                {
                    "name": entry.get("name", ""),
                    "type": entry.get("type", ""),
                    "path": entry.get("path", ""),
                }
                for entry in tree
            ],
        }

    def read_file(self, arguments: dict) -> dict:
        project = arguments["project"]
        path = arguments["path"]
        ref = arguments.get("ref", "")

        project_id = self._resolve_project_id(project)
        if project_id is None:
            return {"error": f"Project not found: {project}"}

        encoded_path = quote(path, safe="")
        params: dict = {}
        if ref:
            params["ref"] = ref

        file_data = self._api(f"/projects/{project_id}/repository/files/{encoded_path}", params=params)
        if isinstance(file_data, dict) and "content" in file_data:
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

    def search_code(self, arguments: dict) -> dict:
        query = arguments["query"]
        project = arguments.get("project", "")

        params: dict = {"scope": "blobs", "search": query, "per_page": "20"}
        if project:
            project_id = self._resolve_project_id(project)
            if project_id is None:
                return {"error": f"Project not found: {project}"}
            results = self._api(f"/projects/{project_id}/search", params=params)
        else:
            results = self._api("/search", params=params)

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

    # --- Internal helpers ---

    def _api(self, path: str, params: dict | None = None) -> dict | list:
        headers = {"PRIVATE-TOKEN": self._token} if self._token else {}
        return http_get(f"{self._url}/api/v4{path}", headers=headers, params=params)

    def _resolve_project_id(self, project: str) -> int | None:
        try:
            return int(project)
        except ValueError:
            pass
        projects = self._api("/projects", params={"search": project, "per_page": "20"})
        if isinstance(projects, list):
            for p in projects:
                if p.get("name", "").lower() == project.lower() or p.get("path_with_namespace", "").lower() == project.lower():
                    return p["id"]
            if projects:
                return projects[0]["id"]
        return None


if __name__ == "__main__":
    GitLabMCPServer.main()
