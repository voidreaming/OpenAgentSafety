# PrivacyLens-Live

Live evaluation platform for LLM privacy norm awareness. Converts the 493-entry PrivacyLens benchmark from static emulated trajectories into real service interactions using OpenHands SDK v1.

## Architecture

```
Docker Network: privacylens_live_privacylens-net

  Infrastructure Services
    BookStack (:3000) + MariaDB        — wiki / knowledge base
    Mattermost (:8065) + PostgreSQL    — direct messaging
    RocketChat (:3100) + MongoDB       — team chat / channels
    Mailpit (:8025/:1025)              — email (REST + SMTP)
    GoToSocial (:4000)                 — social media (Mastodon API)
    Radicale (:5232)                   — calendar (CalDAV)

  MCP Servers (FastMCP HTTP, one per service)
    bookstack-mcp (:9001)     mattermost-mcp (:9002)
    rocketchat-mcp (:9003)    mailpit-mcp (:9004)
    gotosocial-mcp (:9005)    radicale-mcp (:9006)

  Agent Container (OpenHands DockerWorkspace on same network)
```

Host orchestrator: seeder injects data via REST, agent runner creates DockerWorkspace, event collector extracts final action, evaluator checks for privacy leakage.

## MCP Server Design Principles

MCP servers are **proper service wrappers**, not mocks of commercial tools. The agent interacts with BookStack, Mattermost, RocketChat, etc. as what they are.

### Naming conventions

- **snake_case**, verb-first: `search_pages`, `send_message`, `create_post`
- No service prefix in tool names — the SDK auto-prefixes with server name when multiple servers are configured (e.g., `bookstack_search_pages`, `mattermost_send_message`)
- Never name tools after commercial products (no `NotionManagerXxx`, `FacebookManagerXxx`, `GmailSendEmail`)
- Concise descriptions in imperative style
- **Annotate every parameter** with `Annotated[T, Field(description=...)]`. FastMCP propagates the description into the tool's `inputSchema.properties.<param>.description`, which is what the LLM actually reads. Bare `str`/`int` parameters give the model nothing to disambiguate similar fields with.

### Input/output field name consistency

**Critical**: Output field names in tool results MUST match input parameter names of related tools. LLMs copy field names from previous observations when calling subsequent tools — if the names don't match, the LLM sends wrong parameter names.

| Pattern | Example |
|---|---|
| `list_pages` returns `page_id` → `get_page` takes `page_id` | `list_pages` → `{"page_id": 6}` → `get_page(page_id=6)` |
| `search_emails` returns `email_id` → `read_email` takes `email_id` | `search_emails` → `{"email_id": "abc"}` → `read_email(email_id="abc")` |
| `search_users` returns `user_id` → `get_profile` takes `user_id` | `search_users` → `{"user_id": "123"}` → `get_profile(user_id="123")` |
| `list_events` returns `event_id` → `get_event` takes `event_id` | `list_events` → `{"event_id": "xyz"}` → `get_event(event_id="xyz")` |
| `list_users` returns `username` → `send_message` takes `recipient` | Description says "Pass the recipient username from list_users" |
| `mattermost: search_messages / list_messages` returns `sender` (resolved username) → `send_message` takes `recipient` | The sender field is resolved server-side from Mattermost's user_id via `/api/v4/users/ids` so the chain composes directly. |
| `gotosocial: search_posts` returns `user_id` → `get_profile` / `list_user_posts` take `user_id` | `search_posts` includes both `author` (username) and `user_id` (account ID); `user_id` is the composability handle. |
| `mailpit: search_emails / read_email` returns `from_email` → `send_email` takes `to` | Same shape as `list_contacts.email`; renamed from `sender` so the field name reflects the value. |

Tool descriptions should also reference the field: "Pass the page_id from list_pages or search_pages results."

### Server structure

Each server is a FastMCP app in `mcp_servers/`, backed by a real service's REST/CalDAV API:

```python
from fastmcp import FastMCP
mcp = FastMCP("service_name")  # e.g., "bookstack", "mattermost"

@mcp.tool()
async def verb_noun(param: str) -> dict:
    """Imperative description of what this does."""
    ...

if __name__ == "__main__":
    mcp.run(transport="streamable-http", host="0.0.0.0", port=8080)
```

Shared HTTP utilities live in `mcp_servers/base.py`: `http_get`, `http_get_params`, `http_post`, `http_put`, `http_delete`.

## MCP Server Reference

### bookstack (wiki / knowledge base)

Server: `bookstack_server.py` | Port: 9001 | Auth: `Token {id}:{secret}`

| Tool | Params | Description |
|---|---|---|
| `search_pages(query)` | keyword string | Search pages by keyword |
| `get_page(page_id)` | int | Read a page by ID |
| `create_page(name, markdown, tags?)` | title + content | Create a new page |
| `update_page(page_id, markdown)` | int + content | Update page content |
| `list_pages()` | — | List all pages |
| `delete_page(page_id)` | int | Delete a page |

All pages belong to a single "Workspace" book (created lazily). Tags store metadata like `privacylens_id` for seed data traceability.

### mattermost (direct messaging)

Server: `mattermost_server.py` | Port: 9002 | Auth: Bearer token via login

| Tool | Params | Description |
|---|---|---|
| `send_message(recipient, message)` | username + text | Send a DM |
| `list_messages(max_count?)` | limit (default 20) | List recent DMs |
| `search_messages(query)` | keyword | Search message history |
| `list_users()` | — | List all users |

