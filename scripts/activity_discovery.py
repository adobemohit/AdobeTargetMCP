"""Shared activity folder discovery logic (no third-party dependencies)."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

ROOT = Path(
    os.environ.get("GITOPS_ROOT", str(Path(__file__).resolve().parent.parent))
).resolve()
CONFIG_PATH = ROOT / "deploy.config.json"


def load_config() -> dict:
    with CONFIG_PATH.open(encoding="utf-8") as handle:
        return json.load(handle)


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


def path_exists_at_commit(commit: str, relative_path: str) -> bool:
    result = subprocess.run(
        ["git", "cat-file", "-e", f"{commit}:{relative_path}"],
        capture_output=True,
        text=True,
        cwd=ROOT,
        check=False,
    )
    return result.returncode == 0


def resolve_activity_folder(path: str, config: dict) -> Path | None:
    discovery = config.get("discovery", {})
    info_file = discovery.get("info_file", "activity-info.json")
    exclude_dirs = set(discovery.get("exclude_dirs", []))

    current = (ROOT / path).parent
    while current != ROOT and current != current.parent:
        if any(part in exclude_dirs for part in current.parts):
            return None
        if (current / info_file).exists():
            return current.resolve()
        current = current.parent
    return None


def get_newly_added_activity_folders(config: dict) -> list[Path]:
    return [
        change["folder"]
        for change in get_deployable_activity_changes(config)
        if change["mode"] == "create"
    ]


def get_updated_activity_folders(config: dict) -> list[Path]:
    return [
        change["folder"]
        for change in get_deployable_activity_changes(config)
        if change["mode"] == "update"
    ]


def get_deployable_activity_changes(config: dict) -> list[dict]:
    discovery = config.get("discovery", {})
    info_file = discovery.get("info_file", "activity-info.json")
    allowed_modes = set(discovery.get("deploy_modes", ["create", "update"]))

    all_folders = discover_activity_folders(config)
    before_sha = os.environ.get("GITHUB_BEFORE_SHA", "").strip()
    after_sha = os.environ.get("GITHUB_SHA", "").strip()

    if not after_sha:
        print("GITHUB_SHA not set. Deploying all discovered activity folders as create.")
        if "create" not in allowed_modes:
            return []
        return [{"folder": folder, "mode": "create"} for folder in all_folders]

    if not before_sha or before_sha == "0" * 40:
        print("First push detected. Treating all activity folders as create.")
        if "create" not in allowed_modes:
            return []
        return [{"folder": folder, "mode": "create"} for folder in all_folders]

    changed_files = get_git_changed_files(before_sha, after_sha)
    create_folders: set[Path] = set()
    update_folders: set[Path] = set()

    for status, path in changed_files:
        if not status.startswith("A"):
            continue
        if not path.endswith(info_file):
            continue
        folder = (ROOT / path).parent
        if folder.exists() and "create" in allowed_modes:
            create_folders.add(folder.resolve())

    for status, path in changed_files:
        folder = resolve_activity_folder(path, config)
        if not folder or folder in create_folders:
            continue
        if "update" not in allowed_modes:
            continue

        info_relative = str(folder.relative_to(ROOT)).replace("\\", "/") + f"/{info_file}"
        if path_exists_at_commit(before_sha, info_relative):
            update_folders.add(folder)

    changes: list[dict] = []
    for folder in sorted(create_folders):
        changes.append({"folder": folder, "mode": "create"})
    for folder in sorted(update_folders):
        changes.append({"folder": folder, "mode": "update"})
    return changes


def collect_changed_activity_files(config: dict) -> list[Path]:
    folders = {change["folder"] for change in get_deployable_activity_changes(config)}
    files: list[Path] = []

    for folder in sorted(folders):
        discovery = config.get("discovery", {})
        html_pattern = discovery.get("html_pattern", "*.html")
        info_file = discovery.get("info_file", "activity-info.json")

        files.extend(sorted(folder.glob(html_pattern)))
        info_path = folder / info_file
        if info_path.exists():
            files.append(info_path)

    return files
