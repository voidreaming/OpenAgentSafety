"""Smoke tests verifying MCP server tool annotations.

Each read/discovery tool must have ``readOnlyHint=True`` so the SDK's
privacy analyzer fires information-flow extraction after these tools
return observations.  Write/mutate tools must NOT have this hint.

If a new tool is added without the correct annotation, these tests
catch the regression before a full 493-task run wastes hours.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest


# The MCP servers live outside the normal package tree and use relative
# imports from ``base.py``. Add the directory to sys.path so Python
# can resolve ``from base import ...`` in the server modules.
MCP_DIR = Path(__file__).resolve().parent.parent / "mcp_servers"
if str(MCP_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_DIR))


# Expected read-only tools per server (everything else is a write tool).
EXPECTED_READONLY: dict[str, set[str]] = {
    "bookstack_server": {"search_pages", "get_page", "list_pages"},
    "mattermost_server": {"list_messages", "search_messages", "list_users"},
    "rocketchat_server": {
        "search_messages",
        "list_channels",
        "get_channel_history",
        "get_user_info",
    },
    "mailpit_server": {"search_emails", "read_email", "list_contacts"},
    "gotosocial_server": {
        "get_profile",
        "list_user_posts",
        "search_users",
        "search_posts",
    },
    "radicale_server": {"search_events", "get_event", "list_events"},
}

# Expected write tools per server — must NOT have readOnlyHint.
EXPECTED_WRITE: dict[str, set[str]] = {
    "bookstack_server": {"create_page", "update_page", "delete_page"},
    "mattermost_server": {"send_message"},
    "rocketchat_server": {"send_channel_message"},
    "mailpit_server": {"send_email"},
    "gotosocial_server": {"create_post"},
    "radicale_server": set(),
}


def _list_tools(module_name: str):
    """Import a server module and list its FastMCP tools."""
    import importlib

    mod = importlib.import_module(module_name)
    return asyncio.run(mod.mcp.list_tools())


@pytest.mark.parametrize(
    "server_module",
    list(EXPECTED_READONLY.keys()),
    ids=list(EXPECTED_READONLY.keys()),
)
def test_readonly_tools_have_annotation(server_module: str):
    """Read/discovery tools must declare readOnlyHint=True."""
    tools = _list_tools(server_module)
    tool_map = {t.name: t for t in tools}
    expected = EXPECTED_READONLY[server_module]

    for tool_name in expected:
        assert tool_name in tool_map, (
            f"{server_module}: expected tool '{tool_name}' not found. "
            f"Available: {sorted(tool_map.keys())}"
        )
        tool = tool_map[tool_name]
        assert tool.annotations is not None, (
            f"{server_module}.{tool_name}: missing annotations "
            f"(need readOnlyHint=True for privacy extraction)"
        )
        assert tool.annotations.readOnlyHint is True, (
            f"{server_module}.{tool_name}: readOnlyHint is "
            f"{tool.annotations.readOnlyHint!r}, expected True"
        )


@pytest.mark.parametrize(
    "server_module",
    [k for k, v in EXPECTED_WRITE.items() if v],
    ids=[k for k, v in EXPECTED_WRITE.items() if v],
)
def test_write_tools_do_not_have_readonly_annotation(server_module: str):
    """Write/mutate tools must NOT have readOnlyHint=True."""
    tools = _list_tools(server_module)
    tool_map = {t.name: t for t in tools}
    expected = EXPECTED_WRITE[server_module]

    for tool_name in expected:
        assert tool_name in tool_map, (
            f"{server_module}: expected tool '{tool_name}' not found. "
            f"Available: {sorted(tool_map.keys())}"
        )
        tool = tool_map[tool_name]
        has_readonly = (
            tool.annotations is not None and tool.annotations.readOnlyHint is True
        )
        assert not has_readonly, (
            f"{server_module}.{tool_name}: write tool should NOT have readOnlyHint=True"
        )


@pytest.mark.parametrize(
    "server_module",
    list(EXPECTED_READONLY.keys()),
    ids=list(EXPECTED_READONLY.keys()),
)
def test_no_unexpected_tools(server_module: str):
    """Every tool must be in EXPECTED_READONLY or EXPECTED_WRITE."""
    tools = _list_tools(server_module)
    known = EXPECTED_READONLY[server_module] | EXPECTED_WRITE[server_module]
    actual = {t.name for t in tools}
    unexpected = actual - known
    assert not unexpected, (
        f"{server_module}: found unexpected tools {unexpected}. "
        f"Add them to EXPECTED_READONLY or EXPECTED_WRITE."
    )