Both `list_messages` and `search_messages` resolve `sender` to the username server-side via `/api/v4/users/ids`, so the chain `search_messages → send_message(recipient=msg["sender"])` composes directly.

### rocketchat (team chat / channels)

Server: `rocketchat_server.py` | Port: 9003 | Auth: `X-Auth-Token` + `X-User-Id`

| Tool | Params | Description |
|---|---|---|
| `send_channel_message(channel, message)` | channel name + text | Send to channel or user |
| `search_messages(query)` | keyword | Search across channels |
| `list_channels()` | — | List all channels |
| `get_channel_history(channel, count?)` | channel + limit | Get recent channel messages |
| `get_user_info(username)` | username | Get user profile |

### mailpit (email)

Server: `mailpit_server.py` | Port: 9004 | Auth: none

| Tool | Params | Description |
|---|---|---|
| `send_email(to, subject, body, cc?, bcc?)` | email fields | Send email via SMTP |
| `search_emails(query)` | keyword | Search inbox |
| `read_email(email_id)` | message ID | Read full email |
| `list_contacts(name)` | name query | Find contacts from email headers |

`search_emails` and `read_email` return the sender as `from_email` (an email address) so it composes with `send_email`'s `to` parameter and matches `list_contacts`'s `email` field. `cc` and `bcc` on `send_email` accept comma-separated strings (e.g. `'a@x.com,b@x.com'`).

### gotosocial (social media)

Server: `gotosocial_server.py` | Port: 9005 | Auth: Bearer token

| Tool | Params | Description |
|---|---|---|
| `create_post(content)` | post text | Create a new post |
| `get_profile(user_id)` | account ID | Get user profile |
| `list_user_posts(user_id)` | account ID | Get user's recent posts |
| `search_users(query)` | name/username | Search for users |
| `search_posts(query)` | keyword | Search posts |

Uses Mastodon-compatible API (works with GoToSocial, Pleroma, Akkoma). `search_posts` returns both `author` (username, human-readable) and `user_id` (account ID); pass `user_id` to `get_profile` or `list_user_posts` to follow the chain. `content` on post-bearing results is HTML, not plain text (Mastodon convention).

### radicale (calendar)

Server: `radicale_server.py` | Port: 9006 | Auth: HTTP Basic

| Tool | Params | Description |
|---|---|---|
| `search_events(query)` | keyword | Search events (with client-side fallback) |
| `get_event(event_id)` | event UID | Get event details |
| `list_events()` | — | List all events |

## Project Structure

```
privacylens_live/
├── config.py                    # Service URLs, LLM config, ACTION_TO_SERVICE mapping
├── docker-compose.yml           # All infrastructure + MCP server containers
├── cli.py + __main__.py         # CLI: setup/generate/run/evaluate/teardown
├── base/
│   ├── trajectory_parser.py     # Parse PrivacyLens executable_trajectory
│   ├── seeder.py                # Per-service seed handlers (BookStack, Mattermost, etc.)
│   └── evaluator.py             # Rule-based + LLM-judge evaluation
├── tasks/
│   ├── generate.py              # main_data.json → 493 task dirs
│   └── main1..main493/          # task.json + seed_data/*.json
├── mcp_servers/
│   ├── base.py                  # Shared HTTP utilities + HTTPToolError
│   ├── bookstack_server.py      # 6 tools
│   ├── mattermost_server.py     # 4 tools
│   ├── rocketchat_server.py     # 5 tools
│   ├── mailpit_server.py        # 4 tools
│   ├── gotosocial_server.py     # 5 tools
│   ├── radicale_server.py       # 3 tools
│   ├── Dockerfile               # Python 3.13 + fastmcp + httpx + caldav
│   └── requirements.txt
└── runner/
    ├── agent_runner.py           # Seed → DockerWorkspace → collect
    ├── mcp_config_builder.py     # task dependencies → mcp_config dict
    └── event_collector.py        # Extract final action from events
```

## Config

Key fields in `Config` (Pydantic model, `config.py`):

- Service URLs are host-side (for seeder): `bookstack_url`, `mattermost_url`, `rocketchat_url`, etc.
- MCP URLs are Docker-internal (for agent): `mcp_server_urls` dict with keys `bookstack`, `mattermost`, etc.
- LLM: `llm_model="azure/gpt-5.2"`, `llm_base_url`, `llm_api_version="2025-03-01-preview"`
- `ACTION_TO_SERVICE` maps PrivacyLens action names to service names (used only by task generator)
- No `TOOLKIT_TO_MCP` — task `dependencies` directly list MCP server names

## Running

```bash
# Start services
docker compose -f privacylens_live/docker-compose.yml up -d

# Generate tasks
uv run python -m privacylens_live generate --data main_data.json

# Run tasks
export LLM_API_KEY="<azure-key>"
export BOOKSTACK_TOKEN_ID="<id>"
export BOOKSTACK_TOKEN_SECRET="<secret>"
uv run python -m privacylens_live run --names main1 --results-dir results/test

# Evaluate
uv run python -m privacylens_live evaluate --results-dir results/test

# Stop
docker compose -f privacylens_live/docker-compose.yml down
```

## Service Credentials

| Service | User | Password | Notes |
|---|---|---|---|
| BookStack | admin@admin.com | password | Auto-created on first boot |
| Mattermost | admin | admin | Create via web UI on first boot |
| RocketChat | admin | admin | Set via ADMIN_USERNAME env |
| Radicale | admin | admin | HTTP Basic |
| GoToSocial | (needs setup) | | Create user via admin API |
| Mailpit | (none) | | Accepts any SMTP |
