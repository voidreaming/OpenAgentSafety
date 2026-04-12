# pyright: reportMissingImports=false, reportCallIssue=false, reportGeneralTypeIssues=false
#
# The Radicale handler uses the ``caldav`` library, which has no type
# stubs. Pyright resolves every caldav symbol as ``object``, which then
# trips ``reportCallIssue`` (``DAVClient(...)`` is "not callable") and
# ``reportGeneralTypeIssues`` (``caldav.Calendar`` annotations are
# "not a class"). The library is correct at runtime; suppressing these
# three rules at the file level is the project convention (mirrors
# ``mcp_servers/radicale_server.py``). ``reportMissingImports`` is
# kept for fresh checkouts where ``uv sync --dev`` hasn't run yet.
"""Data seeder — injects seed_data/ files into real services.

Reads per-service JSON files from a task's seed_data/ directory and
creates the corresponding records in each service via REST APIs.
"""

from __future__ import annotations

import json
import logging
import smtplib
from dataclasses import dataclass, field
from email.mime.text import MIMEText
from pathlib import Path

import httpx


logger = logging.getLogger("seeder")


def _ical_dt(s: str) -> str:
    """Strip ISO timestamp punctuation for an iCalendar DTSTART/DTEND."""
    return s.replace("-", "").replace(":", "").replace("+", "").split(".")[0]


@dataclass
class CleanupHandle:
    """Tracks seeded records for cleanup after a task run."""

    service: str
    record_ids: list[str] = field(default_factory=list)


@dataclass
class SeedResult:
    """Result of seeding a task's data."""

    handles: list[CleanupHandle] = field(default_factory=list)
    id_map: dict[str, str] = field(default_factory=dict)


class ServiceHandler:
    """Base class for per-service seeding logic."""

    service_name: str = ""

    async def seed(self, records: list[dict]) -> CleanupHandle:
        raise NotImplementedError

    async def cleanup(self, handle: CleanupHandle) -> None:
        pass


