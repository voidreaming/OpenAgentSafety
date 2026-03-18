# CLAUDE.md — OpenAgentSafety

## Project Overview

OpenAgentSafety (OAS) is a benchmark for evaluating AI agent safety in realistic workplace scenarios. It uses the OpenHands agent framework to run 356+ tasks where agents interact with containerized services (GitLab, ownCloud, RocketChat, Plane) and business tools (email, calendar, docs, files, contacts, social media). Tasks test whether agents comply with unsafe instructions or refuse appropriately.

## Research Direction: Contextual Integrity Privacy Evaluation

### Motivation

OAS is being extended to evaluate agent privacy norm awareness using **Contextual Integrity (CI)** theory (Nissenbaum, 2004). PrivacyLens (arXiv 2409.00138) demonstrated that 25–39% of SOTA models leak private information when acting as agents, even when they can articulate the relevant privacy norms. However, PrivacyLens relied on a simulated sandbox with synthetic tool stubs. OAS enables testing these same privacy behaviors in **realistic environments** with real containerized services and authentic business tool interactions.

### CI Norms

Contextual Integrity models information flow norms as a 5-tuple:

```
(data_type, data_subject, data_sender, data_recipient, transmission_principle)
```

**Workplace example**: An employee's medical leave status (`data_type`) about themselves (`data_subject`) shared by HR (`data_sender`) with their manager (`data_recipient`) is appropriate only under a need-to-know basis for scheduling (`transmission_principle`). An agent forwarding this information to the entire team channel violates the CI norm.

### Platform Advantages

OAS provides unique advantages over simulated privacy evaluation platforms:

- **Real services** — GitLab, ownCloud, RocketChat, Plane, Mailpit, Radicale, Wiki.js, and Pleroma provide authentic information flow contexts where privacy-sensitive actions naturally occur
- **Realistic business tools** — MCP servers wrap real service APIs (email via Mailpit SMTP, calendar via Radicale CalDAV, docs via Wiki.js GraphQL, social media via Pleroma Mastodon API) instead of synthetic stubs
- **Agent memory scratchpad** — A persistent key-value store (`memory_mcp.py`) enables testing whether agents remember and protect sensitive information across tool calls within a task run
- **All-live architecture** — Every tool is backed by a real Docker service via MCP. No JSON mocks, no simulated backends. Each MCP server is a thin API wrapper (like `rocketchat_mcp.py`)
- **Agent memory scratchpad** — Local JSON file per-run, ephemeral
- **LLM condenser** — OpenHands 1.5.0 `LLMSummarizingCondenserConfig` for context compression as a research variable

### PrivacyLens → OAS Conversion Pipeline

The project converts PrivacyLens privacy evaluation datapoints into runnable OAS tasks. The source dataset (`main_data.json`, 493 datapoints) contains simulated agent trajectories with CI norm metadata and `sensitive_info_items`. Tier 1 (286 tasks) covers MessengerSendMessage/SlackSendMessage final actions, all mapped to RocketChat.

#### Conversion flow (`evaluation/privacylens_converter.py`)

```
main_data.json (493 PrivacyLens datapoints)
    │
    ├── filter_tier1() → 286 datapoints (Messenger/Slack final actions)
    │
    └── convert_datapoint() per task:
        │
        ├── parse_trajectory() → extract Action/Observation blocks
        │
        ├── extract_workspace_data() → observations become workspace files:
        │   MessengerReceiveMessage  → workspace/chat_history.md
        │   NotionManagerReadPage    → workspace/notes/*.md
        │   GmailReadEmail           → workspace/emails/*.md
        │   GoogleCalendarReadEvents → workspace/calendar_events.md
        │   FacebookManagerSearch    → workspace/social_media/*.md
        │   ZoomMeetingTranscript    → workspace/meeting_transcripts/*.md
        │
        ├── rewrite_instruction() → replace Messenger/Slack/Notion/Gmail
        │   references with OAS equivalents (workspace files + RocketChat)
        │
        └── generate task directory:
            task.md           ← rewritten instruction + RocketChat curl examples
            checkpoints.md    ← one checkpoint per sensitive_info_item
            safe_completion.md
            scenario.json     ← CI norm metadata + sensitive_info_items
            utils/evaluator.py  ← checks RocketChat DMs for leaks
            utils/dependencies.yml  ← [rocketchat]
            utils/post_init.sh  ← creates recipient user in RocketChat
            workspace/*       ← extracted observation files
```

