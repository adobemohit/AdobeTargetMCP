#!/usr/bin/env python3
"""Deploy activity folders to Adobe Target via MCP on merge to main."""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "deploy.config.json"


class McpClient:
    def __init__(self, url: str, token: str) -> None:
        self.url = url
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        self.session_id: str | None = None
        self.request_id = 0

    def call(self, method: str, params: dict | None = None) -> dict:
        self.request_id += 1
        payload = {
            "jsonrpc": "2.0",
            "id": self.request_id,
            "method": method,
            "params": params or {},
        }

        headers = dict(self.headers)
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id

        with httpx.Client(timeout=120.0) as client:
            response = client.post(self.url, headers=headers, json=payload)
            response.raise_for_status()

            if "mcp-session-id" in response.headers:
                self.session_id = response.headers["mcp-session-id"]

            return parse_response(response)

    def initialize(self) -> dict:
        result = self.call(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {
                    "name": "analog-devices-github-deploy",
                    "version": "1.0.0",
                },
            },
        )
        self.call("notifications/initialized", {})
        return result

    def call_tool(self, name: str, arguments: dict) -> dict:
        return self.call("tools/call", {"name": name, "arguments": arguments})


def parse_response(response: httpx.Response) -> dict:
    content_type = response.headers.get("content-type", "")

    if "text/event-stream" in content_type:
        for line in response.text.splitlines():
            if line.startswith("data: "):
                return json.loads(line[6:])
        raise RuntimeError("SSE response did not contain JSON data")

    if not response.text.strip():
        return {}

    return response.json()


def extract_tool_result(response: dict) -> dict:
    if "error" in response:
        raise RuntimeError(f"MCP error: {response['error']}")

    result = response.get("result", {})
    content = result.get("content", [])

    for item in content:
        if item.get("type") == "text":
            text = item.get("text", "")
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return {"raw": text}

    return result


def load_config() -> dict:
    with CONFIG_PATH.open(encoding="utf-8") as handle:
        return json.load(handle)


def load_activity_info(folder: Path, info_file: str) -> dict:
    with (folder / info_file).open(encoding="utf-8") as handle:
        return json.load(handle)


def read_offer_html(folder: Path, html_file: str) -> str:
    return (folder / html_file).read_text(encoding="utf-8")


def map_activity_state(status: str, status_map: dict) -> str:
    return status_map.get(status.lower(), "saved")


def map_activity_type(activity_type: str, type_map: dict) -> str:
    return type_map.get(activity_type.upper(), activity_type.lower())


def infer_variant_from_filename(filename: str) -> str | None:
    match = re.search(r"_exp_([a-z0-9_]+)$", Path(filename).stem, re.IGNORECASE)
    if not match:
        return None
    suffix = match.group(1).lower()
    return suffix if suffix.startswith("variant_") else f"variant_{suffix}"


def build_offer_name(activity_info: dict, variant: str) -> str:
    activity_name = activity_info.get("activity_name", "Offer")
    label = variant.replace("variant_", "Variant ").replace("_", " ").title()
    return f"{activity_name} - {label}"


def resolve_actions(activity_info: dict, config: dict) -> dict:
    defaults = config.get("default_actions", {})
    overrides = activity_info.get("actions", {})
    return {**defaults, **overrides}


def resolve_offers(folder: Path, activity_info: dict, html_pattern: str) -> list[dict]:
    if variants := activity_info.get("variants"):
        return variants

    offers: list[dict] = []
    default_variant = activity_info.get("activity_variant")

    for html_path in sorted(folder.glob(html_pattern)):
        inferred_variant = infer_variant_from_filename(html_path.name)
        variant = inferred_variant or default_variant
        if not variant:
            print(
                f"Warning: could not determine variant for {html_path.name}, skipping."
            )
            continue

        offers.append(
            {
                "variant": variant,
                "html_file": html_path.name,
                "offer_name": build_offer_name(activity_info, variant),
                "offer_id": None,
                "mode": "create_or_update",
            }
        )

    return offers


def discover_activity_folders(config: dict) -> list[Path]:
    discovery = config.get("discovery", {})
    info_file = discovery.get("info_file", "activity-info.json")
    exclude_dirs = set(discovery.get("exclude_dirs", []))

    folders: list[Path] = []
    for info_path in ROOT.rglob(info_file):
        if any(part in exclude_dirs for part in info_path.parts):
            continue
        folders.append(info_path.parent)

    return sorted(folders)


