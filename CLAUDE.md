# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

OpenHands Software Agent SDK -- a Python monorepo for building AI agents that work with code. It powers the OpenHands CLI, OpenHands Cloud, and OpenHands Enterprise. The SDK supports local execution, Docker/Apptainer containers, and remote agent servers.

## Build & Development Commands

Requires `uv >= 0.8.13`. Never use `mypy` -- this repo uses **pyright** for type checking.

```bash
make build                              # Setup dev env (uv sync --dev + pre-commit hooks)
make format                             # Format with ruff
make lint                               # Lint with ruff check --fix
uv run pre-commit run --files <path>    # Check specific files after editing
uv run pre-commit run --all-files       # Run all pre-commit checks
make clean                              # Remove __pycache__ and caches
make build-server                       # Build agent-server binary (dist/agent-server/)
make test-server-schema                 # Validate OpenAPI schema
```

## Testing

```bash
uv run pytest                                  # All tests
uv run pytest tests/sdk/                       # SDK tests only
uv run pytest tests/tools/                     # Tools tests only
uv run pytest tests/workspace/                 # Workspace tests only
uv run pytest tests/agent_server/              # Server tests only
uv run pytest tests/sdk/tool/test_tool.py      # Single file
uv run pytest tests/sdk/ -k "pattern"          # By pattern
```

Test structure mirrors source: changes to `openhands-sdk/openhands/sdk/tool/tool.py` should have tests in `tests/sdk/tool/test_tool.py`. Don't write test classes unless necessary. Shared setup belongs in `conftest.py` as fixtures.

Behavior tests (`b##_*`) and functional tests (`t##_*`) live in `tests/integration/tests/` -- see `tests/integration/BEHAVIOR_TESTS.md` before modifying.

For prompt/tool-description/agent-decision changes, add the `integration-test` label to trigger integration tests.

## Monorepo Architecture

Four `uv`-managed workspace packages under a single `uv.lock`:

| Package | Namespace | Purpose |
|---|---|---|
| `openhands-sdk/` | `openhands.sdk.*` | Core SDK: agent loop, LLM abstraction, tools, events, conversations, settings, security, MCP, plugins, subagents |
| `openhands-tools/` | `openhands.tools.*` | Built-in tools: terminal, file_editor, browser_use, grep, glob, apply_patch, task_tracker, gemini, delegate |
| `openhands-workspace/` | `openhands.workspace.*` | Workspace implementations: local, Docker, Apptainer, cloud, remote API |
| `openhands-agent-server/` | `openhands.agent_server.*` | FastAPI REST/WebSocket server for remote agent execution |

Each package has its own `AGENTS.md` with package-specific policies. When a PR spans packages, consult each relevant one:
- SDK: `openhands-sdk/openhands/sdk/AGENTS.md`
- Tools: `openhands-tools/openhands/tools/AGENTS.md`
- Workspace: `openhands-workspace/openhands/workspace/AGENTS.md`
- Agent server: `openhands-agent-server/AGENTS.md`

## Key Architectural Concepts

**Agent loop**: `Conversation.send_message()` -> `Agent.step()` loops: prepare messages -> call LLM with tools -> parse tool calls -> execute tools (serial or parallel) -> emit observation events -> repeat until done or max steps.

**Tool system**: Tools are Pydantic models with `Action` (input) and `Observation` (output) types. Tools register globally via `ToolRegistry`. Each tool has a `definition.py` (schema/metadata) and `impl.py` (executor). Tool schemas are user-facing and must not break without deprecation.

**Event system**: All state flows through typed events (`MessageEvent`, `ActionEvent`, `ObservationEvent`, etc.). Events are persisted via `EventStore`. Old serialized events must always load -- use `handle_deprecated_model_fields()` with `_DEPRECATED_FIELDS` when removing fields from Pydantic models.

**Conversation state**: `Conversation` wraps the agent loop with event persistence, stats tracking, stuck-loop detection, pause/resume, and secret management. Plugins are lazy-loaded on first `send_message()` or `run()`.

**LLM abstraction**: `LLM` class supports 100+ models (OpenAI, Anthropic, Google, Bedrock, etc.) with unified tool-calling, streaming, extended thinking, vision, retry/fallback, and token tracking. For Bedrock with IAM/SigV4 auth, do not forward `LLM.api_key` to LiteLLM.

**Workspace abstraction**: Uniform interface (`execute_command`, `read_file`, `write_file`, `list_directory`) with implementations for local filesystem, Docker containers, Apptainer, cloud, and remote API.

## Code Style

- Ruff: line-length 88, double quotes, space indent, Python 3.13 target
- isort with `known-first-party = ["openhands"]`
- ARG (unused arguments) lint rule is ignored in `tests/**/*.py`
- Avoid `# type: ignore` -- fix typing instead. Avoid `getattr`/`hasattr` guards; use explicit type assertions.
- No `sys.path.insert` hacks. No inline imports unless circular dependency requires it.
- For E501 (line too long): break code across lines; for single-line strings use `("A" "B" "C")`; for docstrings add `# type: ignore` AFTER the closing `"""`, never inside.

## API Compatibility

- **SDK Python API**: symbols in `openhands.sdk.__all__` must be deprecated (via `@deprecated` or `warn_deprecated`) before removal. Breaking changes require a MINOR version bump. Enforced by `check_sdk_api_breakage.py`.
- **REST API**: breaking changes need a deprecation notice with 5 minor releases of runway before removal. Enforced by `Agent server REST API breakage checks` CI.
- **Pydantic event models**: old events must always load. Use `_DEPRECATED_FIELDS` + `handle_deprecated_model_fields()` when removing fields. Never use `extra="forbid"` without a deprecation handler.
- Metadata-only changes to `Field(...)` (description, title, examples, json_schema_extra, deprecated) are non-breaking.


## Examples

47+ runnable examples in `examples/`. Each must print `EXAMPLE_COST: <value>` to stdout (use `EXAMPLE_COST: 0` for non-LLM examples). Run with:

```bash
LLM_BASE_URL="https://llm-proxy.eval.all-hands.dev" LLM_API_KEY="$LLM_API_KEY" \
  uv run python examples/01_standalone_sdk/<example_name>.py
```
