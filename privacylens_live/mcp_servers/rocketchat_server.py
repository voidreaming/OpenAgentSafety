"""RocketChat MCP server — team chat / channel tools.

Provides channel messaging, search, and user info via RocketChat REST API.

Error handling
--------------
All upstream HTTP calls go through the shared ``base.http_*`` helpers, which
translate every httpx failure into a ``HTTPToolError`` carrying the upstream
status code. Tools wrap their work in :func:`_authenticated_call`, which:

- supplies fresh auth headers from the cached login
- on a confirmed ``HTTP 401`` response, clears the auth cache, re-logs in, and
  retries the call exactly once before propagating the error

Business-logic failures (e.g. an unknown channel) raise :class:`ToolError`
directly with an actionable recovery hint that names the tool the agent can
use to discover valid inputs.

The aggregate :func:`search_messages` tool intentionally tolerates per-channel
failures (logging them as warnings) so a single permission-denied or broken
channel does not fail the whole search. The top-level channel-list call is
*not* tolerated — if listing fails, the tool errors out normally.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
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


ROCKETCHAT_URL = get_env("ROCKETCHAT_URL", "http://rocketchat:3000")
RC_USER = get_env("ROCKETCHAT_USER", "admin")
RC_PASSWORD = get_env("ROCKETCHAT_PASSWORD", "admin")

API = f"{ROCKETCHAT_URL}/api/v1"

mcp = FastMCP("rocketchat")

# ── Auth cache ──
# Module-level so the helpers below can clear it on a 401 retry.
_auth: dict[str, str] = {}  # {"X-Auth-Token": ..., "X-User-Id": ...}


# ── Auth helpers ──


async def _login() -> dict[str, str]:
    """Log in (or return cached creds) and populate ``_auth``.

    RocketChat returns the auth token and user ID in the JSON body under
    ``data.authToken`` / ``data.userId``, so a plain ``http_post`` suffices.
    """
    if _auth.get("X-Auth-Token"):
        return _auth
    body = await http_post(
        f"{API}/login",
        json_data={"user": RC_USER, "password": RC_PASSWORD},
    )
    data = body.get("data") or {}
    token = data.get("authToken")
    user_id = data.get("userId")
    if not token or not user_id:
        raise ToolError(
            "RocketChat login response missing 'authToken'/'userId'. "
            "Check ROCKETCHAT_USER / ROCKETCHAT_PASSWORD credentials."
        )
    _auth["X-Auth-Token"] = token
    _auth["X-User-Id"] = user_id
    return _auth


def _auth_headers(auth: dict[str, str]) -> dict[str, str]:
    return {
        "X-Auth-Token": auth["X-Auth-Token"],
        "X-User-Id": auth["X-User-Id"],
    }


async def _authenticated_call[T](
    fn: Callable[[dict[str, str]], Awaitable[T]],
) -> T:
    """Run ``fn(headers)`` with cached auth, refreshing on a single 401.

    Mirrors the pattern in mattermost_server: only refresh on a confirmed
    HTTP 401, retry at most once, then surface the error normally.
    """
    auth = await _login()
    try:
        return await fn(_auth_headers(auth))
    except HTTPToolError as exc:
        if exc.status_code != 401:
            raise
        logger.info(
            "RocketChat auth token rejected (HTTP 401); "
            "clearing cache and retrying once."
        )
        _auth.clear()
        auth = await _login()
        return await fn(_auth_headers(auth))


async def _resolve_room_id(channel: str, headers: dict[str, str]) -> str:
    """Resolve a name to a RocketChat room ID, treating it as either a DM
    target (username) or a channel name. Raises ``ToolError`` with a recovery
    hint if both lookups fail.

    Lookup-style HTTP failures (4xx other than 401) are *not* fatal — they
    indicate the probe missed, so we fall through to the next probe. Network
    errors and 5xx propagate immediately.
    """
    # Probe 1: treat as username, try to open a DM channel.
    try:
        dm = await http_post(
            f"{API}/im.create",
            json_data={"username": channel},
            headers=headers,
        )
        room_id = (dm.get("room") or {}).get("_id")
        if room_id:
            return room_id
    except HTTPToolError as exc:
        if exc.status_code is None or exc.status_code >= 500:
            raise
        logger.debug(
            f"_resolve_room_id: DM probe for '{channel}' returned "
            f"HTTP {exc.status_code}; trying channel lookup"
        )

    # Probe 2: treat as channel name.
    try:
        ch_data = await http_get_params(
            f"{API}/channels.info",
            params={"roomName": channel},
            headers=headers,
        )
        room_id = (ch_data.get("channel") or {}).get("_id")
        if room_id:
            return room_id
    except HTTPToolError as exc:
        if exc.status_code is None or exc.status_code >= 500:
            raise
        logger.debug(
            f"_resolve_room_id: channel probe for '{channel}' returned "
            f"HTTP {exc.status_code}"
        )

    raise ToolError(
        f"Channel or user '{channel}' not found in RocketChat. "
        "Use list_channels to discover valid channels, or pass an "
        "existing username for a direct message."
    )


# ── Tools ──


@mcp.tool()
async def send_channel_message(
    channel: Annotated[
        str,
        Field(
            description=(
                "RocketChat channel name from list_channels (no '#' prefix), "
                "or a username for a direct message."
            )
        ),
    ],
    message: Annotated[str, Field(description="Plain-text message body.")],
) -> dict:
    """Send a message to a channel or user.

    Pass the channel name from list_channels results, or a username for a DM.
    """

    async def _do(headers: dict[str, str]) -> dict[str, Any]:
        room_id = await _resolve_room_id(channel, headers)
        return await http_post(
            f"{API}/chat.sendMessage",
            json_data={"message": {"rid": room_id, "msg": message}},
            headers=headers,
        )

    result = await _authenticated_call(_do)
    return {
        "success": True,
        "message_id": (result.get("message") or {}).get("_id", ""),
        "channel": channel,
    }


@mcp.tool()
async def search_messages(
    query: Annotated[
        str,
        Field(description="Keyword to match against message text."),
    ],
) -> dict:
    """Search messages by keyword across channels.

    This is an aggregate search across all visible channels. A failure on
    one channel (permission denied, transient error) is logged and skipped
    rather than failing the whole search; if listing channels itself fails,
    the tool errors out normally.

    `time` is an ISO 8601 timestamp string. `sender` is a username (matches
    the username field on get_user_info).
    """

    async def _do(headers: dict[str, str]) -> list[dict[str, Any]]:
        # Top-level channel listing is *not* tolerated — a failure here is
        # a real error.
        channels_data = await http_get_params(
            f"{API}/channels.list",
            params={"count": 50},
            headers=headers,
        )
        all_messages: list[dict[str, Any]] = []
        for ch in channels_data.get("channels", []):
            channel_name = ch.get("name") or ch["_id"]
            try:
                data = await http_get_params(
                    f"{API}/chat.search",
                    params={
                        "roomId": ch["_id"],
                        "searchText": query,
                        "count": 20,
                    },
                    headers=headers,
                )
            except HTTPToolError as exc:
                logger.warning(
                    f"search_messages: skipping channel '{channel_name}' "
                    f"due to upstream failure: {exc}"
                )
                continue
            for msg in data.get("messages", []):
                all_messages.append(
                    {
                        "message_id": msg["_id"],
                        "sender": msg.get("u", {}).get("username", ""),
                        "time": msg.get("ts", ""),
                        "text": msg.get("msg", ""),
                        "channel": channel_name,
                    }
                )
        return all_messages

    messages = await _authenticated_call(_do)
    return {"messages": messages}


@mcp.tool()
async def list_channels() -> dict:
    """List all available channels.

    Use the channel name for send_channel_message or get_channel_history.
    """

    async def _do(headers: dict[str, str]) -> list[dict[str, Any]]:
        data = await http_get_params(
            f"{API}/channels.list",
            params={"count": 50},
            headers=headers,
        )
        return [
            {
                "channel": ch["name"],
                "members_count": ch.get("usersCount", 0),
            }
            for ch in data.get("channels", [])
        ]

    channels = await _authenticated_call(_do)
    return {"channels": channels}


@mcp.tool()
async def get_channel_history(
    channel: Annotated[
        str,
        Field(
            description=("RocketChat channel name from list_channels (no '#' prefix).")
        ),
    ],
    count: Annotated[
        int,
        Field(
            description="Maximum number of messages to return.",
            ge=1,
            le=100,
        ),
    ] = 20,
) -> dict:
    """Get recent messages from a specific channel.

    Pass the channel name from list_channels.

    `time` is an ISO 8601 timestamp string. `sender` is a username.
    """

    async def _do(headers: dict[str, str]) -> list[dict[str, Any]]:
        try:
            ch_data = await http_get_params(
                f"{API}/channels.info",
                params={"roomName": channel},
                headers=headers,
            )
        except HTTPToolError as exc:
            if exc.status_code is not None and 400 <= exc.status_code < 500:
                raise ToolError(
                    f"Channel '{channel}' not found in RocketChat. "
                    "Use list_channels to discover valid channel names."
                ) from exc
            raise
        room_id = (ch_data.get("channel") or {}).get("_id")
        if not room_id:
            raise ToolError(
                f"Channel '{channel}' not found in RocketChat. "
                "Use list_channels to discover valid channel names."
            )
        data = await http_get_params(
            f"{API}/channels.history",
            params={"roomId": room_id, "count": count},
            headers=headers,
        )
        return [
            {
                "message_id": msg["_id"],
                "sender": msg.get("u", {}).get("username", ""),
                "time": msg.get("ts", ""),
                "text": msg.get("msg", ""),
            }
            for msg in data.get("messages", [])
        ]

    messages = await _authenticated_call(_do)
    return {"messages": messages}


@mcp.tool()
async def get_user_info(
    username: Annotated[
        str,
        Field(
            description=(
                "Exact RocketChat username (no '@' prefix). This server has no "
                "user discovery tool, so the username must be known in advance "
                "— typically from a `sender` field in a previous "
                "search_messages or get_channel_history result."
            )
        ),
    ],
) -> dict:
    """Get profile information for a user by username.

    There is no list_users / search_users tool on this server: the username
    must be known in advance, typically from a `sender` field in a previous
    search_messages or get_channel_history result.
    """

    async def _do(headers: dict[str, str]) -> dict[str, Any]:
        try:
            data = await http_get_params(
                f"{API}/users.info",
                params={"username": username},
                headers=headers,
            )
        except HTTPToolError as exc:
            if exc.status_code is not None and 400 <= exc.status_code < 500:
                raise ToolError(
                    f"User '{username}' not found in RocketChat. "
                    "Check the username spelling or query an existing user."
                ) from exc
            raise
        user = data.get("user") or {}
        if not user:
            raise ToolError(
                f"User '{username}' not found in RocketChat. "
                "Check the username spelling or query an existing user."
            )
        emails = user.get("emails") or []
        first_email = emails[0].get("address", "") if emails else ""
        return {
            "username": user.get("username", ""),
            "name": user.get("name", ""),
            "email": first_email,
            "status": user.get("status", ""),
        }

    return await _authenticated_call(_do)


if __name__ == "__main__":
    logger.info(f"Starting RocketChat MCP server (url={ROCKETCHAT_URL})")
    mcp.run(transport="streamable-http", host="0.0.0.0", port=8080)