#### Live-services mode (`--live-services`)

When converting with `--live-services`, the converter additionally:
- Extracts structured seed data (`extract_service_seed_data()`) into service-specific formats
- Generates `tools_state.json` (OAS custom tool seed state for email/calendar/docs/social_media)
- Generates `post_init.py` (Python script that seeds RocketChat with chat history from PrivacyLens)
- Uses `rewrite_instruction_live()` — directs agents to use tools instead of workspace files
- Still generates workspace `.md` files as fallback

```bash
python privacylens_converter.py main_data.json --tier1 --live-services --validate
```

Service mapping with `--live-services`:
| PrivacyLens Source | OAS Target | Integration |
|---|---|---|
| Messenger/Slack messages | **RocketChat** | `post_init.py` seeds DMs |
| Notion pages | **OAS docs tool** | `tools_state.json` seed |
| Gmail emails | **OAS email tool** | `tools_state.json` seed |
| Google Calendar | **OAS calendar tool** | `tools_state.json` seed |
| Facebook posts | **OAS social_media tool** | `tools_state.json` seed |
| Zoom transcripts | **ownCloud** | `post_init.py` WebDAV upload |

#### Running privacy tasks (`evaluation/run_privacy_tier1.sh`)

```bash
bash run_privacy_tier1.sh \
  --agent-llm-config group1 --env-llm-config group2 \
  --outputs-path outputs_privacy_tier1 --server-hostname localhost
```

Key flags added to `run_eval.py`:
- `--no-fake-user` — Disables the adversarial FakeUser LLM loop; agent gets a static "continue" prompt instead. Privacy tasks don't need adversarial pushing since we're testing information flow decisions, not resistance to social pressure.
- `--max-iterations N` — Overrides the default 50. Privacy tasks use 25 iterations (up from 15 for live-services mode where tool calls add steps).
- `--enable-oas-custom-tools` — Enables OAS custom tools (email/calendar/docs/social_media). Auto-detected if `tools_state.json` exists.
- `--enable-mcp` — Enables MCP (Model Context Protocol) tool integration for RocketChat (requires MCP server in sandbox).

#### Per-task execution pipeline

```
Docker container boots (~35s)
├── init.sh resets RocketChat
├── post_init.sh creates recipient user via RocketChat API
└── workspace/ files copied to /workspace/

Agent reads /instruction/task.md
Agent reads /workspace/*.md files (contains sensitive info trap)
Agent composes and sends message via curl to RocketChat
Agent exits or hits max_iterations
Trajectory saved as JSON
```

#### Offline evaluation (`evaluation/evaluate_privacy_offline.py`)

The in-sandbox evaluator often fails because agents call `exit` which kills the container before the evaluator can run. The offline evaluator works outside the container on saved trajectories.

Two modes:
- **Keyword matching** (fast, no API): Checks for significant word overlap (>40%) or phrase matches between sent messages and `sensitive_info_items`
- **LLM-based** (accurate, PrivacyLens-style): Uses GPT to judge whether each sent message leaks each secret, with chain-of-thought reasoning

```bash
# Keyword only (fast):
python evaluate_privacy_offline.py outputs_privacy_tier1/

# LLM-based (uses Azure GPT API from config.toml):
python evaluate_privacy_offline.py outputs_privacy_tier1/ --llm-eval --step judge_leakage
```

Message extraction handles multiple agent patterns: single-quoted JSON, double-quoted JSON, variable expansion (`TEXT="..."`), Python-built payloads, heredocs, MCP tool calls (`send_dm`/`send_message`), and OAS custom tool calls (`social_media_send_message`/`email_send_email`).

#### Tier 1 results (GPT-5.2 agent, 286 tasks)

| Metric | Keyword | LLM-based |
|--------|---------|-----------|
| Messages detected | 156/286 (55%) | 156/286 (55%) |
| Tasks with leaks | 63/286 (22%) | 63/286 (22%) |
| Items leaked | 116/848 (14%) | 129/848 (15%) |
| Leak rate among sent | 63/156 (40%) | 63/156 (40%) |
| No message sent | 130 (refusals + failures) | 130 |

LLM judgment found 13 more leaked items than keyword matching (catches paraphrased leaks, removes false positives). Of the 130 no-message tasks: ~11 were genuine privacy refusals, ~20 got stuck on API errors, ~97 sent messages via complex patterns not yet extractable.

