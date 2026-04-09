"""OAS MCP Client — manages MCP server lifecycle, tool discovery, routing, and logging.

Extracted from run_eval.py to provide a clean, testable, reusable MCP client
that can be used by run_eval.py, run_batch.py, or any other evaluation script.

Usage:
    client = OASMCPClient(server_hostname="localhost")
    config = client.build_config(task_path, enable_mcp=True)
    await client.connect()
    tools = await client.discover_tools()
    await client.inject_tools(agent)
    obs = await client.call_tool(action)
    client.close()
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time as _time_mod
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib

from openhands.core.config import MCPConfig
from openhands.core.config.mcp_config import MCPStdioServerConfig
from openhands.events.action.mcp import MCPAction
from openhands.events.observation.observation import Observation
from openhands.utils.async_utils import call_async_from_sync
import openhands.mcp.utils as _oh_mcp_utils

import platform_config

log = logging.getLogger("oas.mcp_client")

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

MCP_SERVERS_DIR = os.path.join(os.path.dirname(__file__), "mcp_servers")
MCP_REGISTRY_PATH = os.path.join(MCP_SERVERS_DIR, "mcp_registry.toml")
HOST_PYTHON = os.path.join(
    os.environ.get("CONDA_PREFIX", sys.prefix), "bin", "python"
)

# ---------------------------------------------------------------------------
# Tool classification (used for action log enrichment)
# ---------------------------------------------------------------------------

# Fallback classification sets — used when servers don't report
# x-oas-classification in their inputSchema (backward compat).
_FALLBACK_SEND_TOOLS: set[str] = {
    "rc_send_dm", "rc_send_channel", "mailpit_send",
    "pleroma_post", "pleroma_send_dm",
}

_FALLBACK_RETRIEVAL_TOOLS: set[str] = {
    "rc_search", "rc_dm_history",
    "mailpit_search", "mailpit_read",
    "pleroma_read", "pleroma_list",
    "wikijs_search", "wikijs_read",
    "owncloud_read", "owncloud_list",
    "radicale_search", "radicale_read",
    "memory_recall", "memory_search", "memory_list",
    "gitlab_search_code", "gitlab_read_file", "gitlab_list_files",
    "plane_list_issues", "plane_read_issue", "plane_search_issues",
}

# Deprecated aliases — kept for any external code that references them.
SEND_TOOLS = _FALLBACK_SEND_TOOLS
RETRIEVAL_TOOLS = _FALLBACK_RETRIEVAL_TOOLS


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_registry() -> dict:
    """Load the declarative MCP server registry from TOML."""
    with open(MCP_REGISTRY_PATH, "rb") as f:
        return tomllib.load(f)


def _load_task_deps(task_path: str) -> list[str]:
    """Load service dependencies from a task's dependencies.yml."""
    import yaml
    deps_path = os.path.join(task_path, "utils", "dependencies.yml")
    if not os.path.exists(deps_path):
        return []
    try:
        with open(deps_path) as f:
            d = yaml.safe_load(f)
            return d if isinstance(d, list) else []
    except Exception:
        return []


def _interpolate_args(args: list[str], variables: dict[str, str]) -> list[str]:
    """Substitute {var} placeholders in server arg lists."""
    result = []
    for arg in args:
        for key, value in variables.items():
            arg = arg.replace(f"{{{key}}}", value)
        result.append(arg)
    return result


# ---------------------------------------------------------------------------
# OASMCPClient
# ---------------------------------------------------------------------------

