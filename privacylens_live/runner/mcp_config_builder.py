"""Build MCP config dicts for the OpenHands agent from task specs."""

from __future__ import annotations

from privacylens_live.config import Config


def build_mcp_config(
    dependencies: list[str],
    config: Config,
) -> dict:
    """Build an MCP config dict from task dependencies.

    The MCP config uses Docker-internal URLs so the agent container
    (on privacylens-net) can reach the MCP servers by DNS name.

    Args:
        dependencies: List of MCP server names
            (e.g., ["bookstack", "gotosocial"])
        config: Platform configuration with MCP server URLs

    Returns:
        Dict suitable for Agent(mcp_config=...)
    """
    servers = {}
    for mcp_name in dependencies:
        url = config.mcp_server_urls.get(mcp_name)
        if url:
            servers[mcp_name] = {"url": url}
    return {"mcpServers": servers}
