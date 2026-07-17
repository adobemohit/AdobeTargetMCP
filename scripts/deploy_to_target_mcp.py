#!/usr/bin/env python3
"""Deploy activity folders to Adobe Target via MCP on merge to main."""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import httpx

from activity_discovery import (
    ROOT,
    get_deployable_activity_changes,
    load_config,
)
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

    print("Fetching Adobe access token using client credentials...", file=sys.stderr)
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
    print(f"Adobe access token fetched successfully (expires_in={expires_in}).", file=sys.stderr)
    return access_token


def resolve_access_token(config: dict, force_refresh: bool = False) -> str:
    if force_refresh:
        return fetch_access_token_from_client_credentials(config)

    access_token = os.environ.get("ADOBE_ACCESS_TOKEN", "").strip()
    if access_token:
        print("Using ADOBE_ACCESS_TOKEN from environment.", file=sys.stderr)
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


class DeployValidationError(RuntimeError):
    """Raised when deploy pre-checks fail."""


def extract_named_items(result: dict, collection_key: str) -> list[dict]:
    if isinstance(result, list):
        return result

    if collection_key in result and isinstance(result[collection_key], list):
        return result[collection_key]

    for value in result.values():
        if isinstance(value, list) and value and isinstance(value[0], dict):
            if "name" in value[0]:
                return value

    return []


def find_existing_activity_by_name(client: McpClient, activity_name: str) -> dict | None:
    response = client.call_tool("list_target_activities", {"limit": 200})
    result = extract_tool_result(response)
    activities = extract_named_items(result, "activities")

    for activity in activities:
        if activity.get("name") == activity_name:
            return activity

    return None


def find_existing_offer_by_name(client: McpClient, offer_name: str) -> dict | None:
    response = client.call_tool(
        "list_target_offers",
        {"name": offer_name, "type": "content", "limit": 200},
    )
    result = extract_tool_result(response)
    offers = extract_named_items(result, "offers")

    for offer in offers:
        if offer.get("name") == offer_name:
            return offer

    return None


ACTIVITY_GET_TOOLS = {
    "ab": "get_ab_activity",
    "xt": "get_xt_activity",
    "abt": "get_abt_activity",
}


def extract_offer_ids_from_activity_detail(detail: dict) -> list[int]:
    offer_ids: list[int] = []

    def add_offer_id(value: object) -> None:
        if value is None:
            return
        try:
            numeric = int(value)
        except (TypeError, ValueError):
            return
        if numeric > 0 and numeric not in offer_ids:
            offer_ids.append(numeric)

    for key in ("experiences", "options", "variants"):
        entries = detail.get(key)
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            add_offer_id(
                entry.get("offerId")
                or entry.get("offer_id")
                or entry.get("defaultOfferId")
            )
            offer = entry.get("offer")
            if isinstance(offer, dict):
                add_offer_id(offer.get("id"))

    return offer_ids


def resolve_missing_target_ids(
    client: McpClient,
    activity_info: dict,
    offers: list[dict],
    config: dict,
) -> None:
    type_map = config.get("activity_type_map", {})

    if not activity_info.get("activity_id"):
        activity_name = str(activity_info.get("activity_name", ""))
        existing = find_existing_activity_by_name(client, activity_name)
        if not existing:
            search_term = activity_name.split("]")[-1].strip() if "]" in activity_name else activity_name
            response = client.call_tool(
                "list_target_activities",
                {"limit": 200, "name_contains": search_term[:60]},
            )
            result = extract_tool_result(response)
            activities = extract_named_items(result, "activities")
            lowered = search_term.lower()
            for activity in activities:
                target_name = str(activity.get("name", "")).lower()
                if lowered in target_name or target_name in lowered:
                    existing = activity
                    break

        if existing and existing.get("id"):
            activity_info["activity_id"] = int(existing["id"])
            print(
                "INFO: Resolved activity_id="
                f"{activity_info['activity_id']} from Target for update deploy."
            )

    activity_id = activity_info.get("activity_id")
    offer_ids_from_activity: list[int] = []
    if activity_id:
        activity_type = map_activity_type(
            activity_info.get("activity_type", "XT"),
            type_map,
        )
        get_tool = ACTIVITY_GET_TOOLS.get(activity_type, "get_xt_activity")
        try:
            response = client.call_tool(get_tool, {"activity_id": activity_id})
            detail = extract_tool_result(response)
            offer_ids_from_activity = extract_offer_ids_from_activity_detail(detail)
        except Exception as error:
            print(f"WARNING: Could not fetch activity detail for offer IDs: {error}")

    for offer in offers:
        if offer.get("offer_id"):
            continue

        offer_name = str(offer.get("offer_name", ""))
        if offer_name:
            existing_offer = find_existing_offer_by_name(client, offer_name)
            if existing_offer and existing_offer.get("id"):
                offer["offer_id"] = int(existing_offer["id"])
                print(
                    f"INFO: Resolved offer_id={offer['offer_id']} "
                    f"for '{offer_name}' from Target."
                )
                continue

        if offer_ids_from_activity:
            offer["offer_id"] = offer_ids_from_activity[0]
            print(
                f"INFO: Resolved offer_id={offer['offer_id']} "
                "from get_activity response."
            )