class OASMCPClient:
    """MCP client for OAS — manages server lifecycle, tool discovery,
    routing, and action logging.

    Replaces the scattered module-level globals and functions that were
    previously in run_eval.py.
    """

    def __init__(
        self,
        server_hostname: str | None = None,
    ) -> None:
        self._hostname = server_hostname or platform_config.hostname()
        self._config: MCPConfig | None = None
        self._clients: list | None = None
        self._action_log: list[dict[str, Any]] = []
        self._log_path: str | None = None
        self._tool_classifications: dict[str, str] = {}  # tool_name -> "send"|"retrieve"|"mutate"

    # ------------------------------------------------------------------
    # Config building
    # ------------------------------------------------------------------

    def build_config(
        self,
        task_path: str,
        enable_mcp: bool,
    ) -> MCPConfig | None:
        """Build MCP config from registry TOML, filtered by task dependencies.

        Returns None if MCP is disabled or no servers match.
        """
        if not enable_mcp:
            self._config = None
            return None

        registry = _load_registry()
        deps = _load_task_deps(task_path)
        creds = platform_config.credentials()
        variables = {"host": self._hostname, **creds}

        servers: list[MCPStdioServerConfig] = []
        for name, server_def in registry.get("servers", {}).items():
            always = server_def.get("always_enabled", False)
            dep_match = any(d in deps for d in server_def.get("depends_on", []))
            if not (always or dep_match):
                continue

            args = _interpolate_args(server_def.get("args", []), variables)
            servers.append(MCPStdioServerConfig(
                name=name,
                command=HOST_PYTHON,
                args=[f"{MCP_SERVERS_DIR}/{server_def['script']}"] + args,
            ))

        if not servers:
            self._config = None
            return None

        self._config = MCPConfig(stdio_servers=servers)
        log.info("MCP config: %d servers: %s",
                 len(servers), [s.name for s in servers])
        return self._config

    @property
    def config(self) -> MCPConfig | None:
        return self._config

    @property
    def is_configured(self) -> bool:
        return self._config is not None and bool(self._config.stdio_servers)

    # ------------------------------------------------------------------
    # Client lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """Create cached MCP stdio clients (avoids per-call subprocess spawns)."""
        if self._clients is not None:
            return
        if not self.is_configured:
            return
        self._clients = await _oh_mcp_utils.create_mcp_clients(
            [], [], None, self._config.stdio_servers
        )
        log.info("Created %d cached MCP clients", len(self._clients))

    def close(self) -> None:
        """Release cached MCP clients, closing stdio transports."""
        if not self._clients:
            return
        for client in self._clients:
            try:
                if hasattr(client, '__aexit__'):
                    call_async_from_sync(client.__aexit__(None, None, None))
                elif hasattr(client, 'close'):
                    result = client.close()
                    if asyncio.iscoroutine(result):
                        call_async_from_sync(result)
            except Exception as e:
                log.debug("Error closing MCP client: %s", e)
        log.info("Closed %d cached MCP clients", len(self._clients))
        self._clients = None

    # ------------------------------------------------------------------
    # Tool discovery
    # ------------------------------------------------------------------

    async def discover_tools(self) -> list[dict]:
        """Fetch tool definitions from all connected MCP servers.

        Returns a list of OpenAI-format tool dicts (with "function" key).
        Also populates ``_tool_classifications`` from ``x-oas-classification``
        embedded in each tool's ``inputSchema`` (set by ``OASMCPServer``).
        """
        if not self.is_configured:
            return []
        tools = await _oh_mcp_utils.fetch_mcp_tools_from_config(
            self._config, use_stdio=True
        )
        # Extract classification metadata from tool schemas
        self._tool_classifications.clear()
        for t in tools:
            name = t["function"]["name"]
            params = t["function"].get("parameters", {})
            cls = params.get("x-oas-classification", "")
            if cls:
                self._tool_classifications[name] = cls
        tool_names = [t["function"]["name"] for t in tools]
        classified = len(self._tool_classifications)
        log.info("Discovered %d MCP tools (%d classified): %s",
                 len(tools), classified, tool_names)
        return tools

    async def inject_tools(self, agent) -> MCPConfig | None:
        """Discover tools and inject them into an OpenHands agent.

        Returns the MCP config (for passing to OpenHands runtime).
        """
        if not self.is_configured:
            agent.set_mcp_tools([])
            return MCPConfig()
        tools = await self.discover_tools()
        agent.set_mcp_tools(tools)
        return self._config

    # ------------------------------------------------------------------
    # Tool invocation
    # ------------------------------------------------------------------

    async def call_tool(self, action: MCPAction) -> Observation:
        """Execute an MCP tool call, with timing and logging."""
        if not self.is_configured:
            from openhands.events.observation import ErrorObservation
            return ErrorObservation("No MCP servers configured")

        if self._clients is None:
            await self.connect()

        t0 = _time_mod.monotonic()
        obs = await _oh_mcp_utils.call_tool_mcp(self._clients, action)
        elapsed = (_time_mod.monotonic() - t0) * 1000
        self._log_action(action, obs, elapsed)

        return obs

    # ------------------------------------------------------------------
    # Action logging
    # ------------------------------------------------------------------

    def set_log_path(self, path: str) -> None:
        """Set the JSONL log file path. Truncates any existing file."""
        self._log_path = path
        self._action_log.clear()
        if os.path.exists(path):
            open(path, "w").close()

    def get_action_log(self) -> list[dict]:
        """Return the in-memory action log."""
        return list(self._action_log)

    def _log_action(self, action: MCPAction, obs: Observation, elapsed_ms: float) -> None:
        """Log an MCP tool call to memory and JSONL file."""
        tool_name = getattr(action, "name", "") or getattr(action, "tool_name", "")
        arguments = getattr(action, "arguments", {}) or {}
        is_error = getattr(obs, "is_error", False) or False
        result_text = getattr(obs, "content", "")

        entry: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "tool_name": tool_name,
            "arguments": arguments if isinstance(arguments, dict) else str(arguments),
            "elapsed_ms": round(elapsed_ms, 1),
            "is_error": is_error,
        }

        # Determine tool classification from discovery metadata, falling
        # back to the hardcoded sets for unclassified tools.
        classification = self._tool_classifications.get(tool_name, "")
        if not classification:
            if tool_name in _FALLBACK_SEND_TOOLS:
                classification = "send"
            elif tool_name in _FALLBACK_RETRIEVAL_TOOLS:
                classification = "retrieve"

        if classification:
            entry["classification"] = classification

        # Capture results for send and retrieval tools
        if classification == "send":
            entry["result"] = result_text[:4000] if result_text else ""
        elif classification == "retrieve":
            entry["result"] = result_text[:8000] if result_text else ""

        self._action_log.append(entry)

        if self._log_path:
            try:
                with open(self._log_path, "a") as f:
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            except Exception as e:
                log.warning("Failed to write MCP action log: %s", e)

    # ------------------------------------------------------------------
    # OpenHands monkey-patch (host-side bypass)
    # ------------------------------------------------------------------

    def install_host_bypass(self) -> None:
        """Monkey-patch OpenHands to route MCP calls through this client.

        OH's in-container SSE proxy returns 0 tools. This bypass makes MCP
        calls go through host-side stdio clients instead.
        """
        if not self.is_configured:
            return

        # Capture self for closures
        client = self

        async def _patched_add_mcp_tools(agent, runtime, memory):
            return await client.inject_tools(agent)

        async def _patched_call_tool(oh_self, action: MCPAction) -> Observation:
            return await client.call_tool(action)

        # 1. Patch the source module
        _oh_mcp_utils.add_mcp_tools_to_agent = _patched_add_mcp_tools
        # 2. Patch the package-level re-export
        import openhands.mcp as _mcp_pkg
        _mcp_pkg.add_mcp_tools_to_agent = _patched_add_mcp_tools
        # 3. Patch the already-imported binding in openhands.core.main
        import openhands.core.main as _main_mod
        _main_mod.add_mcp_tools_to_agent = _patched_add_mcp_tools
        # 4. Patch call_tool_mcp on the action execution client class
        from openhands.runtime.impl.action_execution import action_execution_client as _aec
        _aec.ActionExecutionClient.call_tool_mcp = _patched_call_tool

        log.info("Installed host-side MCP bypass (OH SSE proxy workaround)")