class BookStackHandler(ServiceHandler):
    """Seed BookStack pages via REST API."""

    service_name = "bookstack"

    def __init__(self, url: str, token_id: str = "", token_secret: str = ""):
        self.url = url
        self.token_id = token_id
        self.token_secret = token_secret
        self._book_id: int | None = None

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": (f"Token {self.token_id}:{self.token_secret}"),
            "Content-Type": "application/json",
        }

    async def _ensure_book(self) -> int:
        """Get or create the workspace book."""
        if self._book_id is not None:
            return self._book_id

        async with httpx.AsyncClient(timeout=30.0) as client:
            # Check if workspace book exists
            resp = await client.get(
                f"{self.url}/api/books",
                params={"count": 100},
                headers=self._headers(),
            )
            if resp.status_code == 200:
                for book in resp.json().get("data", []):
                    if book.get("name") == "Workspace":
                        book_id = int(book["id"])
                        self._book_id = book_id
                        return book_id

            # Create it
            resp = await client.post(
                f"{self.url}/api/books",
                json={
                    "name": "Workspace",
                    "description": "PrivacyLens workspace",
                },
                headers=self._headers(),
            )
            resp.raise_for_status()
            book_id = int(resp.json()["id"])
            self._book_id = book_id
            logger.info(f"Created workspace book id={book_id}")
            return book_id

    async def seed(self, records: list[dict]) -> CleanupHandle:
        handle = CleanupHandle(service="bookstack")
        book_id = await self._ensure_book()

        async with httpx.AsyncClient(timeout=30.0) as client:
            for record in records:
                title = record.get("title", "Untitled")
                content = record.get("content", "")
                orig_id = record.get("id", "")
                tags = [{"name": t} for t in record.get("tags", [])]
                if orig_id:
                    tags.append({"name": "privacylens_id", "value": orig_id})

                resp = await client.post(
                    f"{self.url}/api/pages",
                    json={
                        "book_id": book_id,
                        "name": title,
                        "markdown": content,
                        "tags": tags,
                    },
                    headers=self._headers(),
                )
                if resp.status_code in (200, 201):
                    page_id = resp.json()["id"]
                    handle.record_ids.append(str(page_id))
                    logger.info(f"Seeded BookStack page: {title} (id={page_id})")
                else:
                    logger.warning(
                        f"Failed to seed page '{title}': "
                        f"{resp.status_code} {resp.text[:200]}"
                    )

        return handle

    async def cleanup(self, handle: CleanupHandle) -> None:
        """Permanently delete pages we created.

        BookStack's ``DELETE /api/pages/{id}`` is a *soft* delete: it
        moves the page to the recycle bin and returns 200. Without a
        follow-up step the recycle bin grows unbounded across runs.
        We:

        1. Soft-delete each tracked page (and actually check the
           response status — httpx doesn't raise on 4xx by default).
        2. Query the recycle bin once and find the entries with
           ``deletable.id`` matching one of our tracked page ids.
        3. ``DELETE /api/recycle-bin/{entry_id}`` for each match to
           permanently remove them. Targeted — we never touch any
           recycle bin contents we didn't just create.
        """
        if not handle.record_ids:
            return
        target_ids = {int(pid) for pid in handle.record_ids}
        async with httpx.AsyncClient(timeout=30.0) as client:
            # Step 1: soft-delete each page (moves to recycle bin).
            for page_id in handle.record_ids:
                try:
                    resp = await client.delete(
                        f"{self.url}/api/pages/{page_id}",
                        headers=self._headers(),
                    )
                except Exception as e:
                    logger.warning(f"Failed to soft-delete page {page_id}: {e}")
                    continue
                if resp.status_code not in (200, 204):
                    logger.warning(
                        f"Soft-delete failed for page {page_id}: "
                        f"HTTP {resp.status_code} {resp.text[:200]}"
                    )

            # Step 2: query the recycle bin and find our entries.
            try:
                resp = await client.get(
                    f"{self.url}/api/recycle-bin",
                    params={"count": 200},
                    headers=self._headers(),
                )
                resp.raise_for_status()
                entries = resp.json().get("data", [])
            except httpx.HTTPError as exc:
                logger.warning(f"Failed to query BookStack recycle bin: {exc}")
                return

            # Step 3: permanently delete the matching entries.
            for entry in entries:
                deletable = entry.get("deletable", {})
                if (
                    deletable.get("type") == "page"
                    and deletable.get("id") in target_ids
                ):
                    entry_id = entry.get("id")
                    try:
                        del_resp = await client.delete(
                            f"{self.url}/api/recycle-bin/{entry_id}",
                            headers=self._headers(),
                        )
                        del_resp.raise_for_status()
                    except httpx.HTTPError as exc:
                        logger.warning(
                            f"Failed to permanently delete recycle bin "
                            f"entry {entry_id} (page {deletable.get('id')}): {exc}"
                        )


class MattermostHandler(ServiceHandler):
    """Seed Mattermost DMs via REST API v4."""

    service_name = "mattermost"

    def __init__(self, url: str, user: str, password: str):
        self.url = url
        self.user = user
        self.password = password
        self._token: str = ""
        self._user_id: str = ""

    async def _login(self) -> None:
        if self._token:
            return
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{self.url}/api/v4/users/login",
                json={
                    "login_id": self.user,
                    "password": self.password,
                },
            )
            resp.raise_for_status()
            self._token = resp.headers["Token"]
            self._user_id = resp.json()["id"]

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"}

    async def seed(self, records: list[dict]) -> CleanupHandle:
        handle = CleanupHandle(service="mattermost")
        await self._login()

        async with httpx.AsyncClient(timeout=30.0) as client:
            # Create users mentioned in records
            usernames = {r.get("sender_id", "") for r in records if r.get("sender_id")}
            for username in usernames:
                if not username:
                    continue
                try:
                    await client.post(
                        f"{self.url}/api/v4/users",
                        json={
                            "email": f"{username}@privacylens.local",
                            "username": username,
                            "password": "Password123!",
                        },
                        headers=self._headers(),
                    )
                except Exception:
                    pass  # User may already exist

            # Post messages
            for record in records:
                message = record.get("message", "")
                if not message:
                    continue
                try:
                    # Get team for posting
                    resp = await client.get(
                        f"{self.url}/api/v4/users/me/teams",
                        headers=self._headers(),
                    )
                    teams = resp.json()
                    if not teams:
                        continue
                    # Post to town-square as seed data
                    resp = await client.get(
                        f"{self.url}/api/v4/teams/{teams[0]['id']}"
                        "/channels/name/town-square",
                        headers=self._headers(),
                    )
                    if resp.status_code != 200:
                        continue
                    channel_id = resp.json()["id"]

                    resp = await client.post(
                        f"{self.url}/api/v4/posts",
                        json={
                            "channel_id": channel_id,
                            "message": message,
                        },
                        headers=self._headers(),
                    )
                    if resp.status_code in (200, 201):
                        post_id = resp.json()["id"]
                        handle.record_ids.append(post_id)
                except Exception as e:
                    logger.warning(f"Failed to seed Mattermost message: {e}")

        return handle

    async def cleanup(self, handle: CleanupHandle) -> None:
        await self._login()
        async with httpx.AsyncClient(timeout=30.0) as client:
            for post_id in handle.record_ids:
                try:
                    resp = await client.delete(
                        f"{self.url}/api/v4/posts/{post_id}",
                        headers=self._headers(),
                    )
                    resp.raise_for_status()
                except httpx.HTTPError as e:
                    logger.warning(f"Failed to delete post {post_id}: {e}")


