"""Tests for OASMCPServer base class, ToolDef, and new HTTP helpers in base.py."""
from __future__ import annotations

import asyncio
import json
from http.server import HTTPServer, BaseHTTPRequestHandler
from threading import Thread
from typing import Any

import pytest

from mcp_servers.base import (
    OASMCPServer,
    ToolDef,
    http_patch,
    _do_request_raw,
    http_propfind,
    http_mkcol,
)


# ---------------------------------------------------------------------------
# ToolDef
# ---------------------------------------------------------------------------

class TestToolDef:
    def test_creation(self) -> None:
        def handler(args):
            return {"ok": True}
        td = ToolDef(
            description="test tool",
            input_schema={"type": "object", "properties": {}},
            classification="retrieve",
            handler=handler,
        )
        assert td.description == "test tool"
        assert td.classification == "retrieve"
        assert td.handler is handler

    def test_classification_values(self) -> None:
        for cls in ("send", "retrieve", "mutate"):
            td = ToolDef(
                description="x",
                input_schema={"type": "object"},
                classification=cls,
                handler=lambda a: {},
            )
            assert td.classification == cls


# ---------------------------------------------------------------------------
# Minimal OASMCPServer subclass for testing
# ---------------------------------------------------------------------------

class _EchoServer(OASMCPServer):
    """Minimal test server with one sync and one async handler."""
    server_name = "oas-echo-test"

    def __init__(self) -> None:
        super().__init__()
        self._started = False
        self._health_called = False

    def define_tools(self) -> dict[str, ToolDef]:
        return {
            "echo_sync": ToolDef(
                description="Sync echo",
                input_schema={
                    "type": "object",
                    "properties": {"msg": {"type": "string"}},
                    "required": ["msg"],
                },
                classification="retrieve",
                handler=self.handle_sync,
            ),
            "echo_async": ToolDef(
                description="Async echo",
                input_schema={
                    "type": "object",
                    "properties": {"msg": {"type": "string"}},
                    "required": ["msg"],
                },
                classification="send",
                handler=self.handle_async,
            ),
            "echo_error": ToolDef(
                description="Raises ValueError",
                input_schema={"type": "object", "properties": {}},
                classification="mutate",
                handler=self.handle_error,
            ),
            "echo_crash": ToolDef(
                description="Raises RuntimeError",
                input_schema={"type": "object", "properties": {}},
                classification="mutate",
                handler=self.handle_crash,
            ),
        }

    def handle_sync(self, arguments: dict) -> dict:
        return {"ok": True, "echo": arguments.get("msg", "")}

    async def handle_async(self, arguments: dict) -> dict:
        return {"ok": True, "echo": arguments.get("msg", "")}

    def handle_error(self, arguments: dict) -> dict:
        raise ValueError("bad input")

    def handle_crash(self, arguments: dict) -> dict:
        raise RuntimeError("kaboom")

    async def startup(self) -> None:
        self._started = True

    async def health_check(self) -> dict[str, Any]:
        self._health_called = True
        return {"ok": True, "detail": "echo-ok"}


# ---------------------------------------------------------------------------
# OASMCPServer — tool registration and dispatch
# ---------------------------------------------------------------------------