def deploy_offer(client: McpClient, offer_config: dict, html_content: str) -> dict:
    offer_name = offer_config["offer_name"]
    offer_id = offer_config.get("offer_id")
    mode = offer_config.get("mode", "create_or_update")

    if mode == "update" or (mode == "create_or_update" and offer_id):
        if not offer_id:
            raise ValueError(f"offer_id is required to update offer '{offer_name}'")

        print(f"Updating Target offer {offer_id} ({offer_name})...")
        response = client.call_tool(
            "update_target_offer",
            {
                "offer_id": offer_id,
                "name": offer_name,
                "content": html_content,
            },
        )
        return extract_tool_result(response)

    print(f"Creating Target offer '{offer_name}'...")
    response = client.call_tool(
        "create_target_offer",
        {"name": offer_name, "content": html_content},
    )
    return extract_tool_result(response)


def attach_offer_to_variant(
    client: McpClient,
    activity_info: dict,
    offer_config: dict,
    html_content: str,
    offer_result: dict,
    type_map: dict,
) -> dict:
    activity_id = activity_info.get("activity_id")
    if not activity_id:
        print("Skipping variant attach: activity_id is 0 or missing.")
        return {"skipped": True, "reason": "activity_id missing"}

    activity_type = map_activity_type(activity_info.get("activity_type", "XT"), type_map)
    variant_name = offer_config.get("variant") or activity_info.get("activity_variant")
    offer_id = offer_result.get("id") or offer_config.get("offer_id")

    params = {
        "activity_id": activity_id,
        "activity_type": activity_type,
        "variant_name": variant_name,
    }

    if offer_id:
        params["offer_id"] = offer_id
    else:
        params["offer_content"] = html_content

    print(
        f"Attaching offer to activity {activity_id} "
        f"variant '{variant_name}'..."
    )
    response = client.call_tool("update_variant_offer", params)
    return extract_tool_result(response)


def sync_activity_state(
    client: McpClient, activity_info: dict, status_map: dict
) -> dict:
    activity_id = activity_info.get("activity_id")
    if not activity_id:
        print("Skipping activity state sync: activity_id is 0 or missing.")
        return {"skipped": True, "reason": "activity_id missing"}

    status = activity_info.get("activity_status", "saved")
    state = map_activity_state(status, status_map)

    print(f"Syncing activity {activity_id} state to '{state}'...")
    response = client.call_tool(
        "update_activity_state",
        {"activity_id": activity_id, "state": state},
    )
    return extract_tool_result(response)


def deploy_activity_folder(
    client: McpClient, folder: Path, config: dict
) -> dict:
    discovery = config.get("discovery", {})
    info_file = discovery.get("info_file", "activity-info.json")
    html_pattern = discovery.get("html_pattern", "*.html")

    activity_info = load_activity_info(folder, info_file)
    actions = resolve_actions(activity_info, config)
    offers = resolve_offers(folder, activity_info, html_pattern)

    results = {
        "folder": str(folder.relative_to(ROOT)).replace("\\", "/"),
        "activity_name": activity_info.get("activity_name"),
        "offers": [],
        "activity_state": None,
    }

    print(f"\nDeploying activity folder: {results['folder']}")

    if not offers:
        print("No offers found to deploy.")
        return results

    for offer_config in offers:
        html_file = offer_config["html_file"]
        html_content = read_offer_html(folder, html_file)

        if not html_content.strip():
            print(f"Warning: {html_file} is empty, skipping offer deploy.")
            continue

        if not actions.get("push_offer", True):
            continue

        offer_result = deploy_offer(client, offer_config, html_content)
        offer_entry = {
            "html_file": html_file,
            "variant": offer_config.get("variant"),
            "offer_result": offer_result,
        }

        if actions.get("attach_offer_to_variant", False):
            offer_entry["variant_attach_result"] = attach_offer_to_variant(
                client,
                activity_info,
                offer_config,
                html_content,
                offer_result,
                config.get("activity_type_map", {}),
            )

        results["offers"].append(offer_entry)

    if actions.get("sync_activity_state", False):
        results["activity_state"] = sync_activity_state(
            client,
            activity_info,
            config.get("status_map", {}),
        )

    return results


def main() -> int:
    token = os.environ.get("ADOBE_ACCESS_TOKEN")
    if not token:
        print("Missing ADOBE_ACCESS_TOKEN environment variable.", file=sys.stderr)
        return 1

    config = load_config()
    mcp_url = os.environ.get(
        "MCP_SERVER_URL",
        config.get("mcp_server_url", "https://targetmcp.adobe.io/mcp"),
    )

    activity_folders = discover_activity_folders(config)
    if not activity_folders:
        print("No activity folders found (looking for activity-info.json).")
        return 0

    print(
        "Discovered activity folders: "
        + ", ".join(str(path.relative_to(ROOT)).replace("\\", "/") for path in activity_folders)
    )

    client = McpClient(mcp_url, token)
    print("Connecting to Adobe Target MCP server...")
    client.initialize()

    all_results = [
        deploy_activity_folder(client, folder, config) for folder in activity_folders
    ]

    print("\nDeployment summary:")
    print(json.dumps(all_results, indent=2))
    print("Deployment to Adobe Target MCP completed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
