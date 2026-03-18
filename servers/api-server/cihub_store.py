from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _to_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _from_json(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except Exception:
        return default


def _norm_email(value: str) -> str:
    return value.strip().lower()


class CIHubStore:
    def __init__(self, db_path: str | None = None) -> None:
        self.db_path = db_path or os.getenv("CIHUB_DB_PATH", "/workspace/cihub.sqlite")
        db_dir = os.path.dirname(self.db_path)
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    scenario_id TEXT,
                    task_id TEXT,
                    status TEXT DEFAULT 'active',
                    seeded INTEGER DEFAULT 0,
                    state_version INTEGER DEFAULT 0,
                    created_at TEXT,
                    updated_at TEXT
                );

                CREATE TABLE IF NOT EXISTS contacts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT,
                    name TEXT,
                    email TEXT,
                    org TEXT,
                    role TEXT,
                    tags_json TEXT,
                    updated_at TEXT
                );

                CREATE TABLE IF NOT EXISTS docs (
                    run_id TEXT,
                    doc_id TEXT,
                    title TEXT,
                    content TEXT,
                    labels_json TEXT,
                    updated_at TEXT,
                    PRIMARY KEY (run_id, doc_id)
                );

                CREATE TABLE IF NOT EXISTS files (
                    run_id TEXT,
                    path TEXT,
                    content TEXT,
                    labels_json TEXT,
                    updated_at TEXT,
                    PRIMARY KEY (run_id, path)
                );

                CREATE TABLE IF NOT EXISTS calendar_events (
                    run_id TEXT,
                    event_id TEXT,
                    title TEXT,
                    description TEXT,
                    start_at TEXT,
                    end_at TEXT,
                    labels_json TEXT,
                    updated_at TEXT,
                    PRIMARY KEY (run_id, event_id)
                );

                CREATE TABLE IF NOT EXISTS email_threads (
                    run_id TEXT,
                    thread_id TEXT,
                    participants_json TEXT,
                    updated_at TEXT,
                    PRIMARY KEY (run_id, thread_id)
                );

                CREATE TABLE IF NOT EXISTS email_messages (
                    run_id TEXT,
                    message_id TEXT,
                    thread_id TEXT,
                    from_addr TEXT,
                    to_json TEXT,
                    cc_json TEXT,
                    bcc_json TEXT,
                    subject TEXT,
                    body TEXT,
                    attachments_json TEXT,
                    direction TEXT,
                    ts TEXT,
                    PRIMARY KEY (run_id, message_id)
                );

                CREATE TABLE IF NOT EXISTS social_threads (
                    run_id TEXT,
                    thread_id TEXT,
                    title TEXT,
                    meta_json TEXT,
                    updated_at TEXT,
                    PRIMARY KEY (run_id, thread_id)
                );

                CREATE TABLE IF NOT EXISTS social_posts (
                    run_id TEXT,
                    post_id TEXT,
                    content TEXT,
                    visibility TEXT,
                    ts TEXT,
                    PRIMARY KEY (run_id, post_id)
                );

                CREATE TABLE IF NOT EXISTS social_messages (
                    run_id TEXT,
                    message_id TEXT,
                    thread_id TEXT,
                    to_user TEXT,
                    body TEXT,
                    ts TEXT,
                    meta_json TEXT,
                    PRIMARY KEY (run_id, message_id)
                );

                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT,
                    step_id INTEGER,
                    actor TEXT,
                    tool_name TEXT,
                    args_json TEXT,
                    result_json TEXT,
                    provenance_json TEXT,
                    verdict_json TEXT,
                    ts TEXT
                );

                CREATE TABLE IF NOT EXISTS agent_memories (
                    run_id TEXT,
                    key TEXT,
                    value TEXT,
                    tags_json TEXT,
                    created_at TEXT,
                    updated_at TEXT,
                    PRIMARY KEY (run_id, key)
                );
                """
            )

    def healthcheck(self) -> bool:
        try:
            with self._connect() as conn:
                conn.execute("SELECT 1")
            return True
        except Exception:
            return False

    def _ensure_run(self, run_id: str, scenario_id: str = "", task_id: str = "") -> None:
        now = now_iso()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO runs (run_id, scenario_id, task_id, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET updated_at=excluded.updated_at
                """,
                (run_id, scenario_id, task_id, now, now),
            )

    def _get_seeded(self, run_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT seeded FROM runs WHERE run_id=?",
                (run_id,),
            ).fetchone()
            return bool(row and int(row["seeded"]) == 1)

    def _set_seeded(self, run_id: str, seeded: bool) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE runs SET seeded=?, updated_at=? WHERE run_id=?",
                (1 if seeded else 0, now_iso(), run_id),
            )

    def _bump_state_version(self, run_id: str) -> int:
        with self._connect() as conn:
            conn.execute(
                "UPDATE runs SET state_version=state_version+1, updated_at=? WHERE run_id=?",
                (now_iso(), run_id),
            )
            row = conn.execute(
                "SELECT state_version FROM runs WHERE run_id=?",
                (run_id,),
            ).fetchone()
            return int(row["state_version"]) if row else 0

    def create_run(self, run_id: str, scenario_id: str = "", task_id: str = "") -> dict[str, Any]:
        self._ensure_run(run_id, scenario_id=scenario_id, task_id=task_id)
        return {"run_id": run_id, "scenario_id": scenario_id, "task_id": task_id}

    def reset_all(self) -> None:
        with self._connect() as conn:
            for table in [
                "contacts",
                "docs",
                "files",
                "calendar_events",
                "email_threads",
                "email_messages",
                "social_threads",
                "social_posts",
                "social_messages",
                "agent_memories",
                "audit_events",
                "runs",
            ]:
                conn.execute(f"DELETE FROM {table}")

    def reset_run(self, run_id: str) -> None:
        with self._connect() as conn:
            for table in [
                "contacts",
                "docs",
                "files",
                "calendar_events",
                "email_threads",
                "email_messages",
                "social_threads",
                "social_posts",
                "social_messages",
                "agent_memories",
                "audit_events",
            ]:
                conn.execute(f"DELETE FROM {table} WHERE run_id=?", (run_id,))
            conn.execute("DELETE FROM runs WHERE run_id=?", (run_id,))

    def seed_run(self, run_id: str, seed_state: dict[str, Any]) -> None:
        stores = seed_state.get("stores", seed_state) if isinstance(seed_state, dict) else {}
        now = now_iso()
        self._ensure_run(run_id)
        with self._connect() as conn:
            # Contacts
            for entry in stores.get("contacts", {}).get("entries", []):
                conn.execute(
                    """
                    INSERT INTO contacts (run_id, name, email, org, role, tags_json, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        str(entry.get("name", "")),
                        _norm_email(str(entry.get("email", ""))),
                        str(entry.get("org", "")),
                        str(entry.get("role", "")),
                        _to_json(entry.get("relationship_tags", [])),
                        now,
                    ),
                )

            # Docs
            for doc in stores.get("docs", {}).get("documents", []):
                conn.execute(
                    """
                    INSERT INTO docs (run_id, doc_id, title, content, labels_json, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(run_id, doc_id) DO UPDATE SET
                        title=excluded.title,
                        content=excluded.content,
                        labels_json=excluded.labels_json,
                        updated_at=excluded.updated_at
                    """,
                    (
                        run_id,
                        str(doc.get("doc_id", "")),
                        str(doc.get("title", "")),
                        str(doc.get("content", "")),
                        _to_json(doc.get("labels", [])),
                        now,
                    ),
                )

            # Files
            for file_obj in stores.get("files", {}).get("files", []):
                conn.execute(
                    """
                    INSERT INTO files (run_id, path, content, labels_json, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(run_id, path) DO UPDATE SET
                        content=excluded.content,
                        labels_json=excluded.labels_json,
                        updated_at=excluded.updated_at
                    """,
                    (
                        run_id,
                        str(file_obj.get("path", "")),
                        str(file_obj.get("content", "")),
                        _to_json(file_obj.get("labels", [])),
                        now,
                    ),
                )

            # Calendar
            for event in stores.get("calendar", {}).get("events", []):
                conn.execute(
                    """
                    INSERT INTO calendar_events (run_id, event_id, title, description, start_at, end_at, labels_json, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(run_id, event_id) DO UPDATE SET
                        title=excluded.title,
                        description=excluded.description,
                        start_at=excluded.start_at,
                        end_at=excluded.end_at,
                        labels_json=excluded.labels_json,
                        updated_at=excluded.updated_at
                    """,
                    (
                        run_id,
                        str(event.get("event_id", "")),
                        str(event.get("title", "")),
                        str(event.get("description", "")),
                        str(event.get("start", "")),
                        str(event.get("end", "")),
                        _to_json(event.get("labels", [])),
                        now,
                    ),
                )

            # Email threads/messages
            for thread in stores.get("email", {}).get("threads", []):
                thread_id = str(thread.get("thread_id", ""))
                conn.execute(
                    """
                    INSERT INTO email_threads (run_id, thread_id, participants_json, updated_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(run_id, thread_id) DO UPDATE SET
                        participants_json=excluded.participants_json,
                        updated_at=excluded.updated_at
                    """,
                    (
                        run_id,
                        thread_id,
                        _to_json(thread.get("participants", [])),
                        now,
                    ),
                )
                for msg in thread.get("messages", []):
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO email_messages (
                            run_id, message_id, thread_id, from_addr,
                            to_json, cc_json, bcc_json, subject, body,
                            attachments_json, direction, ts
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            run_id,
                            str(msg.get("message_id", "")),
                            thread_id,
                            _norm_email(str(msg.get("from", ""))),
                            _to_json(msg.get("to", [])),
                            _to_json(msg.get("cc", [])),
                            _to_json(msg.get("bcc", [])),
                            str(msg.get("subject", "")),
                            str(msg.get("body", "")),
                            _to_json(msg.get("attachments", [])),
                            "inbox",
                            str(msg.get("timestamp", now)),
                        ),
                    )
            for msg in stores.get("email", {}).get("outbox", []):
                conn.execute(
                    """
                    INSERT OR REPLACE INTO email_messages (
                        run_id, message_id, thread_id, from_addr,
                        to_json, cc_json, bcc_json, subject, body,
                        attachments_json, direction, ts
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        str(msg.get("message_id", "")),
                        "__outbox__",
                        _norm_email(str(msg.get("from", "agent@local"))),
                        _to_json(msg.get("to", [])),
                        _to_json(msg.get("cc", [])),
                        _to_json(msg.get("bcc", [])),
                        str(msg.get("subject", "")),
                        str(msg.get("body", "")),
                        _to_json(msg.get("attachments", [])),
                        "outbox",
                        str(msg.get("timestamp", now)),
                    ),
                )

            # Social threads/posts/messages
            for thread in stores.get("social_media", {}).get("threads", []):
                conn.execute(
                    """
                    INSERT OR REPLACE INTO social_threads (run_id, thread_id, title, meta_json, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        str(thread.get("thread_id", "")),
                        str(thread.get("title", "")),
                        _to_json(thread),
                        now,
                    ),
                )
            for post in stores.get("social_media", {}).get("posts", []):
                conn.execute(
                    """
                    INSERT OR REPLACE INTO social_posts (run_id, post_id, content, visibility, ts)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        str(post.get("post_id", "")),
                        str(post.get("content", "")),
                        str(post.get("visibility", "public")),
                        str(post.get("timestamp", now)),
                    ),
                )
            for msg in stores.get("social_media", {}).get("messages", []):
                conn.execute(
                    """
                    INSERT OR REPLACE INTO social_messages (run_id, message_id, thread_id, to_user, body, ts, meta_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        str(msg.get("message_id", "")),
                        str(msg.get("thread_id", "")),
                        str(msg.get("to", "")),
                        str(msg.get("body", "")),
                        str(msg.get("timestamp", now)),
                        _to_json(msg),
                    ),
                )
        self._set_seeded(run_id, True)
        self._bump_state_version(run_id)

    def _audit(
        self,
        run_id: str,
        *,
        step_id: int | None,
        actor: str,
        tool_name: str,
        args: dict[str, Any],
        result: dict[str, Any],
        provenance: list[dict[str, Any]],
        verdict: dict[str, Any] | None = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO audit_events (
                    run_id, step_id, actor, tool_name, args_json,
                    result_json, provenance_json, verdict_json, ts
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    step_id,
                    actor,
                    tool_name,
                    _to_json(args),
                    _to_json(result),
                    _to_json(provenance),
                    _to_json(verdict or {}),
                    now_iso(),
                ),
            )

    def _wrap(
        self,
        *,
        run_id: str,
        tool_name: str,
        source: str,
        result: Any,
        classification: list[str] | None = None,
        provenance: list[dict[str, Any]] | None = None,
        ok: bool = True,
        error: str | None = None,
    ) -> dict[str, Any]:
        state_version = self._bump_state_version(run_id) if ok else 0
        return {
            "ok": ok,
            "tool": tool_name,
            "timestamp": now_iso(),
            "source": source,
            "classification": classification or [],
            "provenance": provenance or [],
            "result": result,
            "error": error,
            "state_version": state_version,
        }

    def run_tool(
        self,
        run_id: str,
        *,
        tool_name: str,
        args: dict[str, Any],
        actor: str = "agent",
        step_id: int | None = None,
        seed_state: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self._ensure_run(run_id)
        if seed_state and not self._get_seeded(run_id):
            self.seed_run(run_id, seed_state)

        dispatch = {
            "email.search_threads": self._email_search_threads,
            "email.read_thread": self._email_read_thread,
            "email.send_email": self._email_send_email,
            "calendar.search_events": self._calendar_search_events,
            "calendar.read_event": self._calendar_read_event,
            "docs.search": self._docs_search,
            "docs.read": self._docs_read,
            "docs.write": self._docs_write,
            "files.list": self._files_list,
            "files.read": self._files_read,
            "files.write": self._files_write,
            "contacts.lookup": self._contacts_lookup,
            "social_media.list_threads": self._social_list_threads,
            "social_media.read_thread": self._social_read_thread,
            "social_media.post": self._social_post,
            "social_media.send_message": self._social_send_message,
            "memory.store": self._memory_store,
            "memory.recall": self._memory_recall,
            "memory.search": self._memory_search,
            "memory.list_memories": self._memory_list,
            "memory.forget": self._memory_forget,
        }
        fn = dispatch.get(tool_name)
        if fn is None:
            wrapped = self._wrap(
                run_id=run_id,
                tool_name=tool_name,
                source="cihub",
                result={},
                ok=False,
                error=f"Unknown tool: {tool_name}",
            )
            self._audit(
                run_id,
                step_id=step_id,
                actor=actor,
                tool_name=tool_name,
                args=args,
                result=wrapped.get("result", {}),
                provenance=wrapped.get("provenance", []),
            )
            return wrapped

        try:
            wrapped = fn(run_id, **args)
        except TypeError as exc:
            wrapped = self._wrap(
                run_id=run_id,
                tool_name=tool_name,
                source="cihub",
                result={},
                ok=False,
                error=f"Invalid arguments for {tool_name}: {exc}",
            )
        self._audit(
            run_id,
            step_id=step_id,
            actor=actor,
            tool_name=tool_name,
            args=args,
            result=wrapped.get("result", {}),
            provenance=wrapped.get("provenance", []),
        )
        return wrapped

    # ---------- Tool handlers ----------
    def _email_search_threads(self, run_id: str, query: str) -> dict[str, Any]:
        q = query.lower().strip()
        hits: list[dict[str, Any]] = []
        provenance: list[dict[str, Any]] = []
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT t.thread_id, t.participants_json, COUNT(m.message_id) AS message_count
                FROM email_threads t
                LEFT JOIN email_messages m ON m.run_id=t.run_id AND m.thread_id=t.thread_id
                WHERE t.run_id=?
                GROUP BY t.thread_id, t.participants_json
                """,
                (run_id,),
            ).fetchall()
            for row in rows:
                thread_id = str(row["thread_id"])
                if q:
                    message_rows = conn.execute(
                        """
                        SELECT subject, body, from_addr, to_json
                        FROM email_messages
                        WHERE run_id=? AND thread_id=?
                        """,
                        (run_id, thread_id),
                    ).fetchall()
                    haystack = thread_id.lower()
                    for mr in message_rows:
                        haystack += "\n" + str(mr["subject"] or "").lower()
                        haystack += "\n" + str(mr["body"] or "").lower()
                        haystack += "\n" + str(mr["from_addr"] or "").lower()
                        haystack += "\n" + " ".join(_from_json(mr["to_json"], [])).lower()
                    if q not in haystack:
                        continue
                hits.append(
                    {
                        "thread_id": thread_id,
                        "participants": _from_json(row["participants_json"], []),
                        "message_count": int(row["message_count"] or 0),
                    }
                )
                provenance.append({"store": "email.threads", "thread_id": thread_id})
        return self._wrap(
            run_id=run_id,
            tool_name="email.search_threads",
            source="email",
            result={"query": query, "threads": hits},
            provenance=provenance,
        )

    def _email_read_thread(self, run_id: str, thread_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            trow = conn.execute(
                "SELECT participants_json FROM email_threads WHERE run_id=? AND thread_id=?",
                (run_id, thread_id),
            ).fetchone()
            if trow is None and thread_id != "__outbox__":
                return self._wrap(
                    run_id=run_id,
                    tool_name="email.read_thread",
                    source="email",
                    result={},
                    ok=False,
                    error=f"Thread not found: {thread_id}",
                )
            mrows = conn.execute(
                """
                SELECT message_id, from_addr, to_json, cc_json, bcc_json, subject, body, attachments_json, ts
                FROM email_messages
                WHERE run_id=? AND thread_id=?
                ORDER BY ts ASC
                """,
                (run_id, thread_id),
            ).fetchall()
            messages = []
            for row in mrows:
                messages.append(
                    {
                        "message_id": str(row["message_id"]),
                        "from": str(row["from_addr"] or ""),
                        "to": _from_json(row["to_json"], []),
                        "cc": _from_json(row["cc_json"], []),
                        "bcc": _from_json(row["bcc_json"], []),
                        "subject": str(row["subject"] or ""),
                        "body": str(row["body"] or ""),
                        "attachments": _from_json(row["attachments_json"], []),
                        "timestamp": str(row["ts"] or ""),
                    }
                )
            thread = {
                "thread_id": thread_id,
                "participants": _from_json(trow["participants_json"], []) if trow else [],
                "messages": messages,
            }
        return self._wrap(
            run_id=run_id,
            tool_name="email.read_thread",
            source="email",
            result={"thread": thread},
            provenance=[{"store": "email.threads", "thread_id": thread_id}],
        )

    def _email_send_email(
        self,
        run_id: str,
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
        ts = now_iso()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(1) AS c FROM email_messages WHERE run_id=? AND direction='outbox'",
                (run_id,),
            ).fetchone()
            next_id = int(row["c"] or 0) + 1
            message_id = f"sent_{next_id:04d}"
            thread_id = "__outbox__"
            participants = sorted({_norm_email(x) for x in (to + cc + bcc)})
            conn.execute(
                """
                INSERT OR REPLACE INTO email_threads (run_id, thread_id, participants_json, updated_at)
                VALUES (?, ?, ?, ?)
                """,
                (run_id, thread_id, _to_json(participants), ts),
            )
            conn.execute(
                """
                INSERT INTO email_messages (
                    run_id, message_id, thread_id, from_addr,
                    to_json, cc_json, bcc_json, subject, body,
                    attachments_json, direction, ts
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    message_id,
                    thread_id,
                    "agent@local",
                    _to_json([_norm_email(x) for x in to]),
                    _to_json([_norm_email(x) for x in cc]),
                    _to_json([_norm_email(x) for x in bcc]),
                    subject,
                    body,
                    _to_json(attachments),
                    "outbox",
                    ts,
                ),
            )
        msg = {
            "message_id": message_id,
            "to": [_norm_email(x) for x in to],
            "cc": [_norm_email(x) for x in cc],
            "bcc": [_norm_email(x) for x in bcc],
            "subject": subject,
            "body": body,
            "attachments": attachments,
            "timestamp": ts,
        }
        return self._wrap(
            run_id=run_id,
            tool_name="email.send_email",
            source="email.outbox",
            result={"message": msg},
            classification=["send_email"],
            provenance=[{"store": "email_messages", "message_id": message_id}],
        )

    def _calendar_search_events(
        self, run_id: str, query: str, start: str = "", end: str = ""
    ) -> dict[str, Any]:
        q = query.lower().strip()
        events = []
        provenance = []
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT event_id, title, description, start_at, end_at, labels_json
                FROM calendar_events
                WHERE run_id=?
                """,
                (run_id,),
            ).fetchall()
            for row in rows:
                haystack = (
                    f"{row['event_id']}\n{row['title']}\n{row['description']}"
                ).lower()
                if q and q not in haystack:
                    continue
                event = {
                    "event_id": str(row["event_id"]),
                    "title": str(row["title"] or ""),
                    "description": str(row["description"] or ""),
                    "start": str(row["start_at"] or ""),
                    "end": str(row["end_at"] or ""),
                    "labels": _from_json(row["labels_json"], []),
                }
                events.append(event)
                provenance.append({"store": "calendar_events", "event_id": event["event_id"]})
        return self._wrap(
            run_id=run_id,
            tool_name="calendar.search_events",
            source="calendar",
            result={"query": query, "start": start, "end": end, "events": events},
            provenance=provenance,
        )

    def _calendar_read_event(self, run_id: str, event_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT event_id, title, description, start_at, end_at, labels_json
                FROM calendar_events
                WHERE run_id=? AND event_id=?
                """,
                (run_id, event_id),
            ).fetchone()
            if row is None:
                return self._wrap(
                    run_id=run_id,
                    tool_name="calendar.read_event",
                    source="calendar",
                    result={},
                    ok=False,
                    error=f"Event not found: {event_id}",
                )
            event = {
                "event_id": str(row["event_id"]),
                "title": str(row["title"] or ""),
                "description": str(row["description"] or ""),
                "start": str(row["start_at"] or ""),
                "end": str(row["end_at"] or ""),
                "labels": _from_json(row["labels_json"], []),
            }
        return self._wrap(
            run_id=run_id,
            tool_name="calendar.read_event",
            source="calendar",
            result={"event": event},
            provenance=[{"store": "calendar_events", "event_id": event_id}],
        )

    def _docs_search(self, run_id: str, query: str) -> dict[str, Any]:
        q = query.lower().strip()
        docs = []
        provenance = []
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT doc_id, title, content, labels_json FROM docs WHERE run_id=?",
                (run_id,),
            ).fetchall()
            for row in rows:
                haystack = f"{row['doc_id']}\n{row['title']}\n{row['content']}".lower()
                if q and q not in haystack:
                    continue
                docs.append(
                    {
                        "doc_id": str(row["doc_id"]),
                        "title": str(row["title"] or ""),
                        "labels": _from_json(row["labels_json"], []),
                    }
                )
                provenance.append({"store": "docs", "doc_id": str(row["doc_id"])})
        return self._wrap(
            run_id=run_id,
            tool_name="docs.search",
            source="docs",
            result={"query": query, "documents": docs},
            provenance=provenance,
        )

    def _docs_read(self, run_id: str, doc_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT doc_id, title, content, labels_json FROM docs WHERE run_id=? AND doc_id=?",
                (run_id, doc_id),
            ).fetchone()
            if row is None:
                return self._wrap(
                    run_id=run_id,
                    tool_name="docs.read",
                    source="docs",
                    result={},
                    ok=False,
                    error=f"Document not found: {doc_id}",
                )
            doc = {
                "doc_id": str(row["doc_id"]),
                "title": str(row["title"] or ""),
                "content": str(row["content"] or ""),
                "labels": _from_json(row["labels_json"], []),
            }
        return self._wrap(
            run_id=run_id,
            tool_name="docs.read",
            source="docs",
            result={"document": doc},
            provenance=[{"store": "docs", "doc_id": doc_id}],
        )

    def _docs_write(self, run_id: str, doc_id: str, content: str) -> dict[str, Any]:
        ts = now_iso()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT labels_json FROM docs WHERE run_id=? AND doc_id=?",
                (run_id, doc_id),
            ).fetchone()
            labels = _from_json(row["labels_json"], []) if row else []
            conn.execute(
                """
                INSERT INTO docs (run_id, doc_id, title, content, labels_json, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id, doc_id) DO UPDATE SET
                    content=excluded.content,
                    updated_at=excluded.updated_at
                """,
                (run_id, doc_id, doc_id, content, _to_json(labels), ts),
            )
        return self._wrap(
            run_id=run_id,
            tool_name="docs.write",
            source="docs",
            result={
                "document": {
                    "doc_id": doc_id,
                    "title": doc_id,
                    "content": content,
                    "labels": labels,
                    "updated_at": ts,
                }
            },
            classification=["write_document"],
            provenance=[{"store": "docs", "doc_id": doc_id}],
        )

    def _files_list(self, run_id: str, path: str) -> dict[str, Any]:
        entries = []
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT path, labels_json FROM files WHERE run_id=?",
                (run_id,),
            ).fetchall()
            for row in rows:
                fpath = str(row["path"] or "")
                if path == "/" or fpath.startswith(path):
                    entries.append({"path": fpath, "labels": _from_json(row["labels_json"], [])})
        return self._wrap(
            run_id=run_id,
            tool_name="files.list",
            source="files",
            result={"path": path, "entries": entries},
            provenance=[{"store": "files", "path": item["path"]} for item in entries],
        )

    def _files_read(self, run_id: str, path: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT path, content, labels_json, updated_at FROM files WHERE run_id=? AND path=?",
                (run_id, path),
            ).fetchone()
            if row is None:
                return self._wrap(
                    run_id=run_id,
                    tool_name="files.read",
                    source="files",
                    result={},
                    ok=False,
                    error=f"File not found: {path}",
                )
            file_obj = {
                "path": str(row["path"]),
                "content": str(row["content"] or ""),
                "labels": _from_json(row["labels_json"], []),
                "updated_at": str(row["updated_at"] or ""),
            }
        return self._wrap(
            run_id=run_id,
            tool_name="files.read",
            source="files",
            result={"file": file_obj},
            provenance=[{"store": "files", "path": path}],
        )

    def _files_write(self, run_id: str, path: str, content: str) -> dict[str, Any]:
        ts = now_iso()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT labels_json FROM files WHERE run_id=? AND path=?",
                (run_id, path),
            ).fetchone()
            labels = _from_json(row["labels_json"], []) if row else []
            conn.execute(
                """
                INSERT INTO files (run_id, path, content, labels_json, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(run_id, path) DO UPDATE SET
                    content=excluded.content,
                    updated_at=excluded.updated_at
                """,
                (run_id, path, content, _to_json(labels), ts),
            )
        return self._wrap(
            run_id=run_id,
            tool_name="files.write",
            source="files",
            result={
                "file": {
                    "path": path,
                    "content": content,
                    "labels": labels,
                    "updated_at": ts,
                }
            },
            classification=["write_file"],
            provenance=[{"store": "files", "path": path}],
        )

    def _contacts_lookup(self, run_id: str, name_or_email: str) -> dict[str, Any]:
        q = name_or_email.lower().strip()
        matches = []
        provenance = []
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT name, email, org, role, tags_json
                FROM contacts
                WHERE run_id=?
                """,
                (run_id,),
            ).fetchall()
            for row in rows:
                name = str(row["name"] or "")
                email = str(row["email"] or "")
                if q and q not in name.lower() and q not in email.lower():
                    continue
                match = {
                    "name": name,
                    "email": email,
                    "org": str(row["org"] or ""),
                    "role": str(row["role"] or ""),
                    "relationship_tags": _from_json(row["tags_json"], []),
                }
                matches.append(match)
                provenance.append({"store": "contacts", "name": name, "email": email})
        return self._wrap(
            run_id=run_id,
            tool_name="contacts.lookup",
            source="contacts",
            result={"query": name_or_email, "matches": matches},
            provenance=provenance,
        )

    def _social_list_threads(self, run_id: str, query: str = "") -> dict[str, Any]:
        q = query.lower().strip()
        threads = []
        provenance = []
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT thread_id, title, meta_json FROM social_threads WHERE run_id=?",
                (run_id,),
            ).fetchall()
            for row in rows:
                thread_id = str(row["thread_id"])
                title = str(row["title"] or "")
                haystack = f"{thread_id}\n{title}".lower()
                if q and q not in haystack:
                    continue
                thread = _from_json(row["meta_json"], {})
                if not thread:
                    thread = {"thread_id": thread_id, "title": title}
                threads.append(thread)
                provenance.append({"store": "social_threads", "thread_id": thread_id})
        return self._wrap(
            run_id=run_id,
            tool_name="social_media.list_threads",
            source="social_media",
            result={"query": query, "threads": threads},
            provenance=provenance,
        )

    def _social_read_thread(self, run_id: str, thread_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT meta_json FROM social_threads WHERE run_id=? AND thread_id=?",
                (run_id, thread_id),
            ).fetchone()
            if row is None:
                return self._wrap(
                    run_id=run_id,
                    tool_name="social_media.read_thread",
                    source="social_media",
                    result={},
                    ok=False,
                    error=f"Thread not found: {thread_id}",
                )
            thread = _from_json(row["meta_json"], {"thread_id": thread_id})
        return self._wrap(
            run_id=run_id,
            tool_name="social_media.read_thread",
            source="social_media",
            result={"thread": thread},
            provenance=[{"store": "social_threads", "thread_id": thread_id}],
        )

    def _social_post(self, run_id: str, content: str, visibility: str = "public") -> dict[str, Any]:
        ts = now_iso()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(1) AS c FROM social_posts WHERE run_id=?",
                (run_id,),
            ).fetchone()
            post_id = f"post_{int(row['c'] or 0) + 1:04d}"
            conn.execute(
                """
                INSERT INTO social_posts (run_id, post_id, content, visibility, ts)
                VALUES (?, ?, ?, ?, ?)
                """,
                (run_id, post_id, content, visibility, ts),
            )
        return self._wrap(
            run_id=run_id,
            tool_name="social_media.post",
            source="social_media.posts",
            result={
                "post": {
                    "post_id": post_id,
                    "content": content,
                    "visibility": visibility,
                    "timestamp": ts,
                }
            },
            classification=["social_post"],
            provenance=[{"store": "social_posts", "post_id": post_id}],
        )

    def _social_send_message(self, run_id: str, to: str, body: str) -> dict[str, Any]:
        ts = now_iso()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(1) AS c FROM social_messages WHERE run_id=?",
                (run_id,),
            ).fetchone()
            message_id = f"dm_{int(row['c'] or 0) + 1:04d}"
            conn.execute(
                """
                INSERT INTO social_messages (run_id, message_id, thread_id, to_user, body, ts, meta_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    message_id,
                    "__dm__",
                    to,
                    body,
                    ts,
                    _to_json({"to": to, "body": body}),
                ),
            )
        return self._wrap(
            run_id=run_id,
            tool_name="social_media.send_message",
            source="social_media.messages",
            result={
                "message": {
                    "message_id": message_id,
                    "to": to,
                    "body": body,
                    "timestamp": ts,
                }
            },
            classification=["social_dm"],
            provenance=[{"store": "social_messages", "message_id": message_id}],
        )

    # ---------- Memory tool handlers ----------
    def _memory_store(self, run_id: str, key: str, value: str,
                      tags: list[str] | None = None) -> dict[str, Any]:
        tags = tags or []
        ts = now_iso()
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT created_at FROM agent_memories WHERE run_id=? AND key=?",
                (run_id, key),
            ).fetchone()
            created = str(existing["created_at"]) if existing else ts
            is_update = existing is not None
            conn.execute(
                """
                INSERT INTO agent_memories (run_id, key, value, tags_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id, key) DO UPDATE SET
                    value=excluded.value,
                    tags_json=excluded.tags_json,
                    updated_at=excluded.updated_at
                """,
                (run_id, key, value, _to_json(tags), created, ts),
            )
        memory = {"key": key, "value": value, "tags": tags,
                  "created_at": created, "updated_at": ts}
        return self._wrap(
            run_id=run_id,
            tool_name="memory.store",
            source="agent_memories",
            result={"ok": True, "action": "updated" if is_update else "created",
                    "memory": memory},
            classification=["memory_store"],
            provenance=[{"store": "agent_memories", "key": key}],
        )

    def _memory_recall(self, run_id: str, key: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT key, value, tags_json, created_at, updated_at FROM agent_memories WHERE run_id=? AND key=?",
                (run_id, key),
            ).fetchone()
            if row is None:
                return self._wrap(
                    run_id=run_id,
                    tool_name="memory.recall",
                    source="agent_memories",
                    result={"ok": False, "error": f"Memory not found: {key}"},
                    ok=False,
                    error=f"Memory not found: {key}",
                )
            memory = {
                "key": str(row["key"]),
                "value": str(row["value"]),
                "tags": _from_json(row["tags_json"], []),
                "created_at": str(row["created_at"]),
                "updated_at": str(row["updated_at"]),
            }
        return self._wrap(
            run_id=run_id,
            tool_name="memory.recall",
            source="agent_memories",
            result={"ok": True, "memory": memory},
            classification=["memory_recall"],
            provenance=[{"store": "agent_memories", "key": key}],
        )

    def _memory_search(self, run_id: str, query: str) -> dict[str, Any]:
        q = query.lower().strip()
        matches = []
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT key, value, tags_json, created_at, updated_at FROM agent_memories WHERE run_id=?",
                (run_id,),
            ).fetchall()
            for row in rows:
                haystack = f"{row['key']}\n{row['value']}".lower()
                if q and q not in haystack:
                    continue
                matches.append({
                    "key": str(row["key"]),
                    "value": str(row["value"]),
                    "tags": _from_json(row["tags_json"], []),
                    "created_at": str(row["created_at"]),
                    "updated_at": str(row["updated_at"]),
                })
        return self._wrap(
            run_id=run_id,
            tool_name="memory.search",
            source="agent_memories",
            result={"query": query, "matches": matches, "count": len(matches)},
        )

    def _memory_list(self, run_id: str, tag: str = "") -> dict[str, Any]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT key, value, tags_json, created_at, updated_at FROM agent_memories WHERE run_id=?",
                (run_id,),
            ).fetchall()
            memories = []
            for row in rows:
                tags = _from_json(row["tags_json"], [])
                if tag and tag not in tags:
                    continue
                memories.append({
                    "key": str(row["key"]),
                    "value": str(row["value"]),
                    "tags": tags,
                    "created_at": str(row["created_at"]),
                    "updated_at": str(row["updated_at"]),
                })
        return self._wrap(
            run_id=run_id,
            tool_name="memory.list_memories",
            source="agent_memories",
            result={"memories": memories, "count": len(memories)},
        )

    def _memory_forget(self, run_id: str, key: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT key, value, tags_json FROM agent_memories WHERE run_id=? AND key=?",
                (run_id, key),
            ).fetchone()
            if row is None:
                return self._wrap(
                    run_id=run_id,
                    tool_name="memory.forget",
                    source="agent_memories",
                    result={"ok": False, "error": f"Memory not found: {key}"},
                    ok=False,
                    error=f"Memory not found: {key}",
                )
            removed = {
                "key": str(row["key"]),
                "value": str(row["value"]),
                "tags": _from_json(row["tags_json"], []),
            }
            conn.execute(
                "DELETE FROM agent_memories WHERE run_id=? AND key=?",
                (run_id, key),
            )
        return self._wrap(
            run_id=run_id,
            tool_name="memory.forget",
            source="agent_memories",
            result={"ok": True, "removed": removed},
            classification=["memory_forget"],
            provenance=[{"store": "agent_memories", "key": key}],
        )

    # ---------- Memory CRUD for REST endpoints ----------
    def list_memories(self, run_id: str, tag: str = "") -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT key, value, tags_json, created_at, updated_at FROM agent_memories WHERE run_id=?",
                (run_id,),
            ).fetchall()
            memories = []
            for row in rows:
                tags = _from_json(row["tags_json"], [])
                if tag and tag not in tags:
                    continue
                memories.append({
                    "key": str(row["key"]),
                    "value": str(row["value"]),
                    "tags": tags,
                    "created_at": str(row["created_at"]),
                    "updated_at": str(row["updated_at"]),
                })
        return memories

    def get_memory(self, run_id: str, key: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT key, value, tags_json, created_at, updated_at FROM agent_memories WHERE run_id=? AND key=?",
                (run_id, key),
            ).fetchone()
            if row is None:
                return None
            return {
                "key": str(row["key"]),
                "value": str(row["value"]),
                "tags": _from_json(row["tags_json"], []),
                "created_at": str(row["created_at"]),
                "updated_at": str(row["updated_at"]),
            }

    def export_state(self, run_id: str) -> dict[str, Any]:
        stores: dict[str, Any] = {
            "email": {"threads": [], "outbox": []},
            "calendar": {"events": []},
            "docs": {"documents": []},
            "files": {"files": []},
            "contacts": {"entries": []},
            "social_media": {"threads": [], "posts": [], "messages": []},
        }
        with self._connect() as conn:
            # Email
            thread_rows = conn.execute(
                "SELECT thread_id, participants_json FROM email_threads WHERE run_id=?",
                (run_id,),
            ).fetchall()
            for t in thread_rows:
                thread_id = str(t["thread_id"])
                mrows = conn.execute(
                    """
                    SELECT message_id, from_addr, to_json, cc_json, bcc_json, subject, body, attachments_json, ts, direction
                    FROM email_messages
                    WHERE run_id=? AND thread_id=?
                    ORDER BY ts ASC
                    """,
                    (run_id, thread_id),
                ).fetchall()
                msgs = []
                for m in mrows:
                    msg = {
                        "message_id": str(m["message_id"]),
                        "from": str(m["from_addr"] or ""),
                        "to": _from_json(m["to_json"], []),
                        "cc": _from_json(m["cc_json"], []),
                        "bcc": _from_json(m["bcc_json"], []),
                        "subject": str(m["subject"] or ""),
                        "body": str(m["body"] or ""),
                        "attachments": _from_json(m["attachments_json"], []),
                        "timestamp": str(m["ts"] or ""),
                    }
                    if str(m["direction"] or "") == "outbox":
                        stores["email"]["outbox"].append(msg)
                    msgs.append(msg)
                stores["email"]["threads"].append(
                    {
                        "thread_id": thread_id,
                        "participants": _from_json(t["participants_json"], []),
                        "messages": msgs,
                    }
                )

            # Calendar
            for row in conn.execute(
                """
                SELECT event_id, title, description, start_at, end_at, labels_json
                FROM calendar_events WHERE run_id=?
                """,
                (run_id,),
            ).fetchall():
                stores["calendar"]["events"].append(
                    {
                        "event_id": str(row["event_id"]),
                        "title": str(row["title"] or ""),
                        "description": str(row["description"] or ""),
                        "start": str(row["start_at"] or ""),
                        "end": str(row["end_at"] or ""),
                        "labels": _from_json(row["labels_json"], []),
                    }
                )

            # Docs
            for row in conn.execute(
                "SELECT doc_id, title, content, labels_json, updated_at FROM docs WHERE run_id=?",
                (run_id,),
            ).fetchall():
                stores["docs"]["documents"].append(
                    {
                        "doc_id": str(row["doc_id"]),
                        "title": str(row["title"] or ""),
                        "content": str(row["content"] or ""),
                        "labels": _from_json(row["labels_json"], []),
                        "updated_at": str(row["updated_at"] or ""),
                    }
                )

            # Files
            for row in conn.execute(
                "SELECT path, content, labels_json, updated_at FROM files WHERE run_id=?",
                (run_id,),
            ).fetchall():
                stores["files"]["files"].append(
                    {
                        "path": str(row["path"]),
                        "content": str(row["content"] or ""),
                        "labels": _from_json(row["labels_json"], []),
                        "updated_at": str(row["updated_at"] or ""),
                    }
                )

            # Contacts
            for row in conn.execute(
                "SELECT name, email, org, role, tags_json FROM contacts WHERE run_id=?",
                (run_id,),
            ).fetchall():
                stores["contacts"]["entries"].append(
                    {
                        "name": str(row["name"] or ""),
                        "email": str(row["email"] or ""),
                        "org": str(row["org"] or ""),
                        "role": str(row["role"] or ""),
                        "relationship_tags": _from_json(row["tags_json"], []),
                    }
                )

            # Social
            for row in conn.execute(
                "SELECT thread_id, meta_json FROM social_threads WHERE run_id=?",
                (run_id,),
            ).fetchall():
                thread = _from_json(row["meta_json"], {})
                if not thread:
                    thread = {"thread_id": str(row["thread_id"])}
                stores["social_media"]["threads"].append(thread)
            for row in conn.execute(
                "SELECT post_id, content, visibility, ts FROM social_posts WHERE run_id=?",
                (run_id,),
            ).fetchall():
                stores["social_media"]["posts"].append(
                    {
                        "post_id": str(row["post_id"]),
                        "content": str(row["content"] or ""),
                        "visibility": str(row["visibility"] or "public"),
                        "timestamp": str(row["ts"] or ""),
                    }
                )
            for row in conn.execute(
                "SELECT message_id, to_user, body, ts, meta_json FROM social_messages WHERE run_id=?",
                (run_id,),
            ).fetchall():
                msg = _from_json(row["meta_json"], {})
                if not msg:
                    msg = {
                        "message_id": str(row["message_id"]),
                        "to": str(row["to_user"] or ""),
                        "body": str(row["body"] or ""),
                        "timestamp": str(row["ts"] or ""),
                    }
                stores["social_media"]["messages"].append(msg)

            # Agent memories
            memories = []
            for row in conn.execute(
                "SELECT key, value, tags_json, created_at, updated_at FROM agent_memories WHERE run_id=?",
                (run_id,),
            ).fetchall():
                memories.append({
                    "key": str(row["key"]),
                    "value": str(row["value"]),
                    "tags": _from_json(row["tags_json"], []),
                    "created_at": str(row["created_at"]),
                    "updated_at": str(row["updated_at"]),
                })
            stores["agent_memories"] = memories
        return {"stores": stores}

    def list_audit(self, run_id: str, limit: int = 200) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, step_id, actor, tool_name, args_json, result_json, provenance_json, verdict_json, ts
                FROM audit_events
                WHERE run_id=?
                ORDER BY id DESC
                LIMIT ?
                """,
                (run_id, int(limit)),
            ).fetchall()
            events = []
            for row in rows:
                events.append(
                    {
                        "id": int(row["id"]),
                        "step_id": row["step_id"],
                        "actor": str(row["actor"] or ""),
                        "tool_name": str(row["tool_name"] or ""),
                        "args": _from_json(row["args_json"], {}),
                        "result": _from_json(row["result_json"], {}),
                        "provenance": _from_json(row["provenance_json"], []),
                        "verdict": _from_json(row["verdict_json"], {}),
                        "timestamp": str(row["ts"] or ""),
                    }
                )
        return events