def assert_activity_name_available(
    client: McpClient, activity_name: str, config: dict
) -> None:
    if not config.get("validation", {}).get("check_duplicate_activity_name", True):
        return

    existing = find_existing_activity_by_name(client, activity_name)
    if existing:
        raise DeployValidationError(
            f"Activity '{activity_name}' already exists in Adobe Target "
            f"(id={existing.get('id')}). Use a different activity_name or set activity_id."
        )


def assert_offer_name_available(
    client: McpClient, offer_name: str, config: dict, offer_id: int | None
) -> None:
    if offer_id:
        return

    if not config.get("validation", {}).get("check_duplicate_offer_name", True):
        return

    existing = find_existing_offer_by_name(client, offer_name)
    if existing:
        raise DeployValidationError(
            f"Offer '{offer_name}' already exists in Adobe Target "
            f"(id={existing.get('id')}). Use a different offer_name or set offer_id."
        )


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


def get_deploy_username(config: dict) -> str:
    return (
        os.environ.get("DEPLOY_USERNAME", "").strip()
        or os.environ.get("GITHUB_ACTOR", "").strip()
        or config.get("deploy_username", "").strip()
    )


def get_name_prefix(config: dict) -> str:
    base_prefix = config.get("deploy_name_prefix", "[GitHub]")
    username = get_deploy_username(config)
    if username:
        return f"{base_prefix}[{username}]"
    return base_prefix


def with_name_prefix(name: str, prefix: str) -> str:
    cleaned = name.strip()
    if not prefix:
        return cleaned
    if cleaned.startswith(prefix):
        return cleaned
    return f"{prefix} {cleaned}"


