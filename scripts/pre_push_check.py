#!/usr/bin/env python3
"""Local pre-push check: block push to main without a new activity folder."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from deploy_to_target_mcp import get_newly_added_activity_folders, load_config  # noqa: E402


def get_current_branch() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True,
        text=True,
        cwd=ROOT,
        check=True,
    )
    return result.stdout.strip()


def main() -> int:
    branch = get_current_branch()
    if branch != "main":
        print(f"On branch '{branch}'. Skipping new-activity-folder check.")
        return 0

    before_sha = subprocess.run(
        ["git", "rev-parse", "HEAD~1"],
        capture_output=True,
        text=True,
        cwd=ROOT,
        check=False,
    )
    after_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        cwd=ROOT,
        check=True,
    )

    import os

    if before_sha.returncode == 0:
        os.environ["GITHUB_BEFORE_SHA"] = before_sha.stdout.strip()
    else:
        os.environ["GITHUB_BEFORE_SHA"] = "0" * 40
    os.environ["GITHUB_SHA"] = after_sha.stdout.strip()

    config = load_config()
    folders = get_newly_added_activity_folders(config)
    if folders:
        print("Local check passed. New activity folder detected.")
        return 0

    print(
        "ERROR: No newly created activity folder in this commit.",
        file=sys.stderr,
    )
    print(
        "Add a new folder with activity-info.json before pushing to main.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
