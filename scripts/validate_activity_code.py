#!/usr/bin/env python3
"""Validate HTML, CSS, and JavaScript in newly added activity folders."""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import html5lib
import tinycss2

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from activity_discovery import (  # noqa: E402
    collect_changed_activity_files,
    load_config,
)

SCRIPT_TAG_RE = re.compile(
    r"<script\b[^>]*>(.*?)</script>", re.IGNORECASE | re.DOTALL
)
STYLE_TAG_RE = re.compile(
    r"<style\b[^>]*>(.*?)</style>", re.IGNORECASE | re.DOTALL
)


def wrap_html_fragment(content: str) -> str:
    stripped = content.strip()
    if stripped.lower().startswith("<!doctype") or stripped.lower().startswith("<html"):
        return stripped
    return (
        "<!DOCTYPE html><html><head><meta charset='utf-8'></head><body>"
        f"{stripped}</body></html>"
    )


def collect_files_for_validation(config: dict) -> list[Path]:
    return collect_changed_activity_files(config)


def validate_html_structure(path: Path, content: str) -> list[str]:
    errors: list[str] = []
    wrapped = wrap_html_fragment(content)

    try:
        html5lib.parse(wrapped, namespaceHTMLElements=False)
    except Exception as error:
        errors.append(f"{path}: invalid HTML structure ({error})")

    if not content.strip():
        errors.append(f"{path}: HTML file is empty")

    return errors


def validate_css(path: Path, content: str) -> list[str]:
    errors: list[str] = []

    for index, style_match in enumerate(STYLE_TAG_RE.finditer(content), start=1):
        css = style_match.group(1).strip()
        if not css:
            continue

        try:
            tokens = tinycss2.parse_stylesheet(css, skip_comments=True, skip_whitespace=True)
        except Exception as error:
            errors.append(f"{path}: CSS syntax error in <style> block {index} ({error})")
            continue

        for token in tokens:
            if isinstance(token, tinycss2.ast.ParseError):
                errors.append(
                    f"{path}: CSS parse error in <style> block {index} ({token.message})"
                )

    return errors


def validate_javascript(path: Path, content: str) -> list[str]:
    errors: list[str] = []

    for index, script_match in enumerate(SCRIPT_TAG_RE.finditer(content), start=1):
        script = script_match.group(1).strip()
        if not script:
            continue

        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as handle:
            handle.write(script)
            temp_path = Path(handle.name)

        try:
            result = subprocess.run(
                ["node", "--check", str(temp_path)],
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode != 0:
                message = (result.stderr or result.stdout).strip() or "JavaScript syntax error"
                errors.append(
                    f"{path}: JavaScript error in <script> block {index} ({message})"
                )
                continue
        except FileNotFoundError:
            errors.append(f"{path}: Node.js is required to validate JavaScript")
            return errors
        finally:
            temp_path.unlink(missing_ok=True)

    return errors


def validate_json_file(path: Path) -> list[str]:
    errors: list[str] = []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        return [f"{path}: invalid JSON ({error})"]

    required_fields = ["activity_name", "activity_type", "variants"]
    for field in required_fields:
        if field not in payload:
            errors.append(f"{path}: missing required field '{field}'")

    return errors


def validate_prettier(path: Path) -> list[str]:
    result = subprocess.run(
        ["npx", "prettier", "--check", str(path)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0:
        return []

    message = (result.stdout or result.stderr).strip()
    return [f"{path}: formatting check failed ({message})"]


def validate_file(path: Path) -> list[str]:
    errors: list[str] = []

    if path.suffix == ".json":
        errors.extend(validate_json_file(path))
        return errors

    if path.suffix != ".html":
        return errors

    content = path.read_text(encoding="utf-8")
    errors.extend(validate_html_structure(path, content))
    errors.extend(validate_css(path, content))
    errors.extend(validate_javascript(path, content))
    errors.extend(validate_prettier(path))
    return errors


def main() -> int:
    config = load_config()
    files = collect_files_for_validation(config)

    if not files:
        print("No files to validate in newly added activity folders.")
        return 0

    print("Validating files:")
    for path in files:
        print(f"  - {path.relative_to(ROOT)}")

    all_errors: list[str] = []
    for path in files:
        all_errors.extend(validate_file(path))

    if all_errors:
        print("\nValidation failed:", file=sys.stderr)
        for error in all_errors:
            print(f"  - {error}", file=sys.stderr)
        return 1

    print("HTML, CSS, JavaScript validation passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
