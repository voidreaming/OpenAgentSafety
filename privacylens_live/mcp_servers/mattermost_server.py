"""Mattermost MCP server — direct messaging tools.

Provides DM send/receive, message search, file upload, and user listing
via Mattermost REST API v4.

Error handling
--------------
All upstream HTTP calls go through the shared ``base.http_*`` helpers, which
translate every httpx failure into a ``HTTPToolError`` carrying the upstream
status code. Tools wrap their work in :func:`_authenticated_call`, which:

- supplies fresh auth headers from the cached login
- on a confirmed ``HTTP 401`` response, clears the auth cache, re-logs in, and
  retries the call exactly once before propagating the error

Business-logic failures (e.g. an unknown recipient) raise :class:`ToolError`
directly with an actionable recovery hint that names the tool the agent can
use to discover valid inputs.
"""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable
from typing import Annotated, Any

from base import (
    HTTPToolError,
    http_get,
    http_get_params,
    http_post,
    http_post_for_login,
    logger,
)
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import Field


MATTERMOST_URL = os.environ.get("MATTERMOST_URL", "http://mattermost:8065")
MM_USER = os.environ.get("MATTERMOST_USER", "admin")
MM_PASSWORD = os.environ.get("MATTERMOST_PASSWORD", "admin")

API = f"{MATTERMOST_URL}/api/v4"

mcp = FastMCP("mattermost")

# ── Auth cache ──
# Module-level so the helpers below can clear it on a 401 retry.
_auth: dict[str, str] = {}  # {"token": ..., "user_id": ...}


# ── Auth helpers ──


async def _login() -> dict[str, str]:
    """Log in (or return cached creds) and populate ``_auth``.

    Mattermost returns the bearer token in the ``Token`` *response header*
    rather than the JSON body, so we use ``http_post_for_login`` to get both.
    """
    if _auth.get("token"):
        return _auth
    body, headers = await http_post_for_login(
        f"{API}/users/login",
        json_data={"login_id": MM_USER, "password": MM_PASSWORD},
    )
    token = headers.get("token") or headers.get("Token")
    if not token:
        raise ToolError(
            "Mattermost login response missing 'Token' header. "
            "Check MATTERMOST_USER / MATTERMOST_PASSWORD credentials."
        )
    _auth["token"] = token
    _auth["user_id"] = body["id"]
    return _auth


def _auth_headers(auth: dict[str, str]) -> dict[str, str]:
    return {"Authorization": f"Bearer {auth['token']}"}


async def _authenticated_call[T](
    fn: Callable[[dict[str, str]], Awaitable[T]],
) -> T:
    """Run ``fn(headers)`` with cached auth, refreshing on a single 401.

    The retry is intentionally narrow: we only refresh when the upstream
    explicitly responds with HTTP 401 (a confirmed auth rejection). Network
    errors, 5xx, and other 4xx propagate as-is. The retry happens at most
    once; a second 401 surfaces normally so the agent can see the failure.
    """
    auth = await _login()
    try:
        return await fn(_auth_headers(auth))
    except HTTPToolError as exc:
        if exc.status_code != 401:
            raise
        logger.info(
            "Mattermost auth token rejected (HTTP 401); "
            "clearing cache and retrying once."
        )
        _auth.clear()
        auth = await _login()
        return await fn(_auth_headers(auth))


async def _resolve_user_id(username: str, headers: dict[str, str]) -> str:
    """Resolve a Mattermost username to a user ID, with a friendly miss message."""
    try:
        data = await http_get(f"{API}/users/username/{username}", headers=headers)
    except HTTPToolError as exc:
        if exc.status_code == 404:
            raise ToolError(
                f"User '{username}' not found in Mattermost. "
                "Use list_users to discover valid usernames."
            ) from exc
        raise
    return data["id"]


async def _get_or_create_dm(
    user_id: str, other_user_id: str, headers: dict[str, str]
) -> str:
    """Get or create a direct-message channel between two users."""
    dm = await http_post(
        f"{API}/channels/direct",
        json_data=[user_id, other_user_id],
        headers=headers,
    )
    return dm["id"]


# ── Tools ──


@mcp.tool()
async def send_message(
    recipient: Annotated[
        str,
        Field(
            description=(
                "Mattermost username (no '@' prefix), as returned by list_users. "
                "Not a display name and not an email address."
            )
        ),
    ],
    message: Annotated[str, Field(description="Plain-text message body.")],
) -> dict:
    """Send a direct message to a user.

    Pass the recipient username from list_users results.
    """

    async def _do(headers: dict[str, str]) -> dict[str, Any]:
        other_id = await _resolve_user_id(recipient, headers)
        channel_id = await _get_or_create_dm(_auth["user_id"], other_id, headers)
        return await http_post(
            f"{API}/posts",
            json_data={"channel_id": channel_id, "message": message},
            headers=headers,
        )

    post = await _authenticated_call(_do)
    return {
        "success": True,
        "message_id": post["id"],
        "recipient": recipient,
    }


