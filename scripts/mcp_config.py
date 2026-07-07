"""Load Adobe Target MCP connection settings from mcp.json or environment."""

from __future__ import annotations

import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MCP_CONFIG_PATH = ROOT / "at-git-helper" / ".atgitops" / "mcp.json"
DEFAULT_SERVER_NAME = "remote-server"


def _strip_bearer(value: str) -> str:
    token = value.strip()
    if token.lower().startswith("bearer "):
        return token[7:].strip()
    return token


def load_mcp_json_file() -> dict | None:
    if not MCP_CONFIG_PATH.exists():
        return None

    try:
        return json.loads(MCP_CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def parse_mcp_servers_config(raw: dict | None) -> dict:
    if not raw:
        return {}

    servers = raw.get("mcpServers")
    if not isinstance(servers, dict) or not servers:
        return {}

    server_name = DEFAULT_SERVER_NAME if DEFAULT_SERVER_NAME in servers else next(iter(servers))
    server = servers.get(server_name, {})
    if not isinstance(server, dict):
        return {"server_name": server_name}

    headers = server.get("headers") if isinstance(server.get("headers"), dict) else {}
    auth = server.get("auth") if isinstance(server.get("auth"), dict) else {}

    authorization = str(headers.get("Authorization", "")).strip()
    access_token = _strip_bearer(authorization)

    return {
        "server_name": server_name,
        "server_url": str(server.get("url", "")).strip(),
        "access_token": access_token,
        "client_id": str(auth.get("CLIENT_ID", "")).strip(),
        "client_secret": str(auth.get("CLIENT_SECRET", "")).strip(),
    }


def apply_mcp_connection_settings(base_config: dict | None = None) -> dict:
    """Merge deploy.config.json with mcp.json / env vars for MCP connections."""
    config = dict(base_config or {})

    parsed = parse_mcp_servers_config(load_mcp_json_file())

    mcp_url = (
        os.environ.get("MCP_SERVER_URL", "").strip()
        or parsed.get("server_url", "")
        or config.get("mcp_server_url", "https://targetmcp.adobe.io/mcp")
    )
    access_token = os.environ.get("ADOBE_ACCESS_TOKEN", "").strip() or parsed.get(
        "access_token", ""
    )
    client_id = os.environ.get("ADOBE_CLIENT_ID", "").strip() or parsed.get("client_id", "")
    client_secret = os.environ.get("ADOBE_CLIENT_SECRET", "").strip() or parsed.get(
        "client_secret", ""
    )

    if mcp_url:
        config["mcp_server_url"] = mcp_url
        os.environ["MCP_SERVER_URL"] = mcp_url
    if access_token:
        os.environ["ADOBE_ACCESS_TOKEN"] = access_token
    if client_id:
        os.environ["ADOBE_CLIENT_ID"] = client_id
    if client_secret:
        os.environ["ADOBE_CLIENT_SECRET"] = client_secret

    config["mcp_connection"] = {
        "server_name": parsed.get("server_name", DEFAULT_SERVER_NAME),
        "server_url": mcp_url,
        "has_access_token": bool(access_token),
        "has_client_credentials": bool(client_id and client_secret),
    }
    return config


def build_mcp_json_file(
    *,
    server_url: str,
    access_token: str = "",
    client_id: str = "",
    client_secret: str = "",
    server_name: str = DEFAULT_SERVER_NAME,
) -> dict:
    headers: dict[str, str] = {}
    token = access_token.strip()
    if token:
        headers["Authorization"] = (
            token if token.lower().startswith("bearer ") else f"Bearer {token}"
        )

    auth: dict[str, str] = {}
    if client_id.strip():
        auth["CLIENT_ID"] = client_id.strip()
    if client_secret.strip():
        auth["CLIENT_SECRET"] = client_secret.strip()

    server: dict[str, object] = {"url": server_url.strip() or "https://targetmcp.adobe.io/mcp"}
    if headers:
        server["headers"] = headers
    if auth:
        server["auth"] = auth

    return {"mcpServers": {server_name: server}}
