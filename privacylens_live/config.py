"""Central configuration for PrivacyLens-Live."""

from __future__ import annotations

import os
from pathlib import Path

from pydantic import BaseModel, Field


class Config(BaseModel):
    """Configuration for PrivacyLens-Live platform."""

    # ── Service URLs (host-side, for seeder) ──
    bookstack_url: str = "http://localhost:3000"
    bookstack_token_id: str = ""
    bookstack_token_secret: str = ""

    mattermost_url: str = "http://localhost:8065"
    mattermost_user: str = "admin"
    mattermost_password: str = "Admin123!"

    rocketchat_url: str = "http://localhost:3100"
    rocketchat_user: str = "admin"
    rocketchat_password: str = "admin"

    mailpit_api_url: str = "http://localhost:8025"
    mailpit_smtp_host: str = "localhost"
    mailpit_smtp_port: int = 1025

    gotosocial_url: str = "http://localhost:4000"
    gotosocial_token: str = ""

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

    # ── Evaluation LLM (same model by default) ──
    eval_model: str = "azure/gpt-5.2"

    # ── Paths ──
    data_path: Path = Path("main_data.json")
    tasks_dir: Path = Path("privacylens_live/tasks")
    results_dir: Path = Path("results")

    # ── Docker ──
    docker_network: str = "privacylens_live_privacylens-net"
    agent_server_image: str = "ghcr.io/openhands/agent-server:latest-python"

    @classmethod
    def from_env(cls) -> Config:
        """Create config from environment variables."""
        return cls(
            llm_api_key=os.getenv("LLM_API_KEY", ""),
            llm_base_url=os.getenv(
                "LLM_BASE_URL",
                "https://openai-lava.openai.azure.com/",
            ),
            llm_model=os.getenv("LLM_MODEL", "azure/gpt-5.2"),
            bookstack_token_id=os.getenv("BOOKSTACK_TOKEN_ID", ""),
            bookstack_token_secret=os.getenv("BOOKSTACK_TOKEN_SECRET", ""),
            gotosocial_token=os.getenv("GOTOSOCIAL_TOKEN", ""),
        )


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
