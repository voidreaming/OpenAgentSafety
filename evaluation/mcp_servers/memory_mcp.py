#!/usr/bin/env python3
"""MCP server for Agent Memory — persistent key-value scratchpad.

Gives the agent a scratchpad to store/recall information across tool calls
within a single task run. Uses a local JSON file for persistence (per-run,
ephemeral).

Tools: store, recall, search, list_memories, forget
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

_state_file: str = ""
_memories: dict[str, dict[str, Any]] = {}

server = Server("oas-memory")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_memories() -> None:
    global _memories
    if _state_file:
        p = Path(_state_file)
        if p.exists():
            try:
                raw = json.loads(p.read_text(encoding="utf-8"))
                _memories = raw if isinstance(raw, dict) else {}
            except Exception:
                _memories = {}


def _save_memories() -> None:
    if _state_file:
        p = Path(_state_file)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(_memories, ensure_ascii=False, indent=2), encoding="utf-8")


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="store",
            description=(
                "Store a key-value pair in your memory scratchpad. "
                "Use this to remember information you may need later in the task."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "key": {"type": "string", "description": "Memory key (unique identifier)"},
                    "value": {"type": "string", "description": "Value to store"},
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional tags for categorization",
                    },
                },
                "required": ["key", "value"],
            },
        ),
        Tool(
            name="recall",
            description="Retrieve a stored memory by its key.",
            inputSchema={
                "type": "object",
                "properties": {
                    "key": {"type": "string", "description": "Memory key to recall"},
                },
                "required": ["key"],
            },
        ),
        Tool(
            name="search",
            description="Search memories by substring match across keys and values.",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="list_memories",
            description="List all stored memories, optionally filtered by tag.",
            inputSchema={
                "type": "object",
                "properties": {
                    "tag": {"type": "string", "description": "Optional tag filter"},
                },
            },
        ),
        Tool(
            name="forget",
            description="Delete a memory by key.",
            inputSchema={
                "type": "object",
                "properties": {
                    "key": {"type": "string", "description": "Memory key to delete"},
                },
                "required": ["key"],
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    if name == "store":
        result = _store(
            arguments["key"],
            arguments["value"],
            arguments.get("tags", []),
        )
    elif name == "recall":
        result = _recall(arguments["key"])
    elif name == "search":
        result = _search(arguments["query"])
    elif name == "list_memories":
        result = _list_memories(arguments.get("tag", ""))
    elif name == "forget":
        result = _forget(arguments["key"])
    else:
        result = {"error": f"Unknown tool: {name}"}
    return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]


def _store(key: str, value: str, tags: list[str] | None = None) -> dict:
    now = _utc_now()
    is_update = key in _memories
    _memories[key] = {
        "key": key,
        "value": value,
        "tags": tags or [],
        "created_at": _memories[key]["created_at"] if is_update else now,
        "updated_at": now,
    }
    _save_memories()
    return {
        "ok": True,
        "action": "updated" if is_update else "created",
        "memory": _memories[key],
    }


def _recall(key: str) -> dict:
    if key in _memories:
        return {"ok": True, "memory": _memories[key]}
    return {"ok": False, "error": f"Memory not found: {key}"}


def _search(query: str) -> dict:
    q = query.lower().strip()
    hits = []
    for mem in _memories.values():
        haystack = f"{mem['key']}\n{mem['value']}".lower()
        if q in haystack:
            hits.append(mem)
    return {"query": query, "matches": hits, "count": len(hits)}


def _list_memories(tag: str = "") -> dict:
    if tag:
        filtered = [m for m in _memories.values() if tag in m.get("tags", [])]
    else:
        filtered = list(_memories.values())
    return {"memories": filtered, "count": len(filtered)}


def _forget(key: str) -> dict:
    if key in _memories:
        removed = _memories.pop(key)
        _save_memories()
        return {"ok": True, "removed": removed}
    return {"ok": False, "error": f"Memory not found: {key}"}


async def main() -> None:
    global _state_file
    parser = argparse.ArgumentParser(description="Agent Memory MCP server")
    parser.add_argument("--state-file", default="/workspace/.agent_memory.json",
                        help="Path to memory storage file")
    args = parser.parse_args()
    _state_file = args.state_file
    _load_memories()
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream)


if __name__ == "__main__":
    asyncio.run(main())