def apply_deploy_name_prefix(
    activity_info: dict, offers: list[dict], config: dict
) -> tuple[dict, list[dict]]:
    prefix = get_name_prefix(config)
    prefixed_activity = dict(activity_info)
    prefixed_activity["activity_name"] = with_name_prefix(
        activity_info.get("activity_name", "Activity"), prefix
    )

    prefixed_offers = []
    for offer in offers:
        prefixed_offer = dict(offer)
        prefixed_offer["offer_name"] = with_name_prefix(
            offer.get("offer_name", "Offer"), prefix
        )
        prefixed_offers.append(prefixed_offer)

    return prefixed_activity, prefixed_offers


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
    activity_name = activity_info["activity_name"]

    params = {
        "name": activity_name,
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

    print(f"Creating activity in Adobe Target via {tool_name}...")
    response = client.call_tool(tool_name, params)
    result = extract_tool_result(response)

    activity_id = result.get("id")
    if activity_id:
        print(
            f"SUCCESS: Activity '{activity_name}' created in Adobe Target "
            f"(id={activity_id}, type={activity_type.upper()})."
        )
    else:
        print(
            f"WARNING: Activity '{activity_name}' create call completed, "
            "but no activity id was returned by Adobe Target."
        )

    return result


def deploy_offer(client: McpClient, offer_config: dict, html_content: str) -> dict:
    offer_name = offer_config["offer_name"]
    offer_id = offer_config.get("offer_id")
    mode = offer_config.get("mode", "create_or_update")

    if mode == "update" or (mode == "create_or_update" and offer_id):
        if not offer_id:
            raise ValueError(f"offer_id is required to update offer '{offer_name}'")

        print(f"Updating offer in Adobe Target (id={offer_id})...")
        response = client.call_tool(
            "update_target_offer",
            {
                "offer_id": offer_id,
                "name": offer_name,
                "content": html_content,
            },
        )
        result = extract_tool_result(response)
        print(
            f"SUCCESS: Offer '{offer_name}' updated in Adobe Target (id={offer_id})."
        )
        return result

    print(f"Creating offer in Adobe Target...")
    response = client.call_tool(
        "create_target_offer",
        {"name": offer_name, "content": html_content},
    )
    result = extract_tool_result(response)
    created_offer_id = result.get("id")
    if created_offer_id:
        print(
            f"SUCCESS: Offer '{offer_name}' created in Adobe Target "
            f"(id={created_offer_id})."
        )
    else:
        print(
            f"WARNING: Offer '{offer_name}' create call completed, "
            "but no offer id was returned by Adobe Target."
        )
    return result


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


def validate_update_requirements(
    activity_info: dict, offers: list[dict], folder: Path
) -> None:
    activity_id = activity_info.get("activity_id")
    if not activity_id:
        raise DeployValidationError(
            f"Update deploy for '{folder.name}' requires a non-zero activity_id "
            "in activity-info.json."
        )

    for offer in offers:
        if not offer.get("offer_id"):
            raise DeployValidationError(
                f"Update deploy for '{folder.name}' requires offer_id for "
                f"'{offer.get('html_file', 'offer')}' in activity-info.json."
            )


def deploy_activity_folder(
    client: McpClient, folder: Path, config: dict, *, deploy_mode: str
) -> dict:
    discovery = config.get("discovery", {})
    info_file = discovery.get("info_file", "activity-info.json")
    html_pattern = discovery.get("html_pattern", "*.html")

    activity_info = load_activity_info(folder, info_file)
    actions = resolve_actions(activity_info, config)
    offers = resolve_offers(folder, activity_info, html_pattern)
    activity_info, offers = apply_deploy_name_prefix(activity_info, offers, config)

    results = {
        "folder": str(folder.relative_to(ROOT)).replace("\\", "/"),
        "activity_name": activity_info.get("activity_name"),
        "deploy_mode": deploy_mode,
        "activity_create_result": None,
        "activity_update_result": None,
        "offers": [],
        "activity_state": None,
    }

    print(f"\nDeploying activity folder ({deploy_mode}): {results['folder']}")

    if deploy_mode == "update":
        resolve_missing_target_ids(client, activity_info, offers, config)
        validate_update_requirements(activity_info, offers, folder)
        for offer in offers:
            offer["mode"] = "update"
        print(
            f"INFO: Updating existing Adobe Target activity "
            f"'{activity_info['activity_name']}' (id={activity_info['activity_id']})."
        )

    should_create_activity = (
        deploy_mode == "create"
        and actions.get("create_activity_if_missing", False)
        and not activity_info.get("activity_id")
    )

    if should_create_activity:
        assert_activity_name_available(
            client, activity_info["activity_name"], config
        )
        activity_result = create_target_activity(
            client,
            activity_info,
            config.get("activity_type_map", {}),
        )
        results["activity_create_result"] = activity_result
        created_id = activity_result.get("id")
        if created_id:
            activity_info["activity_id"] = created_id
    elif activity_info.get("activity_id"):
        print(
            f"INFO: Using existing Adobe Target activity "
            f"'{activity_info['activity_name']}' (id={activity_info['activity_id']})."
        )
        results["activity_update_result"] = {
            "activity_id": activity_info.get("activity_id"),
            "message": "Existing activity reused for offer update",
        }

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

        assert_offer_name_available(
            client,
            offer_config["offer_name"],
            config,
            offer_config.get("offer_id"),
        )
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


def print_deploy_summary(all_results: list[dict]) -> None:
    print("\n=== Adobe Target Deploy Summary ===")

    for result in all_results:
        folder = result.get("folder", "unknown")
        activity_name = result.get("activity_name", "unknown")
        deploy_mode = result.get("deploy_mode", "unknown")
        print(f"\nFolder: {folder}")
        print(f"Activity: {activity_name}")
        print(f"Mode: {deploy_mode}")

        activity_create = result.get("activity_create_result") or {}
        activity_id = activity_create.get("id")
        if activity_id:
            print(
                f"  - Activity created in Adobe Target: "
                f"'{activity_name}' (id={activity_id})"
            )
        elif deploy_mode == "update":
            update_result = result.get("activity_update_result") or {}
            existing_id = update_result.get("activity_id")
            print(
                f"  - Activity updated in Adobe Target: "
                f"'{activity_name}' (id={existing_id})"
            )
        else:
            print("  - Activity not created in this deploy run.")

        offers = result.get("offers", [])
        if not offers:
            print("  - No offers deployed.")
            continue

        for offer in offers:
            offer_result = offer.get("offer_result") or {}
            offer_id = offer_result.get("id")
            html_file = offer.get("html_file", "unknown")
            if offer_id:
                action = "updated" if deploy_mode == "update" else "deployed"
                print(
                    f"  - Offer {action} from '{html_file}' "
                    f"in Adobe Target (id={offer_id})"
                )
            else:
                print(f"  - Offer processed from '{html_file}'.")

    print("\nDeployment to Adobe Target MCP completed successfully.")


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

    activity_changes = get_deployable_activity_changes(config)
    if not activity_changes:
        print(
            "ERROR: No activity changes found in this merge.",
            file=sys.stderr,
        )
        print(
            "Add a new activity folder or update an existing activity folder.",
            file=sys.stderr,
        )
        return 1

    print(
        "Activity changes to deploy: "
        + ", ".join(
            f"{change['folder'].relative_to(ROOT).as_posix()} ({change['mode']})"
            for change in activity_changes
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

    all_results = []
    try:
        for change in activity_changes:
            all_results.append(
                deploy_activity_folder(
                    client,
                    change["folder"],
                    config,
                    deploy_mode=change["mode"],
                )
            )
    except DeployValidationError as error:
        print(f"Deploy validation failed: {error}", file=sys.stderr)
        return 1

    print_deploy_summary(all_results)
    print("\nDeployment details:")
    print(json.dumps(all_results, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
