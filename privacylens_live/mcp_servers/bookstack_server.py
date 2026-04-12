"""BookStack MCP server — wiki / knowledge base tools.

Provides page CRUD, search, and permission management via BookStack REST API.

Error handling
--------------
Tools here intentionally do not catch exceptions. The shared HTTP helpers in
``base.py`` translate every httpx failure into a ``ToolError`` carrying the
status code and a body excerpt, so the agent receives a recoverable error
observation (``isError=True``) with enough context to retry with different
arguments. See ``base.py`` for the full error contract.
"""

from __future__ import annotations

import os
from typing import Annotated

from base import (
    HTTPToolError,
    http_delete,
    http_get,
    http_get_params,
    http_post,
    http_put,
    logger,
)
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import Field


BOOKSTACK_URL = os.environ.get("BOOKSTACK_URL", "http://bookstack:80")
TOKEN_ID = os.environ.get("BOOKSTACK_TOKEN_ID", "")
TOKEN_SECRET = os.environ.get("BOOKSTACK_TOKEN_SECRET", "")

mcp = FastMCP("bookstack")


def _headers() -> dict:
    return {"Authorization": f"Token {TOKEN_ID}:{TOKEN_SECRET}"}


# ── Default book ──
# BookStack requires pages to belong to a book. We use a single workspace
# book, looked up by name on every call so the server stays stateless across
# container restarts and across runs that may have left duplicate books from
# the seeder.


async def _ensure_book() -> int:
    # Check if workspace book already exists. Any HTTP error here propagates
    # as a ToolError — we deliberately don't fall through to "create" on a
    # listing failure, because that would mask auth/connectivity problems by
    # silently creating duplicate books.
    data = await http_get_params(
        f"{BOOKSTACK_URL}/api/books",
        params={"count": 100},
        headers=_headers(),
    )
    for book in data.get("data", []):
        if book.get("name") == "Workspace":
            return int(book["id"])

    # Otherwise create it.
    data = await http_post(
        f"{BOOKSTACK_URL}/api/books",
        json_data={"name": "Workspace", "description": "PrivacyLens workspace"},
        headers=_headers(),
    )
    book_id = int(data["id"])
    logger.info(f"Created workspace book id={book_id}")
    return book_id


# ── Tools ──


@mcp.tool()
async def search_pages(
    query: Annotated[
        str,
        Field(
            description=(
                "Keyword to match against page titles and body. "
                "Plain text; partial matches are supported. "
                "Parameter name is `query` (not `keyword`)."
            )
        ),
    ],
) -> dict:
    """Search pages by keyword. Returns matching pages with page_id, name, and snippet.

    The search-term parameter is named ``query`` (not ``keyword``).

    An empty result list is a normal outcome (no matches), not an error.
    """
    data = await http_get_params(
        f"{BOOKSTACK_URL}/api/search",
        params={"query": query, "count": 20},
        headers=_headers(),
    )
    results = [
        {
            "page_id": item["id"],
            "name": item["name"],
            "snippet": item.get("preview_html", {}).get("content", ""),
        }
        for item in data.get("data", [])
        if item.get("type") == "page"
    ]
    return {"results": results}


@mcp.tool()
async def get_page(
    page_id: Annotated[
        int,
        Field(
            description=(
                "Numeric page ID, as returned by list_pages or search_pages "
                "(the `page_id` field)."
            )
        ),
    ],
) -> dict:
    """Read a specific page.

    Pass the page_id from list_pages or search_pages results.
    """
    try:
        data = await http_get(
            f"{BOOKSTACK_URL}/api/pages/{page_id}",
            headers=_headers(),
        )
    except HTTPToolError as exc:
        if exc.status_code == 404:
            raise ToolError(
                f"Page '{page_id}' not found in BookStack. "
                "Use list_pages or search_pages to discover valid page IDs."
            ) from exc
        raise
    return {
        "page_id": data["id"],
        "name": data["name"],
        "markdown": data.get("markdown", data.get("html", "")),
        "tags": [t.get("value") or t.get("name", "") for t in data.get("tags", [])],
    }


@mcp.tool()
async def create_page(
    name: Annotated[str, Field(description="Page title.")],
    markdown: Annotated[str, Field(description="Page body in Markdown.")],
    tags: Annotated[
        list[str] | None,
        Field(
            description=(
                "Optional list of tag values to attach to the page. "
                "Pass None or omit for an untagged page."
            )
        ),
    ] = None,
) -> dict:
    """Create a new page with the given title and markdown content.

    All pages are created in a single shared 'Workspace' book, which is
    created lazily on first call. Returns the new page_id.
    """
    book_id = await _ensure_book()
    tag_list = [{"name": t} for t in (tags or [])]
    data = await http_post(
        f"{BOOKSTACK_URL}/api/pages",
        json_data={
            "book_id": book_id,
            "name": name,
            "markdown": markdown,
            "tags": tag_list,
        },
        headers=_headers(),
    )
    return {"page_id": data["id"], "name": data["name"]}


@mcp.tool()
async def update_page(
    page_id: Annotated[
        int,
        Field(
            description=(
                "Numeric page ID of an existing page, from list_pages or search_pages."
            )
        ),
    ],
    markdown: Annotated[
        str,
        Field(
            description=(
                "New page body in Markdown. Replaces the existing content "
                "entirely; this is not a patch."
            )
        ),
    ],
) -> dict:
    """Update the content of an existing page.

    Pass the page_id from list_pages or search_pages.
    """
    try:
        await http_put(
            f"{BOOKSTACK_URL}/api/pages/{page_id}",
            json_data={"markdown": markdown},
            headers=_headers(),
        )
    except HTTPToolError as exc:
        if exc.status_code == 404:
            raise ToolError(
                f"Page '{page_id}' not found in BookStack. "
                "Use list_pages or search_pages to discover valid page IDs."
            ) from exc
        raise
    return {"success": True, "page_id": page_id}


@mcp.tool()
async def list_pages() -> dict:
    """List all pages in the workspace.

    Returns up to 500 pages. Each entry has a page_id (use with get_page,
    update_page, or delete_page) and a name.
    """
    data = await http_get_params(
        f"{BOOKSTACK_URL}/api/pages",
        params={"count": 500},
        headers=_headers(),
    )
    pages = [
        {
            "page_id": p["id"],
            "name": p["name"],
        }
        for p in data.get("data", [])
    ]
    return {"pages": pages}


@mcp.tool()
async def delete_page(
    page_id: Annotated[
        int,
        Field(
            description=("Numeric page ID, as returned by list_pages or search_pages.")
        ),
    ],
) -> dict:
    """Delete a page. Pass the page_id from list_pages or search_pages."""
    try:
        await http_delete(
            f"{BOOKSTACK_URL}/api/pages/{page_id}",
            headers=_headers(),
        )
    except HTTPToolError as exc:
        if exc.status_code == 404:
            raise ToolError(
                f"Page '{page_id}' not found in BookStack. "
                "Use list_pages or search_pages to discover valid page IDs."
            ) from exc
        raise
    return {"success": True, "page_id": page_id}


if __name__ == "__main__":
    logger.info(f"Starting BookStack MCP server (url={BOOKSTACK_URL})")
    mcp.run(transport="streamable-http", host="0.0.0.0", port=8080)
