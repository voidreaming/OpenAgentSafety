"""Shared utilities for MCP servers.

Error contract
--------------
Every HTTP helper here translates httpx failures into a ``fastmcp`` ``ToolError``
so that tool errors reach the LLM as a recoverable observation
(``isError=True``) instead of being swallowed into a success-shaped dict.

- HTTP status errors → ``ToolError`` with method, URL, status code, and a
  truncated response body (so the LLM can see *why* the upstream API rejected
  the call, e.g. ``404 {"error":"Page not found"}``).
- Connection / timeout / DNS errors → ``ToolError`` with the underlying
  exception class and message.
- Non-JSON response bodies → ``ToolError`` with the body excerpt (catches
  cases where an API returns an HTML stack trace on 500).

Tool authors should generally call these helpers and let any ``ToolError``
propagate. Reserve manual ``raise ToolError(...)`` in tool code for *expected*
business-logic failures (e.g. "user not found by username") that aren't
already an HTTP-level error.
"""

from __future__ import annotations

import logging
import os
from typing import Any, NoReturn

import httpx
from fastmcp.exceptions import ToolError


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("mcp-server")

# Cap how much of an upstream error body we forward to the LLM. Large bodies
# (HTML stack traces, debug pages) waste tokens and rarely help recovery.
_MAX_ERROR_BODY_CHARS = 500


class HTTPToolError(ToolError):
    """ToolError raised from an HTTP failure, with the upstream status code attached.

    Inherits from ``ToolError`` so existing ``except ToolError`` handlers (and
    FastMCP's own error pipeline) keep working unchanged. The ``status_code``
    attribute lets callers branch on specific upstream conditions (e.g.
    ``except HTTPToolError as e: if e.status_code == 404: ...``) without
    string-matching the message.
    """

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def get_env(key: str, default: str = "") -> str:
    """Get environment variable with default."""
    return os.environ.get(key, default)


def _truncate(text: str, limit: int = _MAX_ERROR_BODY_CHARS) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return text[:limit] + "…(truncated)"


def _raise_http_failure(method: str, url: str, exc: httpx.HTTPError) -> NoReturn:
    """Translate an httpx error into a ``HTTPToolError``. Always raises.

    HTTP status errors carry the status code on the exception so callers can
    branch on it (e.g. for not-found-tolerant lookups or 401 token refresh).
    Network/timeout errors have no upstream status, so ``status_code`` is None.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        body = _truncate(exc.response.text)
        msg = f"{method} {url} → HTTP {status}{f': {body}' if body else ''}"
        logger.warning(msg)
        raise HTTPToolError(msg, status_code=status) from exc
    msg = f"{method} {url} failed: {type(exc).__name__}: {exc}"
    logger.warning(msg)
    raise HTTPToolError(msg, status_code=None) from exc


def _parse_json(method: str, url: str, resp: httpx.Response) -> Any:
    """Parse a JSON response or raise a ToolError that includes the body.

    Returns ``Any`` because upstream APIs may legitimately respond with a
    JSON list, dict, or primitive depending on the endpoint.
    """
    try:
        return resp.json()
    except ValueError as exc:
        body = _truncate(resp.text)
        msg = f"{method} {url} → invalid JSON response: {body}"
        logger.warning(msg)
        raise ToolError(msg) from exc


async def http_get(url: str, headers: dict | None = None) -> Any:
    """GET ``url`` and return the parsed JSON body.

    Raises ``ToolError`` on HTTP, network, or JSON-parse failures.
    """
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            resp = await client.get(url, headers=headers or {})
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            _raise_http_failure("GET", url, exc)
        return _parse_json("GET", url, resp)


async def http_get_params(
    url: str,
    params: dict | None = None,
    headers: dict | None = None,
) -> Any:
    """GET ``url`` with query params and return the parsed JSON body.

    Raises ``ToolError`` on HTTP, network, or JSON-parse failures.
    """
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            resp = await client.get(url, params=params or {}, headers=headers or {})
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            _raise_http_failure("GET", url, exc)
        return _parse_json("GET", url, resp)


async def http_post(
    url: str,
    json_data: Any = None,
    headers: dict | None = None,
) -> Any:
    """POST JSON to ``url`` and return the parsed JSON body.

    ``json_data`` accepts ``Any`` because Mattermost's ``/channels/direct``
    endpoint expects a JSON *array* of user IDs, not an object.

    Raises ``ToolError`` on HTTP, network, or JSON-parse failures.
    """
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            resp = await client.post(url, json=json_data, headers=headers or {})
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            _raise_http_failure("POST", url, exc)
        return _parse_json("POST", url, resp)


async def http_put(
    url: str,
    json_data: Any = None,
    headers: dict | None = None,
) -> Any:
    """PUT JSON to ``url`` and return the parsed JSON body.

    Raises ``ToolError`` on HTTP, network, or JSON-parse failures.
    """
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            resp = await client.put(url, json=json_data, headers=headers or {})
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            _raise_http_failure("PUT", url, exc)
        return _parse_json("PUT", url, resp)


async def http_delete(
    url: str,
    headers: dict | None = None,
) -> Any:
    """DELETE ``url`` and return the parsed JSON body, or ``{"success": True}``
    when the response has no body (e.g. ``204 No Content``).

    Raises ``ToolError`` on HTTP or network failures.
    """
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            resp = await client.delete(url, headers=headers or {})
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            _raise_http_failure("DELETE", url, exc)
        if not resp.content:
            return {"success": True}
        return _parse_json("DELETE", url, resp)


async def http_post_for_login(
    url: str,
    json_data: dict | None = None,
    headers: dict | None = None,
) -> tuple[Any, dict]:
    """POST ``url`` and return ``(json_body, response_headers)``.

    Specialized variant of :func:`http_post` for login flows where the auth
    token is delivered in a *response header* (Mattermost's ``Token`` header,
    for example) rather than the JSON body. Standard tools should keep using
    :func:`http_post`, which discards headers; this helper exists only for the
    handful of cases that genuinely need them.

    Raises ``HTTPToolError`` on HTTP, network, or JSON-parse failures.
    """
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            resp = await client.post(url, json=json_data, headers=headers or {})
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            _raise_http_failure("POST", url, exc)
        body = _parse_json("POST", url, resp)
        return body, dict(resp.headers)