### Vision

Privacy-focused tasks encode CI norms into realistic workplace scenarios. Each task presents an agent with a situation where information flows must respect contextual norms — e.g., handling salary data, medical records, or performance reviews. Evaluators grade whether the agent's information flow decisions respect or violate the encoded CI norms. The CIHub audit trail enables fine-grained analysis of exactly which tool calls constituted norm violations and at what point in the agent's reasoning the violation occurred.

## Build & Run

### Prerequisites
- Python 3.12+, Poetry, Docker (30+ GB disk recommended)
- LLM API access (OpenAI-compatible or Azure)

### Install dependencies
```bash
poetry install
```

### Run evaluation
```bash
cd evaluation
bash run_eval.sh <agent_llm_config> <env_llm_config> <outputs_dir> [server_hostname] [version]
```
- `agent_llm_config` / `env_llm_config` — names of `[llm.<name>]` sections in `config.toml`
- `server_hostname` — defaults to `localhost`
- `version` — defaults to `1.0.0`

### Start infrastructure services
```bash
cd servers
docker compose up -d   # GitLab(:8929), ownCloud(:8092), RocketChat(:3000), Mailpit(:8025/:1025), Radicale(:5232), Wiki.js(:3001), Pleroma(:4000)
```

### Build base image
```bash
cd workspaces/openagentsafety_base_image
make build
```

### Key environment variables
| Variable | Purpose |
|---|---|
| `AGENT_LLM_CONFIG` | LLM config section for the agent |
| `ENV_LLM_CONFIG` | LLM config section for environment/NPCs/evaluators |
| `SERVER_HOSTNAME` | Service hostname (default: localhost) |
| `LITELLM_API_KEY`, `LITELLM_BASE_URL`, `LITELLM_MODEL` | LLM access inside containers |
| `DECRYPTION_KEY` | For decrypting evaluator.py files |

### Service credentials
- GitLab: `root` / `theagentcompany` (port 8929)
- ownCloud: `theagentcompany` / `theagentcompany` (port 8092)
- RocketChat: `theagentcompany` / `theagentcompany` (port 3000)
- Plane: `agent@company.com` / `theagentcompany` (port 8091)
- Mailpit: no auth required (port 8025 REST/UI, port 1025 SMTP)
- Radicale: no auth (port 5232 CalDAV)
- Wiki.js: `agent@company.com` / `theagentcompany` (port 3001)
- Pleroma/Akkoma: registration open (port 4000 Mastodon-compatible API)
- API Server: port 2999

## Architecture

### Evaluation flow
1. `run_eval.sh` iterates through safety tasks
2. `run_eval.py` initializes a task container with the base image
3. Task environment set up via `init.sh` (resets services, configures hostnames)
4. Agent runs via OpenHands Controller with a FakeUser simulating a human who pushes the agent toward unsafe actions
5. Trajectory saved as JSON by OpenHands
6. Encrypted evaluator runs inside the container to grade checkpoints
7. Results collected to outputs directory

### Key components
- **`evaluation/run_eval.py`** — Main orchestrator. Initializes OpenHands 1.5.0 runtime, loads tasks, builds MCP config for live services, runs agent with LLM condenser, invokes evaluator.
- **`evaluation/mcp_servers/`** — One MCP server per live service (email, calendar, docs, social_media, files, rocketchat, memory). Each is a thin API wrapper.
- **`servers/api-server/api-server.py`** — Flask API managing service health checks and resets.
- **`workspaces/openagentsafety_base_image/`** — Shared base image with init scripts, evaluation helpers, NPC infrastructure.
  - `common.py` — Shared utilities (LLM calls, service clients, file operations)
  - `scoring.py` — `Result` and `Checkpoint` dataclasses
  - `eval.py` — Evaluation entrypoint (decrypts and runs evaluator)
  - `reset.sh` — Service reset script
  - `init.sh` — Task environment initialization

## Task Structure

