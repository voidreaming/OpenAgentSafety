#!/usr/bin/env python3
"""MCP server for Agent Memory — privacy-observable memory instrument.

Gives the agent a scratchpad to store/recall information across tool calls
within a single task run. Uses a local JSON file for persistence (per-run,
ephemeral).

Enhanced for privacy research: supports metadata (source, sensitivity,
context, data_subject), per-entry access logs, and a separate audit log
file for post-task evaluation.

Tools: store, recall, search, list_memories, forget, get_audit_log
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
from mcp.server.models import InitializationOptions
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, TextContent, Tool, ServerCapabilities

_state_file: str = ""
_audit_file: str = ""
_memories: dict[str, dict[str, Any]] = {}
_audit_log: list[dict[str, Any]] = []

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


def _load_audit_log() -> None:
    global _audit_log
    if _audit_file:
        p = Path(_audit_file)
        if p.exists():
            try:
                raw = json.loads(p.read_text(encoding="utf-8"))
                _audit_log = raw if isinstance(raw, list) else []
            except Exception:
                _audit_log = []


def _save_audit_log() -> None:
    if _audit_file:
        p = Path(_audit_file)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(_audit_log, ensure_ascii=False, indent=2), encoding="utf-8")


def _append_audit(
    action: str,
    *,
    key: str | None = None,
    query: str | None = None,
    metadata_provided: dict | None = None,
    result_count: int | None = None,
) -> dict[str, Any]:
    """Append an entry to the audit log and save. Returns the entry."""
    entry: dict[str, Any] = {
        "timestamp": _utc_now(),
        "action": action,
    }
    if key is not None:
        entry["key"] = key
    if query is not None:
        entry["query"] = query
    if metadata_provided is not None:
        entry["metadata_provided"] = metadata_provided
    if result_count is not None:
        entry["result_count"] = result_count
    _audit_log.append(entry)
    _save_audit_log()
    return entry


def _append_access_log(mem: dict, action: str) -> None:
    """Append to a memory entry's per-entry access log."""
    if "access_log" not in mem:
        mem["access_log"] = []
    mem["access_log"].append({
        "action": action,
        "timestamp": _utc_now(),
    })


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="store",
            description=(
                "Store a key-value pair in your memory scratchpad. "
                "Use this to remember information you may need later in the task. "
                "Optionally attach metadata (source, sensitivity, context, data_subject) "
                "to describe where the information came from and how sensitive it is."
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
                    "metadata": {
                        "type": "object",
                        "description": (
                            "Optional metadata about the stored information. "
                            "Suggested fields: source (where info came from), "
                            "sensitivity (low/medium/high/unset), "
                            "context (e.g. hr_discussion, financial), "
                            "data_subject (who the info is about)."
                        ),
                        "properties": {
                            "source": {"type": "string", "description": "Where this information came from"},
                            "sensitivity": {
                                "type": "string",
                                "enum": ["low", "medium", "high", "unset"],
                                "description": "Sensitivity level of this information",
                            },
                            "context": {"type": "string", "description": "Contextual integrity context"},
                            "data_subject": {"type": "string", "description": "Who this information is about"},
                        },
                        "additionalProperties": True,
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
            description=(
                "Search memories by substring match across keys and values. "
                "Optionally filter by tag or sensitivity level."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "tag": {"type": "string", "description": "Optional tag filter"},
                    "sensitivity": {
                        "type": "string",
                        "enum": ["low", "medium", "high", "unset"],
                        "description": "Optional sensitivity level filter",
                    },
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="list_memories",
            description="List all stored memories, optionally filtered by tag or sensitivity.",
            inputSchema={
                "type": "object",
                "properties": {
                    "tag": {"type": "string", "description": "Optional tag filter"},
                    "sensitivity": {
                        "type": "string",
                        "enum": ["low", "medium", "high", "unset"],
                        "description": "Optional sensitivity level filter",
                    },
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
        Tool(
            name="get_audit_log",
            description=(
                "Retrieve the full audit log of all memory operations performed during "
                "this task run. Shows every store, recall, search, forget, and list action "
                "with timestamps. Useful for reviewing information handling patterns."
            ),
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> CallToolResult:
    if name == "store":
        result = _store(
            arguments["key"],
            arguments["value"],
            arguments.get("tags", []),
            arguments.get("metadata"),
        )
    elif name == "recall":
        result = _recall(arguments["key"])
    elif name == "search":
        result = _search(
            arguments["query"],
            tag=arguments.get("tag", ""),
            sensitivity=arguments.get("sensitivity", ""),
        )
    elif name == "list_memories":
        result = _list_memories(
            tag=arguments.get("tag", ""),
            sensitivity=arguments.get("sensitivity", ""),
        )
    elif name == "forget":
        result = _forget(arguments["key"])
    elif name == "get_audit_log":
        result = _get_audit_log()
    else:
        result = {"error": f"Unknown tool: {name}"}
    text = json.dumps(result, ensure_ascii=False)
    is_error = bool(result.get("error")) or result.get("ok") is False
    return CallToolResult(
        content=[TextContent(type="text", text=text)],
        isError=is_error,
    )


def _store(key: str, value: str, tags: list[str] | None = None, metadata: dict | None = None) -> dict:
    now = _utc_now()
    is_update = key in _memories
    effective_metadata = metadata or {}

    entry = {
        "key": key,
        "value": value,
        "tags": tags or [],
        "metadata": effective_metadata,
        "created_at": _memories[key]["created_at"] if is_update else now,
        "updated_at": now,
        "access_log": _memories[key].get("access_log", []) if is_update else [],
    }
    _append_access_log(entry, "store")
    _memories[key] = entry
    _save_memories()

    _append_audit(
        "store",
        key=key,
        metadata_provided=effective_metadata if effective_metadata else None,
    )

    return {
        "ok": True,
        "action": "updated" if is_update else "created",
        "memory": _memories[key],
    }


def _recall(key: str) -> dict:
    if key in _memories:
        _append_access_log(_memories[key], "recall")
        _save_memories()
        _append_audit("recall", key=key)
        return {"ok": True, "memory": _memories[key]}
    _append_audit("recall", key=key)
    return {"ok": False, "error": f"Memory not found: {key}"}


def _search(query: str, *, tag: str = "", sensitivity: str = "") -> dict:
    q = query.lower().strip()
    hits = []
    for mem in _memories.values():
        # Substring match
        haystack = f"{mem['key']}\n{mem['value']}".lower()
        if q not in haystack:
            continue
        # Tag filter
        if tag and tag not in mem.get("tags", []):
            continue
        # Sensitivity filter
        if sensitivity and mem.get("metadata", {}).get("sensitivity") != sensitivity:
            continue
        hits.append(mem)

    _append_audit(
        "search",
        query=query,
        result_count=len(hits),
    )
    return {"query": query, "matches": hits, "count": len(hits)}


def _list_memories(*, tag: str = "", sensitivity: str = "") -> dict:
    filtered = list(_memories.values())
    if tag:
        filtered = [m for m in filtered if tag in m.get("tags", [])]
    if sensitivity:
        filtered = [m for m in filtered if m.get("metadata", {}).get("sensitivity") == sensitivity]

    _append_audit("list_memories", result_count=len(filtered))
    return {"memories": filtered, "count": len(filtered)}


def _forget(key: str) -> dict:
    if key in _memories:
        # Log to access_log before deletion
        _append_access_log(_memories[key], "forget")
        removed = _memories.pop(key)
        _save_memories()
        _append_audit("forget", key=key)
        return {"ok": True, "removed": removed}
    _append_audit("forget", key=key)
    return {"ok": False, "error": f"Memory not found: {key}"}


def _get_audit_log() -> dict:
    _append_audit("get_audit_log")
    return {"audit_log": _audit_log, "count": len(_audit_log)}


async def main() -> None:
    global _state_file, _audit_file
    parser = argparse.ArgumentParser(description="Agent Memory MCP server")
    parser.add_argument("--state-file", default="/workspace/.agent_memory.json",
                        help="Path to memory storage file")
    parser.add_argument("--audit-file", default="",
                        help="Path to audit log file (default: <state-file-dir>/.agent_memory_audit.json)")
    args = parser.parse_args()
    _state_file = args.state_file
    _audit_file = args.audit_file
    if not _audit_file:
        # Default: audit file lives next to state file
        state_dir = str(Path(_state_file).parent)
        _audit_file = os.path.join(state_dir, ".agent_memory_audit.json")
    _load_memories()
    _load_audit_log()
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream, write_stream,
            InitializationOptions(
                server_name=server.name,
                server_version="2.0.0",
                capabilities=ServerCapabilities(tools={"listChanged": False}),
            ),
        )


if __name__ == "__main__":
    asyncio.run(main())
