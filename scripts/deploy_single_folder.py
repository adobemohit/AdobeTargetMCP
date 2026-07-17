#!/usr/bin/env python3
"""Deploy a single activity folder to Adobe Target via MCP."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Deploy one activity folder to Adobe Target via MCP."
    )
    parser.add_argument("folder", help="Activity folder path relative to repo root")
    parser.add_argument(
        "--root",
        help="GitOps repository root (defaults to parent of scripts/)",
    )
    parser.add_argument(
        "--mode",
        choices=["create", "update"],
        default="create",
        help="Deploy mode for the activity folder",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    if args.root:
        os.environ["GITOPS_ROOT"] = str(Path(args.root).resolve())

    from deploy_to_target_mcp import (
        McpClient,
        deploy_activity_folder,
        extract_tool_result,
        load_config,
        parse_response,
        resolve_access_token,
    )
    from activity_discovery import ROOT

    folder = (ROOT / args.folder).resolve()
    if not folder.exists():
        print(f"Activity folder not found: {folder}", file=sys.stderr)
        return 1

    config = load_config()
    mcp_url = os.environ.get(
        "MCP_SERVER_URL",
        config.get("mcp_server_url", "https://targetmcp.adobe.io/mcp"),
    )

    try:
        token = resolve_access_token(config)
    except Exception as error:
        print(f"Failed to resolve Adobe access token: {error}", file=sys.stderr)
        return 1

    client = McpClient(mcp_url, token)
    print("Connecting to Adobe Target MCP server...", file=sys.stderr)
    client.initialize()

    result = deploy_activity_folder(client, folder, config, deploy_mode=args.mode)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
