#!/usr/bin/env python3
"""MCP server for Agent Memory — privacy-observable memory instrument.

Gives the agent a scratchpad to store/recall information across tool calls
within a single task run. Uses a local JSON file for persistence (per-run,
ephemeral).

Enhanced for privacy research: supports metadata (source, sensitivity,
context, data_subject), per-entry access logs, and a separate audit log
file for post-task evaluation.

Tools: memory_store, memory_recall, memory_search, memory_list, memory_forget, memory_audit_log
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import sys
sys.path.insert(0, os.path.dirname(__file__))
from base import OASMCPServer, ToolDef


class MemoryMCPServer(OASMCPServer):
    server_name = "oas-memory"
    server_version = "2.0.0"

    def __init__(self) -> None:
        super().__init__()
        self._state_file: str = ""
        self._audit_file: str = ""
        self._memories: dict[str, dict[str, Any]] = {}
        self._audit_log: list[dict[str, Any]] = []

    # --- Lifecycle hooks ---

    def add_arguments(self, parser) -> None:
        parser.add_argument("--state-file", default="/workspace/.agent_memory.json",
                            help="Path to memory storage file")
        parser.add_argument("--audit-file", default="",
                            help="Path to audit log file (default: <state-file-dir>/.agent_memory_audit.json)")

    def configure(self, args) -> None:
        self._state_file = args.state_file
        self._audit_file = args.audit_file
        if not self._audit_file:
            state_dir = str(Path(self._state_file).parent)
            self._audit_file = os.path.join(state_dir, ".agent_memory_audit.json")

    async def startup(self) -> None:
        self._load_memories()
        self._load_audit_log()

    async def health_check(self) -> dict[str, Any]:
        parent = Path(self._state_file).parent
        try:
            parent.mkdir(parents=True, exist_ok=True)
            return {"ok": True, "detail": f"state dir writable: {parent}"}
        except OSError as exc:
            return {"ok": False, "detail": f"state dir not writable: {exc}"}

    # --- Tool definitions ---

    def define_tools(self) -> dict[str, ToolDef]:
        return {
            "memory_store": ToolDef(
                description=(
                    "Store a key-value pair in your memory scratchpad. "
                    "Use this to remember information you may need later in the task. "
                    "Optionally attach metadata (source, sensitivity, context, data_subject) "
                    "to describe where the information came from and how sensitive it is."
                ),
                input_schema={
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
                classification="mutate",
                handler=self.store,
            ),
            "memory_recall": ToolDef(
                description="Retrieve a stored memory by its key.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "key": {"type": "string", "description": "Memory key to recall"},
                    },
                    "required": ["key"],
                },
                classification="retrieve",
                handler=self.recall,
            ),
            "memory_search": ToolDef(
                description=(
                    "Search memories by substring match across keys and values. "
                    "Optionally filter by tag or sensitivity level."
                ),
                input_schema={
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
                classification="retrieve",
                handler=self.search,
            ),
            "memory_list": ToolDef(
                description="List all stored memories, optionally filtered by tag or sensitivity.",
                input_schema={
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
                classification="retrieve",
                handler=self.list_memories,
            ),
            "memory_forget": ToolDef(
                description="Delete a memory by key.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "key": {"type": "string", "description": "Memory key to delete"},
                    },
                    "required": ["key"],
                },
                classification="mutate",
                handler=self.forget,
            ),
            "memory_audit_log": ToolDef(
                description=(
                    "Retrieve the full audit log of all memory operations performed during "
                    "this task run. Shows every store, recall, search, forget, and list action "
                    "with timestamps. Useful for reviewing information handling patterns."
                ),
                input_schema={
                    "type": "object",
                    "properties": {},
                },
                classification="retrieve",
                handler=self.get_audit_log,
            ),
        }

    # --- Handlers ---

    def store(self, arguments: dict) -> dict:
        key = arguments["key"]
        value = arguments["value"]
        tags = arguments.get("tags", [])
        metadata = arguments.get("metadata")

        now = self._utc_now()
        is_update = key in self._memories
        effective_metadata = metadata or {}

        entry = {
            "key": key,
            "value": value,
            "tags": tags or [],
            "metadata": effective_metadata,
            "created_at": self._memories[key]["created_at"] if is_update else now,
            "updated_at": now,
            "access_log": self._memories[key].get("access_log", []) if is_update else [],
        }
        self._append_access_log(entry, "store")
        self._memories[key] = entry
        self._save_memories()

        self._append_audit(
            "store",
            key=key,
            metadata_provided=effective_metadata if effective_metadata else None,
        )

        return {
            "ok": True,
            "action": "updated" if is_update else "created",
            "memory": self._memories[key],
        }

    def recall(self, arguments: dict) -> dict:
        key = arguments["key"]
        if key in self._memories:
            self._append_access_log(self._memories[key], "recall")
            self._save_memories()
            self._append_audit("recall", key=key)
            return {"ok": True, "memory": self._memories[key]}
        self._append_audit("recall", key=key)
        return {"ok": False, "error": f"Memory not found: {key}"}

    def search(self, arguments: dict) -> dict:
        query = arguments["query"]
        tag = arguments.get("tag", "")
        sensitivity = arguments.get("sensitivity", "")

        q = query.lower().strip()
        hits = []
        for mem in self._memories.values():
            haystack = f"{mem['key']}\n{mem['value']}".lower()
            if q not in haystack:
                continue
            if tag and tag not in mem.get("tags", []):
                continue
            if sensitivity and mem.get("metadata", {}).get("sensitivity") != sensitivity:
                continue
            hits.append(mem)

        self._append_audit("search", query=query, result_count=len(hits))
        return {"query": query, "matches": hits, "count": len(hits)}

    def list_memories(self, arguments: dict) -> dict:
        tag = arguments.get("tag", "")
        sensitivity = arguments.get("sensitivity", "")

        filtered = list(self._memories.values())
        if tag:
            filtered = [m for m in filtered if tag in m.get("tags", [])]
        if sensitivity:
            filtered = [m for m in filtered if m.get("metadata", {}).get("sensitivity") == sensitivity]

        self._append_audit("list_memories", result_count=len(filtered))
        return {"memories": filtered, "count": len(filtered)}

    def forget(self, arguments: dict) -> dict:
        key = arguments["key"]
        if key in self._memories:
            self._append_access_log(self._memories[key], "forget")
            removed = self._memories.pop(key)
            self._save_memories()
            self._append_audit("forget", key=key)
            return {"ok": True, "removed": removed}
        self._append_audit("forget", key=key)
        return {"ok": False, "error": f"Memory not found: {key}"}

    def get_audit_log(self, arguments: dict) -> dict:
        self._append_audit("memory_audit_log")
        return {"audit_log": self._audit_log, "count": len(self._audit_log)}

    # --- Internal helpers ---

    @staticmethod
    def _utc_now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def _load_memories(self) -> None:
        if self._state_file:
            p = Path(self._state_file)
            if p.exists():
                try:
                    raw = json.loads(p.read_text(encoding="utf-8"))
                    self._memories = raw if isinstance(raw, dict) else {}
                except Exception:
                    self._memories = {}

    def _save_memories(self) -> None:
        if self._state_file:
            p = Path(self._state_file)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(self._memories, ensure_ascii=False, indent=2), encoding="utf-8")

    def _load_audit_log(self) -> None:
        if self._audit_file:
            p = Path(self._audit_file)
            if p.exists():
                try:
                    raw = json.loads(p.read_text(encoding="utf-8"))
                    self._audit_log = raw if isinstance(raw, list) else []
                except Exception:
                    self._audit_log = []

    def _save_audit_log(self) -> None:
        if self._audit_file:
            p = Path(self._audit_file)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(self._audit_log, ensure_ascii=False, indent=2), encoding="utf-8")

    def _append_audit(
        self,
        action: str,
        *,
        key: str | None = None,
        query: str | None = None,
        metadata_provided: dict | None = None,
        result_count: int | None = None,
    ) -> dict[str, Any]:
        entry: dict[str, Any] = {
            "timestamp": self._utc_now(),
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
        self._audit_log.append(entry)
        self._save_audit_log()
        return entry

    @staticmethod
    def _append_access_log(mem: dict, action: str) -> None:
        if "access_log" not in mem:
            mem["access_log"] = []
        mem["access_log"].append({
            "action": action,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })


if __name__ == "__main__":
    MemoryMCPServer.main()
