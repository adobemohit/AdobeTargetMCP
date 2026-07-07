#!/usr/bin/env python3
"""Deploy activity folders to Adobe Target via MCP on merge to main."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "deploy.config.json"
DEFAULT_ADOBE_SCOPES = (
    "openid,AdobeID,target_sdk,additional_info.roles,"
    "read_organizations,additional_info.projectedProductContext"
)


def fetch_access_token_from_client_credentials(config: dict) -> str:
    client_id = os.environ.get("ADOBE_CLIENT_ID", "").strip()
    client_secret = os.environ.get("ADOBE_CLIENT_SECRET", "").strip()

    if not client_id or not client_secret:
        raise ValueError(
            "ADOBE_CLIENT_ID and ADOBE_CLIENT_SECRET are required to refresh token"
        )

    auth_config = config.get("auth", {})
    token_url = auth_config.get(
        "ims_token_url", "https://ims-na1.adobelogin.com/ims/token/v3"
    )
    scopes = auth_config.get("scopes", DEFAULT_ADOBE_SCOPES)

    print("Fetching Adobe access token using client credentials...")
    with httpx.Client(timeout=60.0) as client:
        response = client.post(
            token_url,
            data={
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": client_secret,
                "scope": scopes,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        response.raise_for_status()
        payload = response.json()

    access_token = payload.get("access_token")
    if not access_token:
        raise RuntimeError("Adobe IMS response did not include access_token")

    expires_in = payload.get("expires_in", "unknown")
    print(f"Adobe access token fetched successfully (expires_in={expires_in}).")
    return access_token


def resolve_access_token(config: dict, force_refresh: bool = False) -> str:
    if force_refresh:
        return fetch_access_token_from_client_credentials(config)

    access_token = os.environ.get("ADOBE_ACCESS_TOKEN", "").strip()
    if access_token:
        print("Using ADOBE_ACCESS_TOKEN from GitHub Secrets.")
        return access_token

    return fetch_access_token_from_client_credentials(config)


class McpClient:
    def __init__(self, url: str, token: str) -> None:
        self.url = url
        self.token = token
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        self.session_id: str | None = None
        self.request_id = 0

    def set_token(self, token: str) -> None:
        self.token = token
        self.headers["Authorization"] = f"Bearer {token}"
        self.session_id = None
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


def get_git_changed_files(before_sha: str, after_sha: str) -> list[tuple[str, str]]:
    result = subprocess.run(
        ["git", "diff", "--name-status", before_sha, after_sha],
        capture_output=True,
        text=True,
        cwd=ROOT,
        check=True,
    )

    changes: list[tuple[str, str]] = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        status = parts[0]
        path = parts[-1]
        changes.append((status, path.replace("\\", "/")))
    return changes


def get_newly_added_activity_folders(config: dict) -> list[Path]:
    discovery = config.get("discovery", {})
    info_file = discovery.get("info_file", "activity-info.json")
    deploy_only_new = discovery.get("deploy_only_new_folders", True)

    all_folders = discover_activity_folders(config)
    if not deploy_only_new:
        return all_folders

    before_sha = os.environ.get("GITHUB_BEFORE_SHA", "").strip()
    after_sha = os.environ.get("GITHUB_SHA", "").strip()

    if not after_sha:
        print("GITHUB_SHA not set. Deploying all discovered activity folders.")
        return all_folders

    if not before_sha or before_sha == "0" * 40:
        print("First push detected. Treating all activity folders as new.")
        return all_folders

    changed_files = get_git_changed_files(before_sha, after_sha)
    new_folders: set[Path] = set()

    for status, path in changed_files:
        if not status.startswith("A"):
            continue
        if not path.endswith(info_file):
            continue
        folder = (ROOT / path).parent
        if folder.exists():
            new_folders.add(folder.resolve())

    return sorted(new_folders)


def build_activity_locations(activity_info: dict) -> list[dict]:
    location = activity_info.get("activity_location", "default")
    return [{"name": location}]


def build_activity_experiences(activity_info: dict) -> list[dict]:
    experiences: list[dict] = []

    for variant in activity_info.get("variants", []):
        experiences.append({"name": variant.get("variant", "Experience A")})

    if experiences:
        return experiences

    variant_name = activity_info.get("activity_variant", "Experience A")
    return [{"name": variant_name}]


def create_target_activity(
    client: McpClient, activity_info: dict, type_map: dict
) -> dict:
    activity_type = map_activity_type(activity_info.get("activity_type", "XT"), type_map)
    tool_name = f"create_{activity_type}_activity"

    params = {
        "name": activity_info["activity_name"],
        "state": "saved",
        "experiences": build_activity_experiences(activity_info),
        "locations": build_activity_locations(activity_info),
    }

    if starts_at := activity_info.get("activity_start_date"):
        params["starts_at"] = starts_at
    if ends_at := activity_info.get("activity_end_date"):
        params["ends_at"] = ends_at
    if description := activity_info.get("activity_description"):
        params["description"] = description

    print(f"Creating Target activity via {tool_name}...")
    response = client.call_tool(tool_name, params)
    return extract_tool_result(response)


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
    client: McpClient, folder: Path, config: dict, *, is_new_folder: bool
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
        "is_new_folder": is_new_folder,
        "activity_create_result": None,
        "offers": [],
        "activity_state": None,
    }

    print(f"\nDeploying new activity folder: {results['folder']}")

    should_create_activity = (
        is_new_folder
        and actions.get("create_activity_if_missing", False)
        and not activity_info.get("activity_id")
    )

    if should_create_activity:
        activity_result = create_target_activity(
            client,
            activity_info,
            config.get("activity_type_map", {}),
        )
        results["activity_create_result"] = activity_result
        created_id = activity_result.get("id")
        if created_id:
            activity_info["activity_id"] = created_id
            print(f"Created Target activity with id={created_id}")

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
    config = load_config()
    mcp_url = os.environ.get(
        "MCP_SERVER_URL",
        config.get("mcp_server_url", "https://targetmcp.adobe.io/mcp"),
    )

    try:
        token = resolve_access_token(config)
    except (ValueError, RuntimeError, httpx.HTTPError) as error:
        print(f"Failed to resolve Adobe access token: {error}", file=sys.stderr)
        return 1

    activity_folders = get_newly_added_activity_folders(config)
    if not activity_folders:
        print(
            "ERROR: No newly created activity folders in this merge.",
            file=sys.stderr,
        )
        print(
            "Add a new activity folder with activity-info.json before merging to main.",
            file=sys.stderr,
        )
        return 1

    print(
        "New activity folders to deploy: "
        + ", ".join(
            str(path.relative_to(ROOT)).replace("\\", "/")
            for path in activity_folders
        )
    )

    client = McpClient(mcp_url, token)
    print("Connecting to Adobe Target MCP server...")

    try:
        client.initialize()
    except httpx.HTTPStatusError as error:
        if error.response.status_code != 401:
            raise

        print("Access token rejected (401). Refreshing token from client credentials...")
        try:
            refreshed_token = resolve_access_token(config, force_refresh=True)
            client.set_token(refreshed_token)
            client.initialize()
        except (ValueError, RuntimeError, httpx.HTTPError) as refresh_error:
            print(
                "Token refresh failed. Update ADOBE_ACCESS_TOKEN in GitHub Secrets.",
                file=sys.stderr,
            )
            print(refresh_error, file=sys.stderr)
            return 1

    all_results = [
        deploy_activity_folder(client, folder, config, is_new_folder=True)
        for folder in activity_folders
    ]

    print("\nDeployment summary:")
    print(json.dumps(all_results, indent=2))
    print("Deployment to Adobe Target MCP completed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
