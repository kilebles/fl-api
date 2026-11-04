"""
Run the standalone MCP server over stdio (for Antigravity, Claude Desktop, Cursor, etc.).

Talks over HTTP to a running `fl-api` (or unified Caddy gateway) so it never opens a
duplicate `JobQueue` or competes for browser profiles.

Usage:
    uv run python scripts/mcp_server.py
    FL_MCP_BASE_URL=https://your-site.com FL_MCP_TOKEN=secret uv run python scripts/mcp_server.py
"""

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from loguru import logger

from src.core.settings import get_settings
from src.mcp.server import create_mcp_server


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the fl-platform MCP server over stdio")
    parser.add_argument("--base-url", default=os.environ.get("FL_MCP_BASE_URL"), help="fl-api or Caddy gateway URL")
    parser.add_argument("--token", default=os.environ.get("FL_MCP_TOKEN"), help="Bearer API_TOKEN or session token")
    args = parser.parse_args()

    # MCP stdio strictly reserves stdout for JSON-RPC frames; send all logs to stderr.
    logger.remove()
    logger.add(sys.stderr, level="WARNING")

    settings = get_settings()
    mcp = create_mcp_server(
        settings,
        base_url=args.base_url,
        api_token=args.token,
    )
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
