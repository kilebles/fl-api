"""Model Context Protocol (MCP) control plane for fl-api and upstream pipeline stages."""

from src.mcp.client import FlPlatformClient, PlatformAPIError
from src.mcp.server import create_mcp_server, mcp_lifespan, mount_mcp_on_app

__all__ = [
    "FlPlatformClient",
    "PlatformAPIError",
    "create_mcp_server",
    "mcp_lifespan",
    "mount_mcp_on_app",
]
