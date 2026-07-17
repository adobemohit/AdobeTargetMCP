#!/usr/bin/env python3
"""Local pre-push check: block push to main without activity changes."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from activity_discovery import get_deployable_activity_changes, load_config  # noqa: E402


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
        print(f"On branch '{branch}'. Skipping activity change check.")
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

    if before_sha.returncode == 0:
        os.environ["GITHUB_BEFORE_SHA"] = before_sha.stdout.strip()
    else:
        os.environ["GITHUB_BEFORE_SHA"] = "0" * 40
    os.environ["GITHUB_SHA"] = after_sha.stdout.strip()

    config = load_config()
    changes = get_deployable_activity_changes(config)
    if changes:
        labels = ", ".join(
            f"{change['folder'].relative_to(ROOT).as_posix()} ({change['mode']})"
            for change in changes
        )
        print(f"Local check passed. Activity changes detected: {labels}")
        return 0

    print(
        "ERROR: No activity changes detected in this commit.",
        file=sys.stderr,
    )
    print(
        "Add a new activity folder or update an existing one before pushing to main.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
