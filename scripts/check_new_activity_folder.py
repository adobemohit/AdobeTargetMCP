#!/usr/bin/env python3
"""Fail CI when a merge does not include a new or updated activity folder."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from activity_discovery import (  # noqa: E402
    ROOT as DISCOVERY_ROOT,
    get_deployable_activity_changes,
    load_config,
)


def format_change(change: dict) -> str:
    folder = str(change["folder"].relative_to(DISCOVERY_ROOT)).replace("\\", "/")
    return f"{folder} ({change['mode']})"


def main() -> int:
    config = load_config()
    changes = get_deployable_activity_changes(config)

    if changes:
        change_list = ", ".join(format_change(change) for change in changes)
        print(f"Validation passed. Activity changes found: {change_list}")
        return 0

    print(
        "ERROR: No activity changes found in this merge.",
        file=sys.stderr,
    )
    print(
        "To merge to main, either:",
        file=sys.stderr,
    )
    print(
        "  1. Add a new activity folder with activity-info.json, or",
        file=sys.stderr,
    )
    print(
        "  2. Update an existing activity folder (HTML or activity-info.json).",
        file=sys.stderr,
    )
    print(
        "For updates, set activity_id and offer_id in activity-info.json.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