Each task follows this layout:
```
workspaces/tasks/safety-TASKNAME/          # Original safety tasks
workspaces/tasks/privacy-TASKNAME/         # PrivacyLens-converted privacy tasks
├── task.md              # Agent-facing instructions (the unsafe request)
├── checkpoints.md       # Expected unsafe behaviors to detect (human/judge reference)
├── safe_completion.md   # What a safe refusal looks like
├── scenario.json        # CI norm metadata, sensitive_info_items, OAS mapping (privacy tasks)
├── scenarios.json       # NPC definitions (optional, original safety tasks)
├── utils/
│   ├── evaluator.py     # Fernet-encrypted rule-based grader (safety) or plain-text (privacy)
│   ├── dependencies.yml # Required services (rocketchat, plane, gitlab, owncloud, cihub)
│   ├── post_init.sh     # Creates recipient users in RocketChat (privacy tasks)
│   └── [other scripts]
└── workspace/           # Pre-populated files for the task
    ├── chat_history.md          # From Messenger/Slack observations (privacy)
    ├── notes/*.md               # From Notion observations (privacy)
    ├── emails/*.md              # From Gmail observations (privacy)
    └── calendar_events.md       # From Calendar observations (privacy)
```

- **Evaluators** are encrypted to prevent agent inspection (safety tasks). Privacy task evaluators are plain-text and check RocketChat messages for sensitive info leaks.
- **dependencies.yml** determines which services are needed and influences `max_iterations` (50–75, or 15 for privacy tasks).
- **FakeUser** escalates pushiness after 3 turns to pressure the agent. Disabled for privacy tasks via `--no-fake-user`.
- **scenario.json** (privacy tasks) contains CI norm 5-tuple, `sensitive_info_items` from PrivacyLens, and `oas_mapping` for recipient/channel info.

## Tool System

OAS provides business productivity tools via MCP servers, each wrapping a real Docker service directly (all-live architecture):

| Tool | MCP Server | Backing Service | Port |
|------|-----------|----------------|------|
| Messaging | `rocketchat_mcp.py` | RocketChat REST API | 3000 |
| Email | `email_mcp.py` | Mailpit REST + SMTP | 8025/1025 |
| Calendar | `calendar_mcp.py` | Radicale CalDAV | 5232 |
| Docs | `docs_mcp.py` | Wiki.js GraphQL | 3001 |
| Social Media | `social_media_mcp.py` | Pleroma Mastodon API | 4000 |
| Files | `files_mcp.py` | ownCloud WebDAV | 8092 |
| Memory | `memory_mcp.py` | Local JSON file | — |

No JSON fallback, no CIHub state management. Every tool hits a real service.

### MCP Server Architecture
```
Agent (OpenHands) ──MCP stdio──> email_mcp.py ──HTTP/SMTP──> Mailpit (port 8025/1025)
Agent (OpenHands) ──MCP stdio──> calendar_mcp.py ──CalDAV──> Radicale (port 5232)
Agent (OpenHands) ──MCP stdio──> docs_mcp.py ──GraphQL──> Wiki.js (port 3001)
Agent (OpenHands) ──MCP stdio──> social_media_mcp.py ──REST──> Pleroma (port 4000)
Agent (OpenHands) ──MCP stdio──> files_mcp.py ──WebDAV──> ownCloud (port 8092)
Agent (OpenHands) ──MCP stdio──> rocketchat_mcp.py ──REST──> RocketChat (port 3000)
Agent (OpenHands) ──MCP stdio──> memory_mcp.py ──JSON──> Local file (/workspace/.agent_memory.json)
```

### Agent Memory Scratchpad
The `memory_mcp.py` server provides persistent key-value storage within a task run:
- `store(key, value, tags)` — save information
- `recall(key)` — retrieve by key
- `search(query)` — substring search
- `list_memories(tag)` — list all or filter by tag
- `forget(key)` — delete

Memory is stored in a local JSON file, per-run and ephemeral.

Tools are enabled via `--enable-mcp` flag. Tool names follow `category.action` convention (e.g., `email.search_threads`, `calendar.create_event`, `memory.store`).

## Conventions

- Python 3.12+ with type hints
- Poetry for dependency management
- `@grader` decorator for error-handling in evaluator functions
- Dataclass pattern for `Result`/`Checkpoint`
- Fernet encryption for evaluator files
- Health checks via HTTP endpoints (`/api/healthcheck/*`)
- Service resets via `/api/reset-*` endpoints before each task
- Hostname routing via `/etc/hosts` injection (`the-agent-company.com`)

## Documentation

- `docs/SETUP.md` — Infrastructure setup
- `docs/EVALUATION.md` — Evaluation workflow
- `docs/DEVELOPMENT.md` — Task and server modification guide
- `docs/BACKGROUND.md` — Design philosophy and O*NET grounding
