#!/usr/bin/env python3
"""Strip Cursor attribution lines from a git commit message file."""

from __future__ import annotations

import sys
from pathlib import Path

BLOCKED_MARKERS = (
    "cursoragent@cursor.com",
    "Co-authored-by: Cursor",
    "Made-with: Cursor",
)


def main() -> int:
    if len(sys.argv) != 2:
        print("Usage: strip_cursor_attribution.py <commit-msg-file>", file=sys.stderr)
        return 1

    path = Path(sys.argv[1])
    if not path.exists():
        return 0

    text = path.read_text(encoding="utf-8")
    cleaned = "".join(
        line for line in text.splitlines(True) if not any(marker in line for marker in BLOCKED_MARKERS)
    )
    path.write_text(cleaned, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