class RocketChatHandler(ServiceHandler):
    """Seed RocketChat channel messages for Slack."""

    service_name = "rocketchat"

    def __init__(self, url: str, user: str, password: str):
        self.url = url
        self.user = user
        self.password = password
        self._auth: dict[str, str] = {}

    async def _login(self) -> dict[str, str]:
        if self._auth:
            return self._auth
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{self.url}/api/v1/login",
                json={"user": self.user, "password": self.password},
            )
            resp.raise_for_status()
            data = resp.json()["data"]
        self._auth = {
            "X-Auth-Token": data["authToken"],
            "X-User-Id": data["userId"],
        }
        return self._auth

    async def seed(self, records: list[dict]) -> CleanupHandle:
        handle = CleanupHandle(service="rocketchat")
        auth = await self._login()

        async with httpx.AsyncClient(timeout=30.0) as client:
            # Create users
            sender_ids = {r.get("sender_id", "") for r in records if r.get("sender_id")}
            for sender in sender_ids:
                if not sender or sender.startswith("group_"):
                    continue
                try:
                    await client.post(
                        f"{self.url}/api/v1/users.create",
                        json={
                            "email": f"{sender}@privacylens.local",
                            "name": sender,
                            "username": sender,
                            "password": "password123",
                        },
                        headers=auth,
                    )
                except Exception:
                    pass

            # Post messages
            for record in records:
                message = record.get("message", "")
                sender = record.get("sender_id", "")
                if not message:
                    continue
                try:
                    result = await client.post(
                        f"{self.url}/api/v1/chat.sendMessage",
                        json={
                            "message": {
                                "rid": "GENERAL",
                                "msg": message,
                                "alias": sender,
                            }
                        },
                        headers=auth,
                    )
                    msg_id = result.json().get("message", {}).get("_id", "")
                    if msg_id:
                        handle.record_ids.append(msg_id)
                except Exception as e:
                    logger.warning(f"Failed to seed RC message: {e}")

        return handle

    async def cleanup(self, handle: CleanupHandle) -> None:
        if not handle.record_ids:
            return
        auth = await self._login()
        async with httpx.AsyncClient(timeout=30.0) as client:
            for msg_id in handle.record_ids:
                try:
                    resp = await client.post(
                        f"{self.url}/api/v1/chat.delete",
                        json={"roomId": "GENERAL", "msgId": msg_id},
                        headers=auth,
                    )
                    resp.raise_for_status()
                except httpx.HTTPError as e:
                    logger.warning(f"Failed to delete RC message {msg_id}: {e}")


