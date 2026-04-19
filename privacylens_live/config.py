"""Central configuration for PrivacyLens-Live.

Single source of truth for service credentials, URLs, and the LLM endpoint.

Token management
----------------
All service credentials are defined here as Pydantic field defaults. These
defaults are written to ``privacylens_live/.env`` by :meth:`Config.write_env_file`
so that ``docker compose`` and the host-side seeder/runner share one
authoritative copy. ``Config.from_env`` reads ``.env`` (if present) plus the
process environment, so both sources of truth converge on the same values.

Tokens that must be provisioned at runtime against a fresh service instance
(currently ``gotosocial_token``) start as empty strings and are filled by
:mod:`privacylens_live.bootstrap` after the first ``docker compose up``. The
bootstrap step then re-writes ``.env`` so subsequent runs are zero-touch.

The only true secret here is ``llm_api_key``, which always comes from the
process environment (never from ``.env`` or class defaults) so it doesn't
land in a tracked file by accident.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


logger = logging.getLogger("config")


class Config(BaseModel):
    """Configuration for PrivacyLens-Live platform."""

    # ── BookStack (wiki / knowledge base) ──
    bookstack_url: str = "http://localhost:3000"
    # The token below is bound to the bookstack-db-data Docker volume that
    # was provisioned via `php artisan tinker` on first boot. It stays valid
    # for the lifetime of that volume; if you `docker compose down -v`, run
    # `python -m privacylens_live setup` to re-provision.
    bookstack_token_id: str = "07372a4446cc7cbe48dbb927d5404a4b"
    bookstack_token_secret: str = (
        "8ef1d909611834a51ac06ee88c07153409bea9a03a2af283fb97f0b21e04006b"
    )

    # ── Mattermost (direct messaging) ──
    mattermost_url: str = "http://localhost:8065"
    mattermost_user: str = "admin"
    mattermost_password: str = "Admin123!"
    mattermost_email: str = "admin@privacylens.local"
    mattermost_team: str = "privacylens"

    # ── RocketChat (team chat / channels) ──
    rocketchat_url: str = "http://localhost:3100"
    rocketchat_user: str = "admin"
    rocketchat_password: str = "admin"

    # ── Mailpit (email) ──
    mailpit_api_url: str = "http://localhost:8025"
    mailpit_smtp_host: str = "localhost"
    mailpit_smtp_port: int = 1025

    # ── GoToSocial (social media) ──
    gotosocial_url: str = "http://localhost:4000"
    gotosocial_user: str = "privacylens"
    # GoToSocial enforces a 100% strength check on passwords; the
    # earlier ``Admin123!`` was only 80% and got rejected by the
    # admin CLI on first-time provisioning.
    gotosocial_password: str = "PrivacyLens-Admin-2026!@#"
    gotosocial_email: str = "privacylens@privacylens.local"
    # Empty by default — populated by bootstrap_gotosocial against a fresh
    # gotosocial-data volume, then persisted to .env.
    gotosocial_token: str = ""

    # ── Radicale (calendar) ──
    radicale_url: str = "http://localhost:5232"
    radicale_user: str = "admin"
    radicale_password: str = "admin"

    # ── MCP server URLs (Docker-internal, for agent container) ──
    mcp_server_urls: dict[str, str] = Field(
        default_factory=lambda: {
            "bookstack": "http://bookstack-mcp:8080/mcp",
            "mattermost": "http://mattermost-mcp:8080/mcp",
            "rocketchat": "http://rocketchat-mcp:8080/mcp",
            "mailpit": "http://mailpit-mcp:8080/mcp",
            "gotosocial": "http://gotosocial-mcp:8080/mcp",
            "radicale": "http://radicale-mcp:8080/mcp",
        }
    )

    # ── Agent LLM config — Azure OpenAI GPT 5.2 ──
    llm_model: str = "azure/gpt-5.2"
    llm_api_key: str = ""
    llm_base_url: str = "https://openai-lava.openai.azure.com/"
    llm_api_version: str = "2025-03-01-preview"
    max_iterations: int = 20

    # ── Extraction LLM — DeepSeek-V3.2 for privacy flow extraction ──
    extraction_llm_model: str = "openai/DeepSeek-V3.2"
    extraction_llm_api_key: str = ""
    extraction_llm_base_url: str = "https://foundry-lava.openai.azure.com/openai/v1/"

    # ── Evaluation LLM (same model by default) ──
    eval_model: str = "azure/gpt-5.2"

    # ── Paths ──
    data_path: Path = Path("main_data.json")
    tasks_dir: Path = Path("privacylens_live/tasks")
    results_dir: Path = Path("results")

    # ── Docker ──
    docker_network: str = "privacylens_live_privacylens-net"
    agent_server_image: str = "privacylens-agent-server:local"

    @classmethod
    def from_env(cls, env_file: Path | None = None) -> Config:
        """Create config from environment variables, layered over defaults.

        Resolution order, lowest to highest precedence:

        1. Pydantic field defaults (this class).
        2. The ``privacylens_live/.env`` file (if it exists), parsed and
           merged into ``os.environ``. This is where bootstrap-provisioned
           tokens and any user overrides live.
        3. The process environment (``os.environ``), so an explicit
           ``BOOKSTACK_TOKEN_ID=foo python -m privacylens_live run`` still
           wins.

        ``llm_api_key`` is always read from the environment and never falls
        back to a default — it's a real secret, not a local-dev token.
        """
        if env_file is None:
            env_file = ENV_FILE_PATH
        if env_file.exists():
            load_env_file(env_file)

        # Build kwargs only for env-overridable fields. Anything not present
        # in the environment falls through to the class default. Typed as
        # Any because Pydantic field types are heterogeneous (str, int) and
        # pyright can't track them through a dict construction.
        overrides: dict[str, Any] = {}

        def _str(field: str, env: str) -> None:
            val = os.environ.get(env)
            if val is not None and val != "":
                overrides[field] = val

        _str("bookstack_url", "BOOKSTACK_URL")
        _str("bookstack_token_id", "BOOKSTACK_TOKEN_ID")
        _str("bookstack_token_secret", "BOOKSTACK_TOKEN_SECRET")
        _str("mattermost_url", "MATTERMOST_URL")
        _str("mattermost_user", "MATTERMOST_USER")
        _str("mattermost_password", "MATTERMOST_PASSWORD")
        _str("mattermost_email", "MATTERMOST_EMAIL")
        _str("mattermost_team", "MATTERMOST_TEAM")
        _str("rocketchat_url", "ROCKETCHAT_URL")
        _str("rocketchat_user", "ROCKETCHAT_USER")
        _str("rocketchat_password", "ROCKETCHAT_PASSWORD")
        _str("mailpit_api_url", "MAILPIT_API_URL")
        _str("mailpit_smtp_host", "MAILPIT_SMTP_HOST")
        port = os.environ.get("MAILPIT_SMTP_PORT")
        if port:
            overrides["mailpit_smtp_port"] = int(port)
        _str("gotosocial_url", "GOTOSOCIAL_URL")
        _str("gotosocial_user", "GOTOSOCIAL_USER")
        _str("gotosocial_password", "GOTOSOCIAL_PASSWORD")
        _str("gotosocial_email", "GOTOSOCIAL_EMAIL")
        _str("gotosocial_token", "GOTOSOCIAL_TOKEN")
        _str("radicale_url", "RADICALE_URL")
        _str("radicale_user", "RADICALE_USER")
        _str("radicale_password", "RADICALE_PASSWORD")
        _str("docker_network", "DOCKER_NETWORK")

        # LLM secret + endpoint always come from the process environment.
        overrides["llm_api_key"] = os.environ.get("LLM_API_KEY", "")
        llm_base = os.environ.get("LLM_BASE_URL")
        if llm_base:
            overrides["llm_base_url"] = llm_base
        llm_model = os.environ.get("LLM_MODEL")
        if llm_model:
            # Accept either bare model name (e.g. ``gpt-5.2``) or the
            # litellm-style ``azure/gpt-5.2`` form. The downstream LLM
            # client wants the prefixed form when talking to Azure.
            if "/" not in llm_model:
                llm_model = f"azure/{llm_model}"
            overrides["llm_model"] = llm_model
        llm_api_version = os.environ.get("LLM_API_VERSION")
        if llm_api_version:
            overrides["llm_api_version"] = llm_api_version

        # Extraction LLM (privacy analyzer) — separate credentials
        extraction_key = os.environ.get("EXTRACTION_LLM_API_KEY", "")
        if extraction_key:
            overrides["extraction_llm_api_key"] = extraction_key
        extraction_base = os.environ.get("EXTRACTION_LLM_BASE_URL")
        if extraction_base:
            overrides["extraction_llm_base_url"] = extraction_base
        extraction_model = os.environ.get("EXTRACTION_LLM_MODEL")
        if extraction_model:
            overrides["extraction_llm_model"] = extraction_model

        return cls(**overrides)

    def write_env_file(self, path: Path | None = None) -> Path:
        """Write the docker-compose ``.env`` file from this config.

        The .env file is the bridge between Python-side state (this class)
        and docker-compose-side state (the MCP server containers' env vars).
        Anything that needs to flow from one to the other goes through here.

        Excludes ``llm_api_key`` deliberately — that's a real secret, not a
        local-dev token, and we don't want it landing in a file on disk
        even though .env is gitignored.
        """
        if path is None:
            path = ENV_FILE_PATH
        lines = [
            "# Generated by Config.write_env_file — do not edit by hand.",
            "# Source of truth is privacylens_live/config.py.",
            "# Run `python -m privacylens_live setup` to regenerate.",
            "#",
            "# The *_URL values here are HOST-side (localhost:port) and used by",
            "# the Python seeder + runner. The MCP server containers use",
            "# Docker-internal URLs hardcoded in docker-compose.yml — those are",
            "# NOT pulled from this file.",
            "",
            f"BOOKSTACK_URL={self.bookstack_url}",
            f"BOOKSTACK_TOKEN_ID={self.bookstack_token_id}",
            f"BOOKSTACK_TOKEN_SECRET={self.bookstack_token_secret}",
            "",
            f"MATTERMOST_URL={self.mattermost_url}",
            f"MATTERMOST_USER={self.mattermost_user}",
            f"MATTERMOST_PASSWORD={self.mattermost_password}",
            f"MATTERMOST_EMAIL={self.mattermost_email}",
            f"MATTERMOST_TEAM={self.mattermost_team}",
            "",
            f"ROCKETCHAT_URL={self.rocketchat_url}",
            f"ROCKETCHAT_USER={self.rocketchat_user}",
            f"ROCKETCHAT_PASSWORD={self.rocketchat_password}",
            "",
            f"MAILPIT_API_URL={self.mailpit_api_url}",
            f"MAILPIT_SMTP_HOST={self.mailpit_smtp_host}",
            f"MAILPIT_SMTP_PORT={self.mailpit_smtp_port}",
            "",
            f"GOTOSOCIAL_URL={self.gotosocial_url}",
            f"GOTOSOCIAL_USER={self.gotosocial_user}",
            f"GOTOSOCIAL_PASSWORD={self.gotosocial_password}",
            f"GOTOSOCIAL_EMAIL={self.gotosocial_email}",
            f"GOTOSOCIAL_TOKEN={self.gotosocial_token}",
            "",
            f"RADICALE_URL={self.radicale_url}",
            f"RADICALE_USER={self.radicale_user}",
            f"RADICALE_PASSWORD={self.radicale_password}",
            "",
        ]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines))
        logger.info("Wrote %s", path)
        return path


# ── Module-level helpers ──

ENV_FILE_PATH = Path(__file__).parent / ".env"


def load_env_file(path: Path) -> dict[str, str]:
    """Parse a ``.env`` file and merge it into ``os.environ``.

    Tiny dependency-free parser: ``KEY=VALUE`` per line, ``#`` starts a
    comment, blank lines ignored, no escaping / interpolation. Surrounding
    single or double quotes on the value are stripped (matching the
    convention used by python-dotenv and docker-compose) so lines like
    ``LLM_API_KEY = "abc123"`` parse to ``LLM_API_KEY=abc123``.
    Existing entries in ``os.environ`` win, so the process environment
    overrides ``.env`` (matching docker-compose's own behavior).
    """
    parsed: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        # Strip a single layer of matching surrounding quotes.
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        parsed[key] = value
        if key not in os.environ:
            os.environ[key] = value
    return parsed


# ── PrivacyLens action name → service mapping ──
# Used ONLY by task generator to route seed data to service-specific files.
# Maps original PrivacyLens action names to seed data file stems.
ACTION_TO_SERVICE: dict[str, str] = {
    "NotionManagerSearchContent": "bookstack",
    "NotionManagerReadPage": "bookstack",
    "NotionManagerCreatePage": "bookstack",
    "NotionManagerEditPage": "bookstack",
    "NotionManagerGetAllContent": "bookstack",
    "NotionManagerSharePage": "bookstack",
    "MessengerReceiveMessage": "mattermost",
    "MessengerSearchInChat": "mattermost",
    "MessengerSendMessage": "mattermost",
    "MessengerSend": "mattermost",
    "GmailSendEmail": "mailpit",
    "GmailSearchEmails": "mailpit",
    "GmailReadEmail": "mailpit",
    "GmailSearchContacts": "mailpit",
    "SlackSendMessage": "rocketchat",
    "SlackSearchMessage": "rocketchat",
    "SlackSearchChannelByUser": "rocketchat",
    "SlackSearchChannelOrUser": "rocketchat",
    "SlackGetUserDetails": "rocketchat",
    "FacebookManagerCreatePost": "gotosocial",
    "FacebookManagerGetUserProfile": "gotosocial",
    "FacebookManagerGetUserPosts": "gotosocial",
    "FacebookManagerSearchFriends": "gotosocial",
    "FacebookManagerSearchPosts": "gotosocial",
    "FacebookManagerGetUserFriends": "gotosocial",
    "FacebookManagerUpdateStatus": "gotosocial",
    "GoogleCalendarSearchEvents": "radicale",
    "GoogleCalendarReadEvents": "radicale",
    "ZoomManagerGetMeetingTranscript": "bookstack",
    "ZoomManagerSearchMeetings": "bookstack",
    "ZoomManagerSearchTranscript": "bookstack",
    "ZoomManagerGetParticipants": "bookstack",
    "ZoomManagerSearchMeetingTranscript": "bookstack",
    "ZoomManagerGetMeetingTranscripts": "bookstack",
}
