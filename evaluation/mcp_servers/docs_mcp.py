#!/usr/bin/env python3
"""MCP server for Docs/Notion — wraps Wiki.js GraphQL API.

Tools: search, read, write

Live backend: Wiki.js (GraphQL on port 3001).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from base import utc_now_iso, http_post

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

_wikijs_url: str = ""       # e.g. http://the-agent-company.com:3001
_wikijs_token: str = ""     # API bearer token

server = Server("oas-docs")


def _gql(query: str, variables: dict | None = None) -> dict:
    """Execute a GraphQL query against Wiki.js."""
    payload = {"query": query}
    if variables:
        payload["variables"] = variables
    return http_post(
        f"{_wikijs_url}/graphql",
        body=payload,
        headers={"Authorization": f"Bearer {_wikijs_token}"} if _wikijs_token else {},
    )


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="search",
            description="Search documents or notes by query.",
            inputSchema={
                "type": "object",
                "properties": {"query": {"type": "string", "description": "Search query"}},
                "required": ["query"],
            },
        ),
        Tool(
            name="read",
            description="Read a specific document by ID.",
            inputSchema={
                "type": "object",
                "properties": {"doc_id": {"type": "string", "description": "Document ID"}},
                "required": ["doc_id"],
            },
        ),
        Tool(
            name="write",
            description="Write content to a document.",
            inputSchema={
                "type": "object",
                "properties": {
                    "doc_id": {"type": "string", "description": "Document ID (path)"},
                    "content": {"type": "string", "description": "Document content"},
                    "title": {"type": "string", "description": "Document title"},
                },
                "required": ["doc_id", "content"],
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    if name == "search":
        result = _search(arguments.get("query", ""))
    elif name == "read":
        result = _read(arguments["doc_id"])
    elif name == "write":
        result = _write(
            arguments["doc_id"],
            arguments["content"],
            arguments.get("title", ""),
        )
    else:
        result = {"error": f"Unknown tool: {name}"}
    return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]


def _search(query: str) -> dict:
    """Search pages in Wiki.js via GraphQL."""
    result = _gql(
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


def _read(doc_id: str) -> dict:
    """Read a page from Wiki.js by ID."""
    try:
        page_id = int(doc_id)
        query = """
            query ($id: Int!) {
              pages { single(id: $id) { id, path, title, content, updatedAt } }
            }
        """
        variables = {"id": page_id}
    except ValueError:
        # Treat as path — list pages and find by path
        query = """
            query { pages { list { id, path, title, updatedAt } } }
        """
        variables = None

    result = _gql(query, variables)

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
                return _read(str(page["id"]))
        return {"error": f"Document not found: {doc_id}"}


def _write(doc_id: str, content: str, title: str = "") -> dict:
    """Create or update a page in Wiki.js."""
    try:
        page_id = int(doc_id)
        # Update existing page
        result = _gql(
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
    result = _gql(
        """
        mutation ($content: String!, $path: String!, $title: String!) {
          pages {
            create(content: $content, path: $path, title: $title,
                   editor: "markdown", locale: "en", isPublished: true,
                   isPrivate: false, tags: []) {
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


async def main() -> None:
    global _wikijs_url, _wikijs_token
    parser = argparse.ArgumentParser(description="Docs MCP server")
    parser.add_argument("--wikijs-url", required=True, help="Wiki.js URL (e.g. http://localhost:3001)")
    parser.add_argument("--wikijs-token", default="", help="Wiki.js API bearer token")
    args = parser.parse_args()

    _wikijs_url = args.wikijs_url.rstrip("/")
    _wikijs_token = args.wikijs_token

    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream)


if __name__ == "__main__":
    asyncio.run(main())
