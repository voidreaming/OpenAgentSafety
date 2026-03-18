"""Shared utilities for OAS MCP servers.

Provides HTTP helpers and common response formatting used by all
per-service MCP servers in the all-live architecture.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


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
# HTTP helpers for live-service backends
# ---------------------------------------------------------------------------

def http_get(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    params: dict[str, str] | None = None,
    timeout: int = 30,
) -> dict[str, Any]:
    """Simple HTTP GET returning parsed JSON."""
    if params:
        url = f"{url}?{urlencode(params)}"
    req = Request(url, headers=headers or {}, method="GET")
    try:
        with urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            return json.loads(body)
        except Exception:
            return {"_http_error": exc.code, "_body": body}
    except Exception as exc:
        return {"_error": str(exc)}


def http_post(
    url: str,
    *,
    body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: int = 30,
) -> dict[str, Any]:
    """Simple HTTP POST with JSON body returning parsed JSON."""
    hdrs = {"Content-Type": "application/json"}
    if headers:
        hdrs.update(headers)
    data = json.dumps(body or {}, ensure_ascii=False).encode("utf-8")
    req = Request(url, data=data, headers=hdrs, method="POST")
    try:
        with urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except HTTPError as exc:
        resp_body = exc.read().decode("utf-8", errors="replace")
        try:
            return json.loads(resp_body)
        except Exception:
            return {"_http_error": exc.code, "_body": resp_body}
    except Exception as exc:
        return {"_error": str(exc)}


def http_delete(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    timeout: int = 30,
) -> dict[str, Any]:
    """Simple HTTP DELETE returning parsed JSON."""
    req = Request(url, headers=headers or {}, method="DELETE")
    try:
        with urlopen(req, timeout=timeout) as resp:
            text = resp.read().decode("utf-8", errors="replace")
            return json.loads(text) if text.strip() else {"ok": True}
    except HTTPError as exc:
        resp_body = exc.read().decode("utf-8", errors="replace")
        try:
            return json.loads(resp_body)
        except Exception:
            return {"_http_error": exc.code, "_body": resp_body}
    except Exception as exc:
        return {"_error": str(exc)}


def http_put(
    url: str,
    *,
    body: dict[str, Any] | str | bytes | None = None,
    headers: dict[str, str] | None = None,
    content_type: str = "application/json",
    timeout: int = 30,
) -> dict[str, Any]:
    """Simple HTTP PUT."""
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
    req = Request(url, data=data, headers=hdrs, method="PUT")
    try:
        with urlopen(req, timeout=timeout) as resp:
            text = resp.read().decode("utf-8", errors="replace")
            return json.loads(text) if text.strip() else {"ok": True}
    except HTTPError as exc:
        resp_body = exc.read().decode("utf-8", errors="replace")
        try:
            return json.loads(resp_body)
        except Exception:
            return {"_http_error": exc.code, "_body": resp_body}
    except Exception as exc:
        return {"_error": str(exc)}