@mcp.tool()
async def list_messages(
    max_count: Annotated[
        int,
        Field(
            description=(
                "Maximum number of messages to return across all DM conversations."
            ),
            ge=1,
            le=200,
        ),
    ] = 20,
) -> dict:
    """List recent direct messages across all DM conversations.

    `time` is a Mattermost epoch-milliseconds integer, not an ISO timestamp.
    `sender` is a Mattermost user ID (cleanup pending — see Phase B).
    """

    async def _do(headers: dict[str, str]) -> list[dict[str, Any]]:
        channels = await http_get(f"{API}/users/me/channels", headers=headers)
        dm_channels = [c for c in channels if c.get("type") == "D"]
        messages: list[dict[str, Any]] = []
        for ch in dm_channels[:10]:
            data = await http_get_params(
                f"{API}/channels/{ch['id']}/posts",
                params={"per_page": max_count},
                headers=headers,
            )
            order = data.get("order", [])
            posts = data.get("posts", {})
            for post_id in order[:max_count]:
                post = posts.get(post_id, {})
                if post.get("message"):
                    messages.append(
                        {
                            "message_id": post["id"],
                            "sender": post.get("user_id", ""),
                            "time": post.get("create_at", ""),
                            "text": post["message"],
                        }
                    )
        return messages

    messages = await _authenticated_call(_do)
    return {"messages": messages[:max_count]}


@mcp.tool()
async def search_messages(
    query: Annotated[
        str,
        Field(
            description=(
                "Keyword to match against message text. Space-separated terms "
                "are OR'd together."
            )
        ),
    ],
) -> dict:
    """Search messages by keyword across all conversations.

    `time` is a Mattermost epoch-milliseconds integer; `channel` is a
    Mattermost channel ID, not a channel name; `sender` is a Mattermost
    user ID (all three are cleanup items pending in Phase B).
    """

    async def _do(headers: dict[str, str]) -> dict[str, Any]:
        teams = await http_get(f"{API}/users/me/teams", headers=headers)
        if not teams:
            raise ToolError(
                "No team found for the authenticated Mattermost user. "
                "Create a team first via the admin API "
                "(POST /api/v4/teams) before searching messages."
            )
        team_id = teams[0]["id"]
        return await http_post(
            f"{API}/teams/{team_id}/posts/search",
            json_data={"terms": query, "is_or_search": True},
            headers=headers,
        )

    data = await _authenticated_call(_do)
    order = data.get("order", [])
    posts = data.get("posts", {})
    messages = []
    for post_id in order[:20]:
        post = posts.get(post_id, {})
        if post.get("message"):
            messages.append(
                {
                    "message_id": post["id"],
                    "sender": post.get("user_id", ""),
                    "time": post.get("create_at", ""),
                    "text": post["message"],
                    "channel": post.get("channel_id", ""),
                }
            )
    return {"messages": messages}


@mcp.tool()
async def upload_file(
    recipient: Annotated[
        str,
        Field(
            description=(
                "Mattermost username (no '@' prefix), as returned by list_users."
            )
        ),
    ],
    file_path: Annotated[
        str,
        Field(
            description=(
                "Path to the file to share. NOTE: this server currently sends "
                "the path as a text message rather than uploading the file's "
                "content; the file itself is not transferred. See known issues."
            )
        ),
    ],
    message: Annotated[
        str,
        Field(
            description=(
                "Optional caption to send with the file path. "
                "Defaults to 'Shared file: <path>'."
            )
        ),
    ] = "",
) -> dict:
    """Upload a file and send it as a message to a user.

    Pass the recipient username from list_users.

    Known limitation: this tool currently sends `file_path` as a text message
    rather than uploading the file's content; the file itself is not
    transferred.
    """
    msg = message or f"Shared file: {file_path}"

    async def _do(headers: dict[str, str]) -> dict[str, Any]:
        other_id = await _resolve_user_id(recipient, headers)
        channel_id = await _get_or_create_dm(_auth["user_id"], other_id, headers)
        return await http_post(
            f"{API}/posts",
            json_data={"channel_id": channel_id, "message": msg},
            headers=headers,
        )

    await _authenticated_call(_do)
    return {
        "success": True,
        "recipient": recipient,
        "file_path": file_path,
    }


@mcp.tool()
async def list_users() -> dict:
    """List all users on the server.

    Use the username field as the recipient for send_message or upload_file.
    Bot accounts are filtered out.
    """

    async def _do(headers: dict[str, str]) -> list[dict[str, Any]]:
        return await http_get_params(
            f"{API}/users",
            params={"per_page": 50},
            headers=headers,
        )

    data = await _authenticated_call(_do)
    users = [
        {
            "username": u["username"],
            "name": (f"{u.get('first_name', '')} {u.get('last_name', '')}").strip(),
            "email": u.get("email", ""),
        }
        for u in data
        if not u.get("is_bot")
    ]
    return {"users": users}


if __name__ == "__main__":
    logger.info(f"Starting Mattermost MCP server (url={MATTERMOST_URL})")
    mcp.run(transport="streamable-http", host="0.0.0.0", port=8080)
