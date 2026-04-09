"""Shared utilities for OAS MCP servers.

Provides HTTP helpers with retry, input validation, common response
formatting, and the ``OASMCPServer`` base class used by all per-service
MCP servers in the all-live architecture.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import posixpath
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Literal
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, ServerCapabilities, TextContent, Tool

log = logging.getLogger("mcp.base")


# ---------------------------------------------------------------------------
# Response helpers
# ---------------------------------------------------------------------------

def make_tool_result(result: dict[str, Any]) -> CallToolResult:
    """Wrap a result dict in a CallToolResult, setting isError when appropriate.

    Error is detected when the result dict contains an ``"error"`` key with a
    truthy value, or when ``"ok"`` is explicitly ``False``.
    """
    text = json.dumps(result, ensure_ascii=False)
    is_error = bool(result.get("error")) or result.get("ok") is False
    return CallToolResult(
        content=[TextContent(type="text", text=text)],
        isError=is_error,
    )


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_email(value: str) -> str:
    return value.strip().lower()


def tool_response(
    *,
    tool: str,
    source: str,
    result: Any,
    classification: list[str] | None = None,
    provenance: list[dict[str, Any]] | None = None,
    ok: bool = True,
    error: str | None = None,
) -> dict[str, Any]:
    return {
        "ok": ok,
        "tool": tool,
        "timestamp": utc_now_iso(),
        "source": source,
        "classification": classification or [],
        "provenance": provenance or [],
        "result": result,
        "error": error,
    }


# ---------------------------------------------------------------------------
# Input validation helpers
# ---------------------------------------------------------------------------

def require_arg(args: dict, name: str, *, expected_type: type = str,
                max_length: int | None = None) -> Any:
    """Validate a required tool argument. Raises ValueError on failure."""
    val = args.get(name)
    if val is None:
        raise ValueError(f"Missing required argument: {name}")
    if expected_type is not None and not isinstance(val, expected_type):
        raise TypeError(f"Argument '{name}' must be {expected_type.__name__}, got {type(val).__name__}")
    if max_length and isinstance(val, str) and len(val) > max_length:
        raise ValueError(f"Argument '{name}' exceeds max length {max_length}")
    return val


def optional_arg(args: dict, name: str, default: Any = "",
                 max_length: int | None = None) -> Any:
    """Get an optional argument with a default, applying length validation."""
    val = args.get(name, default)
    if val is None:
        return default
    if max_length and isinstance(val, str) and len(val) > max_length:
        raise ValueError(f"Argument '{name}' exceeds max length {max_length}")
    return val


def sanitize_path(path: str) -> str:
    """Remove path traversal components (.. and absolute path escapes).

    Returns a safe relative path rooted at /.
    """
    # Normalize: resolve . and .. components
    parts = path.replace("\\", "/").split("/")
    safe = [p for p in parts if p and p != "." and p != ".."]
    return "/" + "/".join(safe) if safe else "/"


def sanitize_header_value(value: str) -> str:
    """Strip CR/LF from a string to prevent header injection."""
    return re.sub(r"[\r\n]", " ", value)


def sanitize_html(value: str) -> str:
    """Escape HTML special characters to prevent injection."""
    return (value
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
            .replace("'", "&#x27;"))


# ---------------------------------------------------------------------------
# HTTP helpers for live-service backends (with retry)
# ---------------------------------------------------------------------------

_DEFAULT_TIMEOUT = 30
_DEFAULT_RETRIES = 2
_RETRYABLE_CODES = {429, 500, 502, 503, 504}


def _should_retry(exc: Exception, retryable_codes: set[int]) -> bool:
    """Check if an HTTP exception is retryable."""
    if isinstance(exc, HTTPError) and exc.code in retryable_codes:
        return True
    if isinstance(exc, URLError):
        return True  # Connection refused, timeout, DNS failure
    return False


def _do_request(
    url: str,
    *,
    method: str = "GET",
    data: bytes | None = None,
    headers: dict[str, str] | None = None,
    timeout: int = _DEFAULT_TIMEOUT,
    retries: int = _DEFAULT_RETRIES,
    retryable_codes: set[int] = _RETRYABLE_CODES,
) -> dict[str, Any]:
    """Core HTTP request with retry and exponential backoff."""
    req = Request(url, data=data, headers=headers or {}, method=method)

    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            with urlopen(req, timeout=timeout) as resp:
                body = resp.read().decode("utf-8", errors="replace")
                return json.loads(body) if body.strip() else {"ok": True}
        except HTTPError as exc:
            resp_body = exc.read().decode("utf-8", errors="replace")
            last_exc = exc
            # Try to parse error response as JSON
            try:
                error_json = json.loads(resp_body)
            except Exception:
                error_json = None

            if _should_retry(exc, retryable_codes) and attempt < retries:
                wait = min(2 ** attempt, 10)
                log.warning("HTTP %s %s → %d, retry %d/%d in %ds",
                            method, url, exc.code, attempt + 1, retries, wait)
                time.sleep(wait)
                continue

            if error_json:
                return error_json
            return {"_http_error": exc.code, "_body": resp_body}

        except (URLError, OSError) as exc:
            last_exc = exc
            if attempt < retries:
                wait = min(2 ** attempt, 10)
                log.warning("HTTP %s %s → %s, retry %d/%d in %ds",
                            method, url, exc, attempt + 1, retries, wait)
                time.sleep(wait)
                continue
            return {"_error": str(exc)}

        except Exception as exc:
            return {"_error": str(exc)}

    return {"_error": str(last_exc) if last_exc else "Unknown error"}


def http_get(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    params: dict[str, str] | None = None,
    timeout: int = _DEFAULT_TIMEOUT,
    retries: int = _DEFAULT_RETRIES,
) -> dict[str, Any]:
    """HTTP GET with retry, returning parsed JSON."""
    if params:
        url = f"{url}?{urlencode(params)}"
    return _do_request(url, method="GET", headers=headers, timeout=timeout, retries=retries)


def http_post(
    url: str,
    *,
    body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: int = _DEFAULT_TIMEOUT,
    retries: int = _DEFAULT_RETRIES,
) -> dict[str, Any]:
    """HTTP POST with JSON body, retry, returning parsed JSON."""
    hdrs = {"Content-Type": "application/json"}
    if headers:
        hdrs.update(headers)
    data = json.dumps(body or {}, ensure_ascii=False).encode("utf-8")
    return _do_request(url, method="POST", data=data, headers=hdrs,
                       timeout=timeout, retries=retries)


def http_delete(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    timeout: int = _DEFAULT_TIMEOUT,
    retries: int = _DEFAULT_RETRIES,
) -> dict[str, Any]:
    """HTTP DELETE with retry, returning parsed JSON."""
    return _do_request(url, method="DELETE", headers=headers,
                       timeout=timeout, retries=retries)


def http_put(
    url: str,
    *,
    body: dict[str, Any] | str | bytes | None = None,
    headers: dict[str, str] | None = None,
    content_type: str = "application/json",
    timeout: int = _DEFAULT_TIMEOUT,
    retries: int = _DEFAULT_RETRIES,
) -> dict[str, Any]:
    """HTTP PUT with retry."""
    hdrs = {"Content-Type": content_type}
    if headers:
        hdrs.update(headers)
    if isinstance(body, dict):
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    elif isinstance(body, str):
        data = body.encode("utf-8")
    elif isinstance(body, bytes):
        data = body
    else:
        data = b""
    return _do_request(url, method="PUT", data=data, headers=hdrs,
                       timeout=timeout, retries=retries)


def http_patch(
    url: str,
    *,
    body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: int = _DEFAULT_TIMEOUT,
    retries: int = _DEFAULT_RETRIES,
) -> dict[str, Any]:
    """HTTP PATCH with JSON body, retry, returning parsed JSON."""
    hdrs = {"Content-Type": "application/json"}
    if headers:
        hdrs.update(headers)
    data = json.dumps(body or {}, ensure_ascii=False).encode("utf-8")
    return _do_request(url, method="PATCH", data=data, headers=hdrs,
                       timeout=timeout, retries=retries)


# ---------------------------------------------------------------------------
# Raw HTTP request (returns body string instead of parsed JSON)
# ---------------------------------------------------------------------------

def _do_request_raw(
    url: str,
    *,
    method: str = "GET",
    data: bytes | None = None,
    headers: dict[str, str] | None = None,
    timeout: int = _DEFAULT_TIMEOUT,
    retries: int = _DEFAULT_RETRIES,
    retryable_codes: set[int] = _RETRYABLE_CODES,
) -> str:
    """Like _do_request but returns the raw response body as a string.

    Used for non-JSON responses (e.g. WebDAV XML).
    """
    req = Request(url, data=data, headers=headers or {}, method=method)

    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            with urlopen(req, timeout=timeout) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except HTTPError as exc:
            last_exc = exc
            if _should_retry(exc, retryable_codes) and attempt < retries:
                wait = min(2 ** attempt, 10)
                log.warning("HTTP %s %s → %d, retry %d/%d in %ds",
                            method, url, exc.code, attempt + 1, retries, wait)
                time.sleep(wait)
                continue
            # Non-retryable or final attempt — read the error body
            body = exc.read().decode("utf-8", errors="replace")
            raise HTTPError(exc.url, exc.code, body, exc.headers, None) from exc
        except (URLError, OSError) as exc:
            last_exc = exc
            if attempt < retries:
                wait = min(2 ** attempt, 10)
                log.warning("HTTP %s %s → %s, retry %d/%d in %ds",
                            method, url, exc, attempt + 1, retries, wait)
                time.sleep(wait)
                continue
            raise
    raise last_exc or RuntimeError("Unknown error")


# ---------------------------------------------------------------------------
# WebDAV helpers
# ---------------------------------------------------------------------------

def http_propfind(
    url: str,
    *,
    body: str = "",
    headers: dict[str, str] | None = None,
    depth: str = "1",
    timeout: int = _DEFAULT_TIMEOUT,
    retries: int = _DEFAULT_RETRIES,
) -> str:
    """WebDAV PROPFIND, returning raw XML response body."""
    hdrs: dict[str, str] = {"Content-Type": "application/xml", "Depth": depth}
    if headers:
        hdrs.update(headers)
    return _do_request_raw(
        url, method="PROPFIND",
        data=body.encode("utf-8") if body else None,
        headers=hdrs, timeout=timeout, retries=retries,
    )


def http_mkcol(
    url: str,
    *,
    body: str = "",
    headers: dict[str, str] | None = None,
    timeout: int = _DEFAULT_TIMEOUT,
) -> bool:
    """WebDAV MKCOL (create collection). Returns True on success, False if already exists."""
    hdrs: dict[str, str] = {}
    if body:
        hdrs["Content-Type"] = "application/xml"
    if headers:
        hdrs.update(headers)
    try:
        _do_request_raw(
            url, method="MKCOL",
            data=body.encode("utf-8") if body else None,
            headers=hdrs, timeout=timeout, retries=0,
        )
        return True
    except HTTPError as exc:
        if exc.code in (405, 409):
            return False  # already exists or conflict
        raise


# ---------------------------------------------------------------------------
# Tool definition dataclass
# ---------------------------------------------------------------------------

@dataclass
class ToolDef:
    """Declarative tool definition with classification metadata.

    Each MCP server subclass returns a dict of ``{name: ToolDef}`` from
    :meth:`OASMCPServer.define_tools`.  The ``handler`` is a bound method
    (sync or async) that accepts ``(arguments: dict) -> dict``.
    """
    description: str
    input_schema: dict[str, Any]
    classification: Literal["send", "retrieve", "mutate"]
    handler: Callable[..., Any]


# ---------------------------------------------------------------------------
# OASMCPServer base class
# ---------------------------------------------------------------------------

class OASMCPServer(ABC):
    """Base class for all OAS MCP servers.

    Subclasses must set ``server_name`` and implement ``define_tools()``.
    Optional hooks: ``add_arguments``, ``configure``, ``startup``,
    ``health_check``.

    Usage::

        class MyServer(OASMCPServer):
            server_name = "oas-myservice"

            def define_tools(self):
                return {
                    "myservice_search": ToolDef(
                        description="Search items",
                        input_schema={"type": "object", "properties": {...}},
                        classification="retrieve",
                        handler=self.search,
                    ),
                }

            def search(self, arguments: dict) -> dict:
                ...

        if __name__ == "__main__":
            MyServer.main()
    """

    server_name: str
    server_version: str = "1.0.0"

    def __init__(self) -> None:
        self._server = Server(self.server_name)

    # --- Subclass API (must implement) ---

    @abstractmethod
    def define_tools(self) -> dict[str, ToolDef]:
        """Return ``{tool_name: ToolDef}`` mapping."""
        ...

    # --- Subclass API (optional hooks) ---

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        """Add CLI arguments beyond the defaults."""

    def configure(self, args: argparse.Namespace) -> None:
        """Store parsed args as instance attributes."""

    async def startup(self) -> None:
        """Async one-time setup (auth, ensure collections, load files).

        Called after ``configure()``, before the stdio loop.
        """

    async def health_check(self) -> dict[str, Any]:
        """Verify backend connectivity.

        Returns ``{"ok": True/False, "detail": "..."}``.
        Failure is logged but does not prevent startup.
        """
        return {"ok": True, "detail": "no-op"}

    # --- Internal registration ---

    def _register_handlers(self) -> None:
        """Wire up tool definitions from ``define_tools()`` into the MCP server."""
        tool_map = self.define_tools()
        self._tool_map = tool_map  # exposed for testing

        @self._server.list_tools()
        async def list_tools() -> list[Tool]:
            return [
                Tool(
                    name=name,
                    description=td.description,
                    inputSchema={
                        **td.input_schema,
                        "x-oas-classification": td.classification,
                    },
                )
                for name, td in tool_map.items()
            ]

        async def _dispatch(name: str, arguments: dict) -> CallToolResult:
            td = tool_map.get(name)
            if td is None:
                return make_tool_result({"error": f"Unknown tool: {name}"})
            try:
                if asyncio.iscoroutinefunction(td.handler):
                    result = await td.handler(arguments)
                else:
                    result = td.handler(arguments)
                return make_tool_result(result)
            except (ValueError, TypeError) as exc:
                return make_tool_result({"error": str(exc)})
            except Exception as exc:
                log.exception("Unhandled error in %s.%s", self.server_name, name)
                return make_tool_result({"error": f"Internal error: {exc}"})

        self._dispatch = _dispatch  # exposed for testing

        @self._server.call_tool()
        async def call_tool(name: str, arguments: dict) -> CallToolResult:
            return await _dispatch(name, arguments)

    # --- Entry point ---

    async def run(self) -> None:
        """Parse args, configure, register tools, run health check, start stdio loop."""
        parser = argparse.ArgumentParser(description=self.server_name)
        self.add_arguments(parser)
        args = parser.parse_args()
        self.configure(args)

        # Register after configure so define_tools() can use instance attrs
        self._register_handlers()

        await self.startup()

        health = await self.health_check()
        if not health.get("ok"):
            log.error("Health check FAILED for %s: %s",
                      self.server_name, health.get("detail", "unknown"))

        async with stdio_server() as (read_stream, write_stream):
            await self._server.run(
                read_stream,
                write_stream,
                self._server.create_initialization_options(),
            )

    @classmethod
    def main(cls) -> None:
        """Convenience entry point: instantiate and run."""
        asyncio.run(cls().run())
