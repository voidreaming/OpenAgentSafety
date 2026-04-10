"""Mailpit MCP server — email tools.

Provides email send (SMTP), search, read, and contact lookup via Mailpit API.

Error handling
--------------
HTTP-side tools (search/read/list_contacts) go through the shared
``base.http_*`` helpers, which translate every httpx failure into a
``HTTPToolError`` carrying the upstream status code. There is no auth on
Mailpit's REST API, so no login flow or 401 retry is needed.

The :func:`send_email` tool uses ``smtplib`` rather than HTTP, so its error
translation is inline and Mailpit-specific: SMTP-level failures (connection
refused, server rejection, transient errors) are converted into ``ToolError``
with the SMTP response code and message attached so the agent sees actionable
context instead of an opaque traceback.

Business-logic failures (e.g. an unknown ``email_id``) raise :class:`ToolError`
directly with an actionable recovery hint that names the tool the agent can
use to discover valid inputs.
"""

from __future__ import annotations

import smtplib
from email.mime.text import MIMEText
from typing import Any

from base import (
    HTTPToolError,
    get_env,
    http_get_params,
    logger,
)
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError


MAILPIT_API = get_env("MAILPIT_API_URL", "http://mailpit:8025")
SMTP_HOST = get_env("MAILPIT_SMTP_HOST", "mailpit")
SMTP_PORT = int(get_env("MAILPIT_SMTP_PORT", "1025"))

# Hardcoded persona for the seed environment. Tasks rewrite recipients but
# always send "from" the test user.
_FROM_ADDRESS = "john.doe@gmail.com"

mcp = FastMCP("mailpit")


def _format_address(field: Any) -> str:
    """Mailpit returns addresses as either ``{Address, Name}`` dicts or strings."""
    if isinstance(field, dict):
        return field.get("Address", "")
    return str(field) if field else ""


# ── Tools ──


@mcp.tool()
async def send_email(
    to: str,
    subject: str,
    body: str,
    cc: str = "",
    bcc: str = "",
) -> dict:
    """Send an email to the specified recipient."""
    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = _FROM_ADDRESS
    msg["To"] = to
    if cc:
        msg["Cc"] = cc

    recipients = [to]
    if cc:
        recipients.extend(cc.split(","))
    if bcc:
        recipients.extend(bcc.split(","))

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as smtp:
            smtp.sendmail(_FROM_ADDRESS, recipients, msg.as_string())
    except smtplib.SMTPResponseException as exc:
        # SMTPResponseException covers most rejection paths and carries
        # both the SMTP code and the server's textual reason.
        raw = exc.smtp_error
        detail = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
        msg_text = (
            f"SMTP send to {to} rejected by {SMTP_HOST}:{SMTP_PORT} "
            f"(code {exc.smtp_code}): {detail}"
        )
        logger.warning(msg_text)
        raise ToolError(msg_text) from exc
    except smtplib.SMTPException as exc:
        # Generic SMTP-protocol errors (no response code attached).
        msg_text = (
            f"SMTP send to {to} failed against {SMTP_HOST}:{SMTP_PORT}: "
            f"{type(exc).__name__}: {exc}"
        )
        logger.warning(msg_text)
        raise ToolError(msg_text) from exc
    except OSError as exc:
        # Connection refused / DNS / socket errors before SMTP handshake.
        msg_text = (
            f"Cannot reach SMTP server {SMTP_HOST}:{SMTP_PORT}: "
            f"{type(exc).__name__}: {exc}"
        )
        logger.warning(msg_text)
        raise ToolError(msg_text) from exc

    return {"success": True, "to": to, "subject": subject}


@mcp.tool()
async def search_emails(query: str) -> dict:
    """Search emails by keyword in subject or body.

    Use the email_id field to read a specific email.
    """
    data = await http_get_params(
        f"{MAILPIT_API}/api/v1/search",
        params={"query": query, "limit": 20},
    )
    emails = [
        {
            "email_id": msg.get("ID", ""),
            "subject": msg.get("Subject", ""),
            "sender": _format_address(msg.get("From")),
            "time": msg.get("Created", ""),
            "snippet": msg.get("Snippet", ""),
        }
        for msg in data.get("messages", [])
    ]
    return {"emails": emails}


@mcp.tool()
async def read_email(email_id: str) -> dict:
    """Read a specific email.

    Pass the email_id from search_emails results.
    """
    try:
        msg = await http_get_params(
            f"{MAILPIT_API}/api/v1/message/{email_id}",
        )
    except HTTPToolError as exc:
        if exc.status_code == 404:
            raise ToolError(
                f"Email '{email_id}' not found in Mailpit. "
                "Use search_emails to discover valid email IDs."
            ) from exc
        raise
    return {
        "email_id": msg.get("ID", ""),
        "subject": msg.get("Subject", ""),
        "sender": _format_address(msg.get("From")),
        "to": [_format_address(r) for r in msg.get("To", [])],
        "time": msg.get("Created", ""),
        "body": msg.get("Text", ""),
    }


@mcp.tool()
async def list_contacts(name: str) -> dict:
    """Search for contacts by name. Derives contacts from email headers."""
    data = await http_get_params(
        f"{MAILPIT_API}/api/v1/search",
        params={"query": name, "limit": 50},
    )
    contacts: dict[str, dict[str, str]] = {}
    needle = name.lower()
    for msg in data.get("messages", []):
        # "From" field
        from_addr = msg.get("From")
        if isinstance(from_addr, dict):
            email = from_addr.get("Address", "")
            contact_name = from_addr.get("Name", "")
            if email and needle in (email + contact_name).lower():
                contacts[email] = {"name": contact_name, "email": email}
        # "To" field (list of dicts)
        for to in msg.get("To", []):
            if isinstance(to, dict):
                email = to.get("Address", "")
                contact_name = to.get("Name", "")
                if email and needle in (email + contact_name).lower():
                    contacts[email] = {
                        "name": contact_name,
                        "email": email,
                    }
    return {"contacts": list(contacts.values())}


if __name__ == "__main__":
    logger.info(f"Starting Mailpit MCP server (API at {MAILPIT_API})")
    mcp.run(transport="streamable-http", host="0.0.0.0", port=8080)