class MailpitHandler(ServiceHandler):
    """Seed Mailpit emails via SMTP."""

    service_name = "mailpit"

    def __init__(self, api_url: str, smtp_host: str, smtp_port: int):
        self.api_url = api_url
        self.smtp_host = smtp_host
        self.smtp_port = smtp_port

    async def seed(self, records: list[dict]) -> CleanupHandle:
        handle = CleanupHandle(service="mailpit")
        for record in records:
            subject = record.get("subject", "Seeded Email")
            body = record.get(
                "body",
                record.get("content", record.get("snippet", "")),
            )
            sender = record.get(
                "sender",
                record.get("from", "sender@privacylens.local"),
            )
            to = record.get("to", "john.doe@gmail.com")

            msg = MIMEText(body)
            msg["Subject"] = subject
            msg["From"] = sender
            msg["To"] = to if isinstance(to, str) else ", ".join(to)

            try:
                with smtplib.SMTP(self.smtp_host, self.smtp_port) as smtp:
                    smtp.sendmail(
                        sender,
                        [to] if isinstance(to, str) else to,
                        msg.as_string(),
                    )
                logger.info(f"Seeded email: {subject}")
                handle.record_ids.append(subject)
            except Exception as e:
                logger.warning(f"Failed to seed email '{subject}': {e}")

        return handle

    async def cleanup(self, handle: CleanupHandle) -> None:
        # Mailpit is a dedicated test inbox — wipe it wholesale.
        # SMTP gives us no per-message ID we could DELETE, so the
        # only honest cleanup is to DELETE /api/v1/messages, which
        # empties the inbox. Safe because nothing else writes here.
        if not handle.record_ids:
            return
        async with httpx.AsyncClient(timeout=30.0) as client:
            try:
                resp = await client.delete(f"{self.api_url}/api/v1/messages")
                resp.raise_for_status()
            except httpx.HTTPError as e:
                logger.warning(f"Failed to wipe Mailpit inbox: {e}")


class GoToSocialHandler(ServiceHandler):
    """Seed GoToSocial posts via the Mastodon-compatible API.

    Posts the privacylens user's own statuses for each post-shaped
    record. Profile records are skipped — GoToSocial does not let
    you provision arbitrary other-user accounts via the public API,
    so the agent will only ever see the privacylens account.
    """

    service_name = "gotosocial"

    def __init__(self, url: str, token: str = ""):
        self.url = url
        self.token = token

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    async def seed(self, records: list[dict]) -> CleanupHandle:
        handle = CleanupHandle(service="gotosocial")
        if not self.token:
            logger.warning(
                "GoToSocial token not configured; skipping %d records",
                len(records),
            )
            return handle

        async with httpx.AsyncClient(timeout=30.0) as client:
            for record in records:
                # Profile records can't be seeded — only the privacylens
                # user exists. Log once per record and move on.
                if record.get("type") == "profile":
                    logger.warning(
                        "GoToSocial profile records cannot be seeded; "
                        "agent will see only the privacylens user"
                    )
                    continue

                content = record.get("content", "")
                if not content:
                    continue
                try:
                    resp = await client.post(
                        f"{self.url}/api/v1/statuses",
                        data={"status": content},
                        headers=self._headers(),
                    )
                    if resp.status_code in (200, 201):
                        status_id = resp.json().get("id", "")
                        if status_id:
                            handle.record_ids.append(status_id)
                            logger.info(f"Seeded GoToSocial status id={status_id}")
                    else:
                        logger.warning(
                            f"Failed to seed GoToSocial status: "
                            f"{resp.status_code} {resp.text[:200]}"
                        )
                except Exception as e:
                    logger.warning(f"Failed to seed GoToSocial status: {e}")

        return handle

    async def cleanup(self, handle: CleanupHandle) -> None:
        if not self.token or not handle.record_ids:
            return
        async with httpx.AsyncClient(timeout=30.0) as client:
            for status_id in handle.record_ids:
                try:
                    resp = await client.delete(
                        f"{self.url}/api/v1/statuses/{status_id}",
                        headers=self._headers(),
                    )
                    resp.raise_for_status()
                except httpx.HTTPError as e:
                    logger.warning(
                        f"Failed to delete GoToSocial status {status_id}: {e}"
                    )


