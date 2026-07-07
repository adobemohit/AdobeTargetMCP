#!/usr/bin/env python3
"""Fail CI when a merge does not include a newly created activity folder."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from deploy_to_target_mcp import (  # noqa: E402
    get_newly_added_activity_folders,
    load_config,
)


def format_folder(path: Path) -> str:
    return str(path.relative_to(ROOT)).replace("\\", "/")


def main() -> int:
    config = load_config()
    activity_folders = get_newly_added_activity_folders(config)

    if activity_folders:
        folder_list = ", ".join(format_folder(folder) for folder in activity_folders)
        print(f"Validation passed. New activity folders found: {folder_list}")
        return 0

    print(
        "ERROR: No newly created activity folders in this merge.",
        file=sys.stderr,
    )
    print(
        "To merge to main, add a new activity folder with activity-info.json.",
        file=sys.stderr,
    )
    print(
        "Example:\n"
        "  my_new_activity/\n"
        "    activity-info.json\n"
        "    my_new_activity_exp_a.html",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
