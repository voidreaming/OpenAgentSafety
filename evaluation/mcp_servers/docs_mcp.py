#!/usr/bin/env python3
"""MCP server for Docs/Notion — wraps Wiki.js GraphQL API.

Tools: wikijs_search, wikijs_read, wikijs_write

Live backend: Wiki.js (GraphQL on port 3001).
"""
from __future__ import annotations

import os
import sys
from typing import Any

sys.path.insert(0, os.path.dirname(__file__))
from base import OASMCPServer, ToolDef, utc_now_iso, http_post, http_get


class DocsMCPServer(OASMCPServer):
    server_name = "oas-docs"

    def __init__(self) -> None:
        super().__init__()
        self._url: str = ""
        self._token: str = ""

    # --- Lifecycle hooks ---

    def add_arguments(self, parser) -> None:
        parser.add_argument("--wikijs-url", required=True, help="Wiki.js URL (e.g. http://localhost:3001)")
        parser.add_argument("--wikijs-token", default="", help="Wiki.js API bearer token")

    def configure(self, args) -> None:
        self._url = args.wikijs_url.rstrip("/")
        self._token = args.wikijs_token

    async def health_check(self) -> dict[str, Any]:
        try:
            result = self._gql("{ pages { list { id } } }")
            if "data" in result:
                return {"ok": True, "detail": "wikijs reachable"}
            return {"ok": False, "detail": f"wikijs error: {result}"}
        except Exception as exc:
            return {"ok": False, "detail": str(exc)}

    # --- Tool definitions ---

    def define_tools(self) -> dict[str, ToolDef]:
        return {
            "wikijs_search": ToolDef(
                description="Search documents or notes by query.",
                input_schema={
                    "type": "object",
                    "properties": {"query": {"type": "string", "description": "Search query"}},
                    "required": ["query"],
                },
                classification="retrieve",
                handler=self.search,
            ),
            "wikijs_read": ToolDef(
                description="Read a specific document by ID.",
                input_schema={
                    "type": "object",
                    "properties": {"doc_id": {"type": "string", "description": "Document ID"}},
                    "required": ["doc_id"],
                },
                classification="retrieve",
                handler=self.read,
            ),
            "wikijs_write": ToolDef(
                description="Write content to a document.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "doc_id": {"type": "string", "description": "Document ID (path)"},
                        "content": {"type": "string", "description": "Document content"},
                        "title": {"type": "string", "description": "Document title"},
                    },
                    "required": ["doc_id", "content"],
                },
                classification="mutate",
                handler=self.write,
            ),
        }

    # --- Handlers ---

    def search(self, arguments: dict) -> dict:
        query = arguments.get("query", "")
        result = self._gql(
            """
            query ($query: String!) {
              pages {
                search(query: $query) {
                  results { id, title, path, description }
                  totalHits
                }
              }
            }
            """,
            {"query": query or "*"},
        )
        search_data = result.get("data", {}).get("pages", {}).get("search", {})
        docs = []
        for r in search_data.get("results", []):
            docs.append({
                "doc_id": str(r.get("id", "")),
                "title": r.get("title", ""),
                "path": r.get("path", ""),
                "labels": [],
            })
        return {"query": query, "documents": docs}

    def read(self, arguments: dict) -> dict:
        doc_id = arguments["doc_id"]
        try:
            page_id = int(doc_id)
            query = """
                query ($id: Int!) {
                  pages { single(id: $id) { id, path, title, content, updatedAt } }
                }
            """
            variables = {"id": page_id}
        except ValueError:
            query = """
                query { pages { list { id, path, title, updatedAt } } }
            """
            variables = None

        result = self._gql(query, variables)

        if variables and "id" in variables:
            page = result.get("data", {}).get("pages", {}).get("single")
            if page:
                return {"document": {
                    "doc_id": str(page["id"]),
                    "title": page.get("title", ""),
                    "content": page.get("content", ""),
                    "path": page.get("path", ""),
                    "labels": [],
                    "updated_at": page.get("updatedAt", ""),
                }}
            return {"error": f"Document not found: {doc_id}"}
        else:
            pages = result.get("data", {}).get("pages", {}).get("list", [])
            for page in pages:
                if page.get("path") == doc_id:
                    return self.read({"doc_id": str(page["id"])})
            return {"error": f"Document not found: {doc_id}"}

    def write(self, arguments: dict) -> dict:
        doc_id = arguments["doc_id"]
        content = arguments["content"]
        title = arguments.get("title", "")

        try:
            page_id = int(doc_id)
            result = self._gql(
                """
                mutation ($id: Int!, $content: String!) {
                  pages {
                    update(id: $id, content: $content) {
                      responseResult { succeeded, message }
                      page { id, path, title, updatedAt }
                    }
                  }
                }
                """,
                {"id": page_id, "content": content},
            )
            page_data = result.get("data", {}).get("pages", {}).get("update", {})
            page = page_data.get("page", {})
            resp = page_data.get("responseResult", {})
            if resp.get("succeeded"):
                return {"document": {
                    "doc_id": str(page.get("id", page_id)),
                    "title": page.get("title", title or doc_id),
                    "content": content,
                    "labels": [],
                    "updated_at": page.get("updatedAt", utc_now_iso()),
                }}
            return {"error": f"Update failed: {resp.get('message', 'unknown')}"}
        except ValueError:
            pass

        # Create new page (doc_id is the path)
        result = self._gql(
            """
            mutation ($content: String!, $path: String!, $title: String!) {
              pages {
                create(content: $content, path: $path, title: $title,
                       description: "", editor: "markdown", locale: "en",
                       isPublished: true, isPrivate: false, tags: []) {
                  responseResult { succeeded, message }
                  page { id, path, title, updatedAt }
                }
              }
            }
            """,
            {"content": content, "path": doc_id, "title": title or doc_id},
        )
        page_data = result.get("data", {}).get("pages", {}).get("create", {})
        page = page_data.get("page", {})
        resp = page_data.get("responseResult", {})
        if resp.get("succeeded"):
            return {"document": {
                "doc_id": str(page.get("id", doc_id)),
                "title": page.get("title", title or doc_id),
                "content": content,
                "path": page.get("path", doc_id),
                "labels": [],
                "updated_at": page.get("updatedAt", utc_now_iso()),
            }}
        return {"error": f"Create failed: {resp.get('message', 'unknown')}"}

    # --- Internal helpers ---

    def _gql(self, query: str, variables: dict | None = None) -> dict:
        payload = {"query": query}
        if variables:
            payload["variables"] = variables
        return http_post(
            f"{self._url}/graphql",
            body=payload,
            headers={"Authorization": f"Bearer {self._token}"} if self._token else {},
        )


if __name__ == "__main__":
    DocsMCPServer.main()
