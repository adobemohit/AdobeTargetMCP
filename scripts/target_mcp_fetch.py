#!/usr/bin/env python3
"""Fetch Adobe Target activities and offers via MCP for ATGitOps Helper."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from activity_discovery import load_config  # noqa: E402
from create_activity import to_snake_case  # noqa: E402
from deploy_to_target_mcp import (  # noqa: E402
    McpClient,
    extract_named_items,
    extract_tool_result,
    resolve_access_token,
)

ACTIVITY_GET_TOOLS = {
    "ab": "get_ab_activity",
    "xt": "get_xt_activity",
    "abt": "get_abt_activity",
    "ap": "get_abt_activity",
}


from mcp_config import apply_mcp_connection_settings  # noqa: E402


def connect_client() -> McpClient:
    config = apply_mcp_connection_settings(load_config())
    mcp_url = config.get("mcp_server_url", "https://targetmcp.adobe.io/mcp")
    token = resolve_access_token(config)
    client = McpClient(mcp_url, token)
    client.initialize()
    return client


def normalize_activity_type(activity_type: str | None) -> str:
    value = (activity_type or "xt").lower()
    if value in {"ab", "xt", "abt", "ap"}:
        return "abt" if value == "ap" else value
    return "xt"


def list_target_activities(
    *,
    limit: int = 200,
    name_contains: str | None = None,
    activity_type: str | None = None,
) -> list[dict]:
    client = connect_client()
    params: dict = {"limit": limit}
    if name_contains:
        params["name_contains"] = name_contains
    if activity_type:
        params["activity_type"] = normalize_activity_type(activity_type)

    response = client.call_tool("list_target_activities", params)
    result = extract_tool_result(response)
    return extract_named_items(result, "activities")


def get_activity_tool_name(activity_type: str) -> str:
    normalized = normalize_activity_type(activity_type)
    return ACTIVITY_GET_TOOLS[normalized]


def get_target_activity(activity_id: int, activity_type: str) -> dict:
    client = connect_client()
    tool_name = get_activity_tool_name(activity_type)
    response = client.call_tool(tool_name, {"activity_id": activity_id})
    return extract_tool_result(response)


def get_target_offer(offer_id: int) -> dict:
    client = connect_client()
    response = client.call_tool("get_target_offer", {"offer_id": offer_id})
    return extract_tool_result(response)


def _walk_values(node: object) -> list[object]:
    items: list[object] = []
    if isinstance(node, dict):
        items.extend(node.values())
        for value in node.values():
            items.extend(_walk_values(value))
    elif isinstance(node, list):
        for value in node:
            items.extend(_walk_values(value))
    return items


def extract_offer_candidates(activity_detail: dict) -> list[dict]:
    candidates: list[dict] = []
    seen: set[int] = set()

    def add_candidate(offer_id: object, name: str | None = None, variant: str | None = None) -> None:
        if offer_id is None:
            return
        try:
            numeric_id = int(offer_id)
        except (TypeError, ValueError):
            return
        if numeric_id in seen:
            return
        seen.add(numeric_id)
        candidates.append(
            {
                "offer_id": numeric_id,
                "offer_name": name,
                "variant": variant,
            }
        )

    for key in ("experiences", "options", "variants"):
        entries = activity_detail.get(key)
        if not isinstance(entries, list):
            continue
        for index, entry in enumerate(entries):
            if not isinstance(entry, dict):
                continue
            variant = (
                entry.get("name")
                or entry.get("variant")
                or entry.get("experienceLocalId")
                or f"variant_{chr(97 + index)}"
            )
            offer_id = (
                entry.get("offerId")
                or entry.get("offer_id")
                or entry.get("defaultOfferId")
            )
            offer_name = entry.get("offerName") or entry.get("offer_name")
            add_candidate(offer_id, offer_name, str(variant))

            offer = entry.get("offer")
            if isinstance(offer, dict):
                add_candidate(offer.get("id"), offer.get("name"), str(variant))

    for value in _walk_values(activity_detail):
        if not isinstance(value, dict):
            continue
        if "offerId" in value:
            add_candidate(value.get("offerId"), value.get("name"))
        if "offer_id" in value:
            add_candidate(value.get("offer_id"), value.get("name"))

    return candidates


def strip_name_prefix(name: str) -> str:
    return re.sub(r"^\[GitHub\]\[[^\]]+\]\s*", "", name).strip()


def build_folder_name(activity_name: str, activity_type: str) -> str:
    base = to_snake_case(strip_name_prefix(activity_name))
    suffix = "_ab_test" if normalize_activity_type(activity_type) == "ab" else "_xt_test"
    if not base.endswith("_xt_test") and not base.endswith("_ab_test"):
        base = f"{base}{suffix}"
    return base


def map_target_state(state: str | None) -> str:
    mapping = {
        "approved": "active",
        "deactivated": "inactive",
        "paused": "paused",
        "saved": "saved",
    }
    return mapping.get((state or "saved").lower(), "saved")


def map_repo_activity_type(activity_type: str) -> str:
    normalized = normalize_activity_type(activity_type)
    return {"ab": "AB", "xt": "XT", "abt": "ABT"}.get(normalized, "XT")


def extract_location(activity_detail: dict) -> str:
    for key in ("locations", "mboxes", "mboxNames"):
        value = activity_detail.get(key)
        if isinstance(value, list) and value:
            first = value[0]
            if isinstance(first, dict):
                return str(first.get("name") or first.get("mbox") or "home")
            return str(first)
        if isinstance(value, str) and value:
            return value
    return "home"


def import_activity_to_repo(
    activity_id: int,
    activity_type: str,
    *,
    offer_id: int | None = None,
    folder_name: str | None = None,
) -> dict:
    detail = get_target_activity(activity_id, activity_type)
    activity_name = detail.get("name") or f"Activity {activity_id}"
    clean_name = strip_name_prefix(activity_name)
    repo_type = map_repo_activity_type(activity_type)
    folder = folder_name or build_folder_name(clean_name, activity_type)
    folder_path = ROOT / folder

    if folder_path.exists():
        raise FileExistsError(f"Folder already exists: {folder}")

    offer_candidates = extract_offer_candidates(detail)
    selected_offer_id = offer_id or (offer_candidates[0]["offer_id"] if offer_candidates else None)
    if not selected_offer_id:
        raise ValueError(
            "Could not find an offer linked to this activity. Provide offer_id manually."
        )

    offer = get_target_offer(selected_offer_id)
    offer_content = offer.get("content") or "<section><h2>Imported offer</h2></section>"
    offer_name = offer.get("name") or f"{clean_name} - Variant A"

    variant_name = "variant_a"
    if offer_candidates:
        for candidate in offer_candidates:
            if candidate["offer_id"] == selected_offer_id and candidate.get("variant"):
                variant_name = str(candidate["variant"]).replace(" ", "_").lower()
                if not variant_name.startswith("variant_"):
                    variant_name = f"variant_{variant_name}"
                break

    html_file = f"{folder}_exp_a.html"
    template_info_path = ROOT / "_activity_template" / "activity-info.json"
    activity_info = json.loads(template_info_path.read_text(encoding="utf-8"))

    activity_info["activity_id"] = activity_id
    activity_info["activity_name"] = clean_name
    activity_info["activity_description"] = detail.get("description") or (
        f"Imported from Adobe Target activity {activity_id}"
    )
    activity_info["activity_status"] = map_target_state(detail.get("state"))
    activity_info["activity_type"] = repo_type
    activity_info["activity_location"] = extract_location(detail)
    if starts_at := detail.get("startsAt") or detail.get("starts_at"):
        activity_info["activity_start_date"] = str(starts_at)[:10]
    if ends_at := detail.get("endsAt") or detail.get("ends_at"):
        activity_info["activity_end_date"] = str(ends_at)[:10]

    activity_info["variants"] = [
        {
            "variant": variant_name,
            "html_file": html_file,
            "offer_name": strip_name_prefix(offer_name),
            "offer_id": selected_offer_id,
            "mode": "create_or_update",
        }
    ]

    folder_path.mkdir(parents=True)
    with (folder_path / "activity-info.json").open("w", encoding="utf-8") as handle:
        json.dump(activity_info, handle, indent=2)
        handle.write("\n")

    content = offer_content if offer_content.endswith("\n") else f"{offer_content}\n"
    with (folder_path / html_file).open("w", encoding="utf-8") as handle:
        handle.write(content)

    return {
        "folder": folder,
        "activity_id": activity_id,
        "offer_id": selected_offer_id,
        "activity_name": clean_name,
        "imported": True,
    }


def get_activity_bundle(activity_id: int, activity_type: str) -> dict:
    detail = get_target_activity(activity_id, activity_type)
    offers = extract_offer_candidates(detail)
    offer_details = []
    for offer in offers[:5]:
        try:
            offer_details.append(get_target_offer(offer["offer_id"]))
        except RuntimeError:
            offer_details.append({"id": offer["offer_id"], "content": None})

    return {
        "activity": detail,
        "offer_candidates": offers,
        "offers": offer_details,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch Adobe Target resources via MCP")
    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser("list")
    list_parser.add_argument("--limit", type=int, default=200)
    list_parser.add_argument("--name-contains")
    list_parser.add_argument("--activity-type")

    get_parser = subparsers.add_parser("get")
    get_parser.add_argument("activity_id", type=int)
    get_parser.add_argument("--activity-type", default="xt")

    import_parser = subparsers.add_parser("import")
    import_parser.add_argument("activity_id", type=int)
    import_parser.add_argument("--activity-type", default="xt")
    import_parser.add_argument("--offer-id", type=int)
    import_parser.add_argument("--folder-name")

    args = parser.parse_args()

    try:
        if args.command == "list":
            payload = list_target_activities(
                limit=args.limit,
                name_contains=args.name_contains,
                activity_type=args.activity_type,
            )
        elif args.command == "get":
            payload = get_activity_bundle(args.activity_id, args.activity_type)
        else:
            payload = import_activity_to_repo(
                args.activity_id,
                args.activity_type,
                offer_id=args.offer_id,
                folder_name=args.folder_name,
            )
    except Exception as error:
        print(json.dumps({"error": str(error)}), file=sys.stderr)
        return 1

    print(json.dumps(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