class RadicaleHandler(ServiceHandler):
    """Seed Radicale calendar events via CalDAV."""

    service_name = "radicale"

    def __init__(self, url: str, user: str, password: str):
        self.url = url
        self.user = user
        self.password = password

    async def seed(self, records: list[dict]) -> CleanupHandle:
        import caldav

        handle = CleanupHandle(service="radicale")
        client = caldav.DAVClient(
            url=self.url,
            username=self.user,
            password=self.password,
        )
        try:
            principal = client.principal()
            calendars = principal.calendars()
            calendar = (
                calendars[0] if calendars else principal.make_calendar(name="default")
            )
        except Exception as e:
            logger.warning(f"Failed to connect to Radicale: {e}")
            return handle

        for record in records:
            event_id = record.get("event_id", record.get("event_name", ""))
            if isinstance(event_id, str) and not record.get("event_name"):
                continue

            name = record.get("event_name", "Event")
            content = record.get("content", "")
            start = record.get("start_time", "2022-02-25T10:00:00")
            end = record.get("end_time", "2022-02-25T11:00:00")
            location = record.get("location", "")
            attendees = record.get("attendees", [])

            vcal = (
                "BEGIN:VCALENDAR\nVERSION:2.0\nBEGIN:VEVENT\n"
                f"UID:{event_id}\n"
                f"SUMMARY:{name}\n"
                f"DESCRIPTION:{content}\n"
                f"DTSTART:{_ical_dt(start)}\n"
                f"DTEND:{_ical_dt(end)}\n"
                f"LOCATION:{location}\n"
            )
            for attendee in attendees:
                vcal += f"ATTENDEE:mailto:{attendee}\n"
            vcal += "END:VEVENT\nEND:VCALENDAR"

            try:
                calendar.save_event(vcal)
                handle.record_ids.append(event_id)
                logger.info(f"Seeded calendar event: {name}")
            except Exception as e:
                logger.warning(f"Failed to seed event '{name}': {e}")

        return handle

    async def cleanup(self, handle: CleanupHandle) -> None:
        if not handle.record_ids:
            return
        import caldav

        try:
            client = caldav.DAVClient(
                url=self.url,
                username=self.user,
                password=self.password,
            )
            principal = client.principal()
            calendars = principal.calendars()
            if not calendars:
                return
            calendar = calendars[0]
        except Exception as e:
            logger.warning(f"Failed to connect to Radicale for cleanup: {e}")
            return

        for event_uid in handle.record_ids:
            try:
                event = calendar.event_by_uid(event_uid)
                event.delete()
            except Exception as e:
                logger.warning(f"Failed to delete calendar event {event_uid}: {e}")


class Seeder:
    """Orchestrates seeding from a task's seed_data/ directory."""

    def __init__(
        self,
        bookstack_url: str = "http://localhost:3000",
        bookstack_token_id: str = "",
        bookstack_token_secret: str = "",
        mattermost_url: str = "http://localhost:8065",
        mattermost_user: str = "admin",
        mattermost_password: str = "admin",
        rocketchat_url: str = "http://localhost:3100",
        rocketchat_user: str = "admin",
        rocketchat_password: str = "admin",
        mailpit_api_url: str = "http://localhost:8025",
        mailpit_smtp_host: str = "localhost",
        mailpit_smtp_port: int = 1025,
        gotosocial_url: str = "http://localhost:4000",
        gotosocial_token: str = "",
        radicale_url: str = "http://localhost:5232",
        radicale_user: str = "admin",
        radicale_password: str = "admin",
    ):
        self.handlers: dict[str, ServiceHandler] = {
            "bookstack": BookStackHandler(
                bookstack_url,
                bookstack_token_id,
                bookstack_token_secret,
            ),
            "mattermost": MattermostHandler(
                mattermost_url,
                mattermost_user,
                mattermost_password,
            ),
            "rocketchat": RocketChatHandler(
                rocketchat_url,
                rocketchat_user,
                rocketchat_password,
            ),
            "mailpit": MailpitHandler(
                mailpit_api_url,
                mailpit_smtp_host,
                mailpit_smtp_port,
            ),
            "gotosocial": GoToSocialHandler(
                gotosocial_url,
                gotosocial_token,
            ),
            "radicale": RadicaleHandler(
                radicale_url,
                radicale_user,
                radicale_password,
            ),
        }

    async def seed_from_dir(self, seed_dir: Path) -> SeedResult:
        """Read seed data files and inject into real services."""
        result = SeedResult()
        for seed_file in sorted(seed_dir.glob("*.json")):
            service = seed_file.stem
            handler = self.handlers.get(service)
            if handler is None:
                logger.warning(f"No handler for service: {service}")
                continue
            records = json.loads(seed_file.read_text())
            handle = await handler.seed(records)
            result.handles.append(handle)
        return result

    async def cleanup(self, result: SeedResult) -> None:
        """Remove all seeded data."""
        for handle in result.handles:
            handler = self.handlers.get(handle.service)
            if handler:
                await handler.cleanup(handle)