class TestOASMCPServer:
    """Test base class registration, dispatch, and error handling.

    Uses _tool_map and _dispatch exposed by _register_handlers() for direct
    in-process testing without needing the MCP stdio transport.
    """

    @pytest.fixture
    def server(self) -> _EchoServer:
        srv = _EchoServer()
        srv._register_handlers()
        return srv

    def _run(self, coro):
        """Run a coroutine synchronously."""
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    def test_tool_map_populated(self, server: _EchoServer) -> None:
        assert set(server._tool_map.keys()) == {
            "echo_sync", "echo_async", "echo_error", "echo_crash",
        }

    def test_classification_in_tool_map(self, server: _EchoServer) -> None:
        assert server._tool_map["echo_sync"].classification == "retrieve"
        assert server._tool_map["echo_async"].classification == "send"
        assert server._tool_map["echo_error"].classification == "mutate"

    def test_sync_handler_dispatch(self, server: _EchoServer) -> None:
        result = self._run(server._dispatch("echo_sync", {"msg": "hello"}))
        assert result.isError is False
        data = json.loads(result.content[0].text)
        assert data["echo"] == "hello"

    def test_async_handler_dispatch(self, server: _EchoServer) -> None:
        result = self._run(server._dispatch("echo_async", {"msg": "world"}))
        assert result.isError is False
        data = json.loads(result.content[0].text)
        assert data["echo"] == "world"

    def test_unknown_tool(self, server: _EchoServer) -> None:
        result = self._run(server._dispatch("nonexistent", {}))
        assert result.isError is True
        data = json.loads(result.content[0].text)
        assert "Unknown tool" in data["error"]

    def test_value_error_handler(self, server: _EchoServer) -> None:
        result = self._run(server._dispatch("echo_error", {}))
        assert result.isError is True
        data = json.loads(result.content[0].text)
        assert "bad input" in data["error"]

    def test_unhandled_error(self, server: _EchoServer) -> None:
        result = self._run(server._dispatch("echo_crash", {}))
        assert result.isError is True
        data = json.loads(result.content[0].text)
        assert "Internal error" in data["error"]

    def test_startup_called(self) -> None:
        srv = _EchoServer()
        self._run(srv.startup())
        assert srv._started is True

    def test_health_check(self) -> None:
        srv = _EchoServer()
        result = self._run(srv.health_check())
        assert result["ok"] is True
        assert srv._health_called is True

    def test_default_health_check(self) -> None:
        """Base class default health check returns ok."""
        class _Bare(OASMCPServer):
            server_name = "oas-bare"
            def define_tools(self):
                return {}
        srv = _Bare()
        result = self._run(srv.health_check())
        assert result["ok"] is True


# ---------------------------------------------------------------------------
# HTTP helpers (http_patch, _do_request_raw, http_propfind, http_mkcol)
# ---------------------------------------------------------------------------

class _TestHTTPHandler(BaseHTTPRequestHandler):
    """Handles PATCH, PROPFIND, MKCOL, GET for test purposes."""

    def do_PATCH(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"ok": True, "patched": json.loads(body)}).encode())

    def do_PROPFIND(self):
        self.send_response(207)
        self.send_header("Content-Type", "application/xml")
        self.end_headers()
        xml = '<?xml version="1.0"?><multistatus><response><href>/cal/</href></response></multistatus>'
        self.wfile.write(xml.encode())

    def do_MKCOL(self):
        if self.path == "/exists/":
            self.send_error(405)
        else:
            self.send_response(201)
            self.end_headers()
            self.wfile.write(b"")

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"raw-text-body")

    def log_message(self, format, *args):
        pass  # suppress output


@pytest.fixture(scope="module")
def test_http_server():
    """Start a local HTTP server for testing HTTP helpers."""
    httpd = HTTPServer(("127.0.0.1", 0), _TestHTTPHandler)
    port = httpd.server_address[1]
    thread = Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}"
    httpd.shutdown()


class TestHttpPatch:
    def test_patch_json(self, test_http_server: str) -> None:
        result = http_patch(f"{test_http_server}/resource", body={"key": "val"})
        assert result["ok"] is True
        assert result["patched"]["key"] == "val"


class TestDoRequestRaw:
    def test_returns_string(self, test_http_server: str) -> None:
        result = _do_request_raw(f"{test_http_server}/anything", method="GET")
        assert isinstance(result, str)
        assert "raw-text-body" in result


class TestHttpPropfind:
    def test_returns_xml(self, test_http_server: str) -> None:
        result = http_propfind(f"{test_http_server}/cal/")
        assert isinstance(result, str)
        assert "<multistatus>" in result
        assert "<href>/cal/</href>" in result


class TestHttpMkcol:
    def test_create_new(self, test_http_server: str) -> None:
        result = http_mkcol(f"{test_http_server}/new-collection/")
        assert result is True

    def test_already_exists(self, test_http_server: str) -> None:
        result = http_mkcol(f"{test_http_server}/exists/")
        assert result is False
