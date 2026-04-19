"""GoToSocial MCP server — social media tools.

Provides post creation, profile viewing, and search via Mastodon-compatible API.
Works with GoToSocial, Pleroma, Akkoma, or any Mastodon-API server.

Error handling
--------------
All upstream HTTP calls go through the shared ``base.http_*`` helpers, which
translate every httpx failure into a ``HTTPToolError`` carrying the upstream
status code. Unlike mattermost / rocketchat, GoToSocial uses a single static
bearer token from the environment, so there is no login flow and no 401 retry:
a 401 here means the configured ``GOTOSOCIAL_TOKEN`` is wrong or missing, and
we want that to fail loudly so the operator notices.

Business-logic failures (e.g. an unknown user_id) raise :class:`ToolError`
directly with an actionable recovery hint that names the tool the agent can
use to discover valid inputs.
"""

from __future__ import annotations

from typing import Annotated, Any

from base import (
    HTTPToolError,
    get_env,
    http_get_params,
    http_post,
    logger,
)
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import Field


GOTOSOCIAL_URL = get_env("GOTOSOCIAL_URL", "http://gotosocial:8080")
TOKEN = get_env("GOTOSOCIAL_TOKEN", "")

API_V1 = f"{GOTOSOCIAL_URL}/api/v1"
API_V2 = f"{GOTOSOCIAL_URL}/api/v2"

mcp = FastMCP("gotosocial")


def _headers() -> dict[str, str]:
    """Build request headers, including the static bearer token if configured."""
    h: dict[str, str] = {"Content-Type": "application/json"}
    if TOKEN:
        h["Authorization"] = f"Bearer {TOKEN}"
    return h


def _is_not_found(exc: HTTPToolError) -> bool:
    """A user_id miss surfaces as 404 from /accounts/{id}; treat 410 the same way."""
    return exc.status_code in (404, 410)


# ── Tools ──


@mcp.tool()
async def create_post(
    content: Annotated[
        str,
        Field(
            description=(
                "Post body. Plain text (the server may render basic markup). "
                "Parameter name is `content` (not `status`). This tool does "
                "not expose Mastodon's visibility / status / spoiler fields."
            )
        ),
    ],
) -> dict:
    """Create a new post visible to your network.

    The post-body parameter is named ``content`` (not ``status`` as in the
    raw Mastodon API). This tool takes only ``content`` — there is no
    ``visibility``, ``spoiler_text``, or ``sensitive`` parameter.
    """
    result = await http_post(
        f"{API_V1}/statuses",
        json_data={"status": content},
        headers=_headers(),
    )
    return {
        "success": True,
        "post_id": result.get("id", ""),
        "url": result.get("url", ""),
    }


@mcp.tool(annotations={"readOnlyHint": True})
async def get_profile(
    user_id: Annotated[
        str,
        Field(
            description=(
                "Account ID, as returned by search_users (the `user_id` "
                "field). This is an opaque ID, not a username."
            )
        ),
    ],
) -> dict:
    """Get a user's profile.

    Pass the user_id from search_users results.
    """
    try:
        account = await http_get_params(
            f"{API_V1}/accounts/{user_id}",
            headers=_headers(),
        )
    except HTTPToolError as exc:
        if _is_not_found(exc):
            raise ToolError(
                f"User '{user_id}' not found in GoToSocial. "
                "Use search_users to discover valid user IDs."
            ) from exc
        raise
    return {
        "user_id": account.get("id", ""),
        "username": account.get("username", ""),
        "display_name": account.get("display_name", ""),
        "bio": account.get("note", ""),
        "followers": account.get("followers_count", 0),
        "following": account.get("following_count", 0),
        "posts_count": account.get("statuses_count", 0),
    }


@mcp.tool(annotations={"readOnlyHint": True})
async def list_user_posts(
    user_id: Annotated[
        str,
        Field(
            description=(
                "Account ID, as returned by search_users (the `user_id` "
                "field). This is an opaque ID, not a username."
            )
        ),
    ],
) -> dict:
    """Get recent posts from a user.

    Pass the user_id from search_users results.

    `content` is HTML (Mastodon API convention), not plain text.
    `time` is an ISO 8601 timestamp string.
    """
    try:
        statuses = await http_get_params(
            f"{API_V1}/accounts/{user_id}/statuses",
            params={"limit": 20},
            headers=_headers(),
        )
    except HTTPToolError as exc:
        if _is_not_found(exc):
            raise ToolError(
                f"User '{user_id}' not found in GoToSocial. "
                "Use search_users to discover valid user IDs."
            ) from exc
        raise
    items: list[dict[str, Any]] = statuses if isinstance(statuses, list) else []
    posts = [
        {
            "post_id": s.get("id", ""),
            "content": s.get("content", ""),
            "time": s.get("created_at", ""),
        }
        for s in items
    ]
    return {"posts": posts}


@mcp.tool(annotations={"readOnlyHint": True})
async def search_users(
    query: Annotated[
        str,
        Field(
            description=(
                "Username or display-name fragment to search for. "
                "Partial matches are supported."
            )
        ),
    ],
) -> dict:
    """Search for users by name or username.

    Use user_id to call get_profile or list_user_posts.
    """
    results = await http_get_params(
        f"{API_V1}/accounts/search",
        params={"q": query, "limit": 10},
        headers=_headers(),
    )
    items: list[dict[str, Any]] = results if isinstance(results, list) else []
    users = [
        {
            "user_id": a.get("id", ""),
            "username": a.get("username", ""),
            "display_name": a.get("display_name", ""),
        }
        for a in items
    ]
    return {"users": users}


@mcp.tool(annotations={"readOnlyHint": True})
async def search_posts(
    query: Annotated[
        str,
        Field(description="Keyword to match against post content."),
    ],
) -> dict:
    """Search posts by keyword.

    `content` is HTML (Mastodon API convention), not plain text.
    `time` is an ISO 8601 timestamp string. `author` is the author's
    username; `user_id` is the author's account ID — pass `user_id` to
    get_profile or list_user_posts to follow the chain.
    """
    data = await http_get_params(
        f"{API_V2}/search",
        params={"q": query, "type": "statuses", "limit": 20},
        headers=_headers(),
    )
    posts = [
        {
            "post_id": s.get("id", ""),
            "content": s.get("content", ""),
            "author": s.get("account", {}).get("username", ""),
            "user_id": s.get("account", {}).get("id", ""),
            "time": s.get("created_at", ""),
        }
        for s in data.get("statuses", [])
    ]
    return {"posts": posts}


if __name__ == "__main__":
    logger.info(f"Starting GoToSocial MCP server (url={GOTOSOCIAL_URL})")
    mcp.run(transport="streamable-http", host="0.0.0.0", port=8080)
