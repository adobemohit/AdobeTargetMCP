#!/usr/bin/env python3
"""Create a new activity folder using the Prettier-formatted template."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_DIR = ROOT / "_activity_template"


def to_snake_case(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9]+", "_", value.strip().lower())
    return cleaned.strip("_")


def create_activity(folder_name: str, activity_name: str) -> Path:
    if not TEMPLATE_DIR.exists():
        raise FileNotFoundError("Template folder '_activity_template' not found.")

    target_dir = ROOT / folder_name
    if target_dir.exists():
        raise FileExistsError(f"Activity folder already exists: {folder_name}")

    html_file = f"{folder_name}_exp_a.html"
    offer_name = f"{activity_name} - Variant A"

    with (TEMPLATE_DIR / "activity-info.json").open(encoding="utf-8") as handle:
        activity_info = json.load(handle)

    activity_info["activity_name"] = activity_name
    activity_info["activity_description"] = f"Activity created from GitHub template: {activity_name}"
    activity_info["variants"][0]["variant"] = "variant_a"
    activity_info["variants"][0]["html_file"] = html_file
    activity_info["variants"][0]["offer_name"] = offer_name

    target_dir.mkdir(parents=True)
    with (target_dir / "activity-info.json").open("w", encoding="utf-8") as handle:
        json.dump(activity_info, handle, indent=2)
        handle.write("\n")

    html_template = (TEMPLATE_DIR / "your_activity_name_exp_a.html").read_text(encoding="utf-8")
    html_content = html_template.replace("Your Activity Heading", activity_name).replace(
        "your-activity-name", to_snake_case(folder_name).replace("_", "-")
    )
    with (target_dir / html_file).open("w", encoding="utf-8") as handle:
        handle.write(html_content)
        if not html_content.endswith("\n"):
            handle.write("\n")

    return target_dir


def main() -> int:
    if len(sys.argv) < 3:
        print(
            "Usage: python scripts/create_activity.py <folder_name> <activity_name>",
            file=sys.stderr,
        )
        print(
            "Example: python scripts/create_activity.py summer_promo_xt_test \"Summer Promo XT Test\"",
            file=sys.stderr,
        )
        return 1

    folder_name = sys.argv[1]
    activity_name = sys.argv[2]

    try:
        created = create_activity(folder_name, activity_name)
    except (FileExistsError, FileNotFoundError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1

    print(f"Created activity folder: {created.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
