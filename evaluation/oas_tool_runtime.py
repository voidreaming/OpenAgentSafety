#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


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


def default_state() -> dict[str, Any]:
    return {
        "email": {"threads": [], "outbox": []},
        "calendar": {"events": []},
        "docs": {"documents": []},
        "files": {"files": []},
        "contacts": {"entries": []},
        "social_media": {"threads": [], "posts": [], "messages": []},
    }


def merge_state(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            merged = dict(out[k])
            merged.update(v)
            out[k] = merged
        else:
            out[k] = v
    return out


def load_state(state_path: str, seed_state_path: str | None) -> dict[str, Any]:
    state_file = Path(state_path)
    if state_file.exists():
        with state_file.open("r", encoding="utf-8") as f:
            raw = json.load(f)
        if isinstance(raw, dict) and "stores" in raw and isinstance(raw["stores"], dict):
            return raw["stores"]
        if isinstance(raw, dict):
            return raw
        return default_state()

    stores = default_state()
    if seed_state_path:
        seed_file = Path(seed_state_path)
        if seed_file.exists():
            with seed_file.open("r", encoding="utf-8") as f:
                seed_raw = json.load(f)
            if isinstance(seed_raw, dict):
                if "stores" in seed_raw and isinstance(seed_raw["stores"], dict):
                    stores = merge_state(stores, seed_raw["stores"])
                else:
                    stores = merge_state(stores, seed_raw)
    return stores


def save_state(stores: dict[str, Any], state_path: str) -> None:
    state_file = Path(state_path)
    state_file.parent.mkdir(parents=True, exist_ok=True)
    with state_file.open("w", encoding="utf-8") as f:
        json.dump({"stores": stores}, f, ensure_ascii=False, indent=2)


class OASToolRuntime:
    TOOL_ALIASES = {
        "notion.search": "docs.search",
        "notion.read": "docs.read",
        "notion.write": "docs.write",
        "drive.list": "files.list",
        "drive.read": "files.read",
        "drive.write": "files.write",
    }

    def __init__(self, stores: dict[str, Any]) -> None:
        self.stores = stores

    def call(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        canonical_tool = self.TOOL_ALIASES.get(tool, tool)
        dispatch = {
            "email.search_threads": self.email_search_threads,
            "email.read_thread": self.email_read_thread,
            "email.send_email": self.email_send_email,
            "calendar.search_events": self.calendar_search_events,
            "calendar.read_event": self.calendar_read_event,
            "docs.search": self.docs_search,
            "docs.read": self.docs_read,
            "docs.write": self.docs_write,
            "files.list": self.files_list,
            "files.read": self.files_read,
            "files.write": self.files_write,
            "contacts.lookup": self.contacts_lookup,
            "social_media.list_threads": self.social_media_list_threads,
            "social_media.read_thread": self.social_media_read_thread,
            "social_media.post": self.social_media_post,
            "social_media.send_message": self.social_media_send_message,
        }
        fn = dispatch.get(canonical_tool)
        if fn is None:
            return tool_response(
                tool=canonical_tool,
                source="oas_tool_runtime",
                result={},
                ok=False,
                error=f"Unknown tool: {tool}",
            )
        try:
            return fn(**args)
        except TypeError as e:
            return tool_response(
                tool=canonical_tool,
                source="oas_tool_runtime",
                result={},
                ok=False,
                error=f"Invalid arguments for {canonical_tool}: {e}",
            )

    # Email
    def email_search_threads(self, query: str) -> dict[str, Any]:
        q = query.lower().strip()
        hits: list[dict[str, Any]] = []
        provenance: list[dict[str, Any]] = []
        for thread in self.stores["email"]["threads"]:
            tid = thread.get("thread_id", "")
            messages = thread.get("messages", [])
            haystack_parts = [str(tid).lower()]
            for msg in messages:
                haystack_parts.extend(
                    [
                        str(msg.get("subject", "")).lower(),
                        str(msg.get("body", "")).lower(),
                        str(msg.get("from", "")).lower(),
                        " ".join(msg.get("to", [])).lower(),
                    ]
                )
            haystack = "\n".join(haystack_parts)
            if not q or q in haystack:
                hits.append(
                    {
                        "thread_id": tid,
                        "participants": thread.get("participants", []),
                        "message_count": len(messages),
                    }
                )
                provenance.append({"store": "email.threads", "thread_id": tid})
        return tool_response(
            tool="email.search_threads",
            source="email",
            result={"query": query, "threads": hits},
            provenance=provenance,
        )

    def email_read_thread(self, thread_id: str) -> dict[str, Any]:
        for thread in self.stores["email"]["threads"]:
            if thread.get("thread_id") == thread_id:
                return tool_response(
                    tool="email.read_thread",
                    source="email",
                    result={"thread": thread},
                    provenance=[{"store": "email.threads", "thread_id": thread_id}],
                )
        return tool_response(
            tool="email.read_thread",
            source="email",
            result={},
            ok=False,
            error=f"Thread not found: {thread_id}",
        )

    def email_send_email(
        self,
        to: list[str],
        subject: str,
        body: str,
        cc: list[str] | None = None,
        bcc: list[str] | None = None,
        attachments: list[str] | None = None,
    ) -> dict[str, Any]:
        cc = cc or []
        bcc = bcc or []
        attachments = attachments or []
        outbox = self.stores["email"]["outbox"]
        message_id = f"sent_{len(outbox) + 1:04d}"
        msg = {
            "message_id": message_id,
            "to": [normalize_email(x) for x in to],
            "cc": [normalize_email(x) for x in cc],
            "bcc": [normalize_email(x) for x in bcc],
            "subject": subject,
            "body": body,
            "attachments": attachments,
            "timestamp": utc_now_iso(),
        }
        outbox.append(msg)
        return tool_response(
            tool="email.send_email",
            source="email.outbox",
            result={"message": msg},
            classification=["send_email"],
            provenance=[{"store": "email.outbox", "message_id": message_id}],
        )

    # Calendar
    def calendar_search_events(self, query: str, start: str = "", end: str = "") -> dict[str, Any]:
        q = query.lower().strip()
        events: list[dict[str, Any]] = []
        provenance: list[dict[str, Any]] = []
        for event in self.stores["calendar"]["events"]:
            eid = str(event.get("event_id", ""))
            title = str(event.get("title", ""))
            desc = str(event.get("description", ""))
            haystack = f"{eid}\n{title}\n{desc}".lower()
            if q and q not in haystack:
                continue
            events.append(event)
            provenance.append({"store": "calendar.events", "event_id": eid})
        return tool_response(
            tool="calendar.search_events",
            source="calendar",
            result={"query": query, "start": start, "end": end, "events": events},
            provenance=provenance,
        )

    def calendar_read_event(self, event_id: str) -> dict[str, Any]:
        for event in self.stores["calendar"]["events"]:
            if event.get("event_id") == event_id:
                return tool_response(
                    tool="calendar.read_event",
                    source="calendar",
                    result={"event": event},
                    provenance=[{"store": "calendar.events", "event_id": event_id}],
                )
        return tool_response(
            tool="calendar.read_event",
            source="calendar",
            result={},
            ok=False,
            error=f"Event not found: {event_id}",
        )

    # Docs / Notion
    def docs_search(self, query: str) -> dict[str, Any]:
        q = query.lower().strip()
        docs: list[dict[str, Any]] = []
        provenance: list[dict[str, Any]] = []
        for doc in self.stores["docs"]["documents"]:
            did = str(doc.get("doc_id", ""))
            title = str(doc.get("title", ""))
            content = str(doc.get("content", ""))
            haystack = f"{did}\n{title}\n{content}".lower()
            if q and q not in haystack:
                continue
            docs.append({"doc_id": did, "title": title, "labels": doc.get("labels", [])})
            provenance.append({"store": "docs.documents", "doc_id": did})
        return tool_response(
            tool="docs.search",
            source="docs",
            result={"query": query, "documents": docs},
            provenance=provenance,
        )

    def docs_read(self, doc_id: str) -> dict[str, Any]:
        for doc in self.stores["docs"]["documents"]:
            if doc.get("doc_id") == doc_id:
                return tool_response(
                    tool="docs.read",
                    source="docs",
                    result={"document": doc},
                    provenance=[{"store": "docs.documents", "doc_id": doc_id}],
                )
        return tool_response(
            tool="docs.read",
            source="docs",
            result={},
            ok=False,
            error=f"Document not found: {doc_id}",
        )

    def docs_write(self, doc_id: str, content: str) -> dict[str, Any]:
        for doc in self.stores["docs"]["documents"]:
            if doc.get("doc_id") == doc_id:
                doc["content"] = content
                doc["updated_at"] = utc_now_iso()
                return tool_response(
                    tool="docs.write",
                    source="docs",
                    result={"document": doc},
                    classification=["write_document"],
                    provenance=[{"store": "docs.documents", "doc_id": doc_id}],
                )
        new_doc = {
            "doc_id": doc_id,
            "title": doc_id,
            "content": content,
            "labels": [],
            "updated_at": utc_now_iso(),
        }
        self.stores["docs"]["documents"].append(new_doc)
        return tool_response(
            tool="docs.write",
            source="docs",
            result={"document": new_doc},
            classification=["write_document", "create_document"],
            provenance=[{"store": "docs.documents", "doc_id": doc_id}],
        )

    # Drive / Files
    def files_list(self, path: str) -> dict[str, Any]:
        entries = []
        for f in self.stores["files"]["files"]:
            fpath = str(f.get("path", ""))
            if path == "/" or fpath.startswith(path):
                entries.append({"path": fpath, "labels": f.get("labels", [])})
        return tool_response(
            tool="files.list",
            source="files",
            result={"path": path, "entries": entries},
            provenance=[{"store": "files.files", "path": x["path"]} for x in entries],
        )

    def files_read(self, path: str) -> dict[str, Any]:
        for f in self.stores["files"]["files"]:
            if f.get("path") == path:
                return tool_response(
                    tool="files.read",
                    source="files",
                    result={"file": f},
                    provenance=[{"store": "files.files", "path": path}],
                )
        return tool_response(
            tool="files.read",
            source="files",
            result={},
            ok=False,
            error=f"File not found: {path}",
        )

    def files_write(self, path: str, content: str) -> dict[str, Any]:
        for f in self.stores["files"]["files"]:
            if f.get("path") == path:
                f["content"] = content
                f["updated_at"] = utc_now_iso()
                return tool_response(
                    tool="files.write",
                    source="files",
                    result={"file": f},
                    classification=["write_file"],
                    provenance=[{"store": "files.files", "path": path}],
                )
        new_file = {
            "path": path,
            "content": content,
            "labels": [],
            "updated_at": utc_now_iso(),
        }
        self.stores["files"]["files"].append(new_file)
        return tool_response(
            tool="files.write",
            source="files",
            result={"file": new_file},
            classification=["write_file", "create_file"],
            provenance=[{"store": "files.files", "path": path}],
        )

    # Contacts
    def contacts_lookup(self, name_or_email: str) -> dict[str, Any]:
        q = name_or_email.lower().strip()
        matches = []
        provenance: list[dict[str, Any]] = []
        for entry in self.stores["contacts"]["entries"]:
            name = str(entry.get("name", "")).lower()
            email = normalize_email(str(entry.get("email", "")))
            if q in name or q in email:
                matches.append(entry)
                provenance.append(
                    {
                        "store": "contacts.entries",
                        "name": entry.get("name"),
                        "email": entry.get("email"),
                    }
                )
        return tool_response(
            tool="contacts.lookup",
            source="contacts",
            result={"query": name_or_email, "matches": matches},
            provenance=provenance,
        )

    # Social media
    def social_media_list_threads(self, query: str = "") -> dict[str, Any]:
        q = query.lower().strip()
        threads = []
        provenance: list[dict[str, Any]] = []
        for thread in self.stores["social_media"]["threads"]:
            tid = str(thread.get("thread_id", ""))
            title = str(thread.get("title", ""))
            haystack = f"{tid}\n{title}".lower()
            if q and q not in haystack:
                continue
            threads.append(thread)
            provenance.append({"store": "social_media.threads", "thread_id": tid})
        return tool_response(
            tool="social_media.list_threads",
            source="social_media",
            result={"query": query, "threads": threads},
            provenance=provenance,
        )

    def social_media_read_thread(self, thread_id: str) -> dict[str, Any]:
        for thread in self.stores["social_media"]["threads"]:
            if thread.get("thread_id") == thread_id:
                return tool_response(
                    tool="social_media.read_thread",
                    source="social_media",
                    result={"thread": thread},
                    provenance=[{"store": "social_media.threads", "thread_id": thread_id}],
                )
        return tool_response(
            tool="social_media.read_thread",
            source="social_media",
            result={},
            ok=False,
            error=f"Thread not found: {thread_id}",
        )

    def social_media_post(self, content: str, visibility: str = "public") -> dict[str, Any]:
        posts = self.stores["social_media"]["posts"]
        post_id = f"post_{len(posts) + 1:04d}"
        post = {
            "post_id": post_id,
            "content": content,
            "visibility": visibility,
            "timestamp": utc_now_iso(),
        }
        posts.append(post)
        return tool_response(
            tool="social_media.post",
            source="social_media.posts",
            result={"post": post},
            classification=["social_post"],
            provenance=[{"store": "social_media.posts", "post_id": post_id}],
        )

    def social_media_send_message(self, to: str, body: str) -> dict[str, Any]:
        messages = self.stores["social_media"]["messages"]
        message_id = f"dm_{len(messages) + 1:04d}"
        msg = {
            "message_id": message_id,
            "to": to,
            "body": body,
            "timestamp": utc_now_iso(),
        }
        messages.append(msg)
        return tool_response(
            tool="social_media.send_message",
            source="social_media.messages",
            result={"message": msg},
            classification=["social_dm"],
            provenance=[{"store": "social_media.messages", "message_id": message_id}],
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="OAS custom tool runtime.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    call = subparsers.add_parser("call")
    call.add_argument("--tool", required=True, type=str)
    call.add_argument("--args-json", default="{}", type=str)
    call.add_argument("--state", required=True, type=str)
    call.add_argument("--seed-state", default="", type=str)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if args.command != "call":
        return 1

    try:
        call_args = json.loads(args.args_json)
        if not isinstance(call_args, dict):
            raise ValueError("args-json must decode to an object")
    except Exception as e:
        print(
            json.dumps(
                tool_response(
                    tool=args.tool,
                    source="oas_tool_runtime",
                    result={},
                    ok=False,
                    error=f"Failed to parse --args-json: {e}",
                ),
                ensure_ascii=False,
            )
        )
        return 2

    stores = load_state(args.state, args.seed_state or None)
    runtime = OASToolRuntime(stores)
    result = runtime.call(args.tool, call_args)
    save_state(runtime.stores, args.state)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("ok") else 3


if __name__ == "__main__":
    raise SystemExit(main())
