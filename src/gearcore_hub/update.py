"""Version detection and source-path inference for `gearcore update`."""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path
from typing import Any

from gearcore_hub.config import McpServerConfig
from gearcore_hub.registry import (
    _config_path,
    _read_yaml,
    _write_yaml,
    add_mcp,
    remove_mcp,
)

logger = logging.getLogger("gearcore.update")


def get_git_revision(path: Path) -> str | None:
    """Return short SHA of HEAD if *path* is inside a git repo, else None."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=path,
            capture_output=True,
            text=True,
            check=True,
            timeout=10.0,
        )
        return result.stdout.strip()
    except Exception as exc:
        logger.debug("git rev-parse failed for %s: %s", path, exc)
        return None


def get_manifest_version(path: Path) -> str | None:
    """Read 'version' from manifest.json in *path*, if present."""
    manifest = path / "manifest.json"
    if not manifest.exists():
        return None
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
        version = data.get("version") if isinstance(data, dict) else None
        return version if isinstance(version, str) else None
    except Exception as exc:
        logger.debug("Failed to read manifest at %s: %s", manifest, exc)
        return None


def _split_kv(arg: str) -> tuple[str, str] | None:
    if "=" in arg:
        key, value = arg.split("=", 1)
        return key, value
    return None


def infer_mcp_source_path(server: McpServerConfig) -> Path | None:
    """Infer the local source path of an MCP server from its command/args."""
    # 1. Look for --directory / -d / --directory=... in args
    for i, arg in enumerate(server.args):
        arg_norm = arg.strip()
        if arg_norm in ("--directory", "-d") and i + 1 < len(server.args):
            return Path(server.args[i + 1]).expanduser().resolve()
        kv = _split_kv(arg_norm)
        if kv and kv[0] in ("--directory", "-d"):
            return Path(kv[1]).expanduser().resolve()

    # 2. If command is an absolute path inside a workspace, assume it's the source root
    cmd = Path(server.command).expanduser()
    if cmd.is_absolute() and not cmd.name.startswith(("uv", "python", "npx")):
        # e.g. /home/.../sample-devtools-mcp
        return cmd.parent.resolve()

    return None


def load_skill_manifest(path: Path) -> dict[str, Any] | None:
    """Load a skill bundle's manifest.json."""
    manifest = path / "manifest.json"
    if not manifest.exists():
        return None
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception as exc:
        logger.debug("Failed to parse manifest at %s: %s", manifest, exc)
        return None


def update_mcp_server(
    config,
    server_id: str,
    *,
    dry_run: bool = False,
    source_path: Path | None = None,
) -> dict:
    """Refresh a single MCP server registration if its source has changed.

    Returns a dict: {
        "id": str,
        "changed": bool,
        "previous_revision": str | None,
        "current_revision": str | None,
        "message": str,
    }
    """
    server = next((s for s in config.mcp_servers if s.id == server_id), None)
    if server is None:
        return {
            "id": server_id,
            "changed": False,
            "previous_revision": None,
            "current_revision": None,
            "message": f"MCP server '{server_id}' not found.",
        }

    path = source_path or infer_mcp_source_path(server)
    if path is None:
        return {
            "id": server_id,
            "changed": False,
            "previous_revision": None,
            "current_revision": None,
            "message": f"Could not infer source path for '{server_id}'.",
        }

    previous = (server.update_metadata or {}).get("revision")
    current = get_git_revision(path)
    if current is None:
        return {
            "id": server_id,
            "changed": False,
            "previous_revision": previous,
            "current_revision": None,
            "message": f"'{path}' is not a git repo; cannot detect version.",
        }

    if previous == current:
        return {
            "id": server_id,
            "changed": False,
            "previous_revision": previous,
            "current_revision": current,
            "message": f"'{server_id}' is up to date ({current}).",
        }

    if dry_run:
        return {
            "id": server_id,
            "changed": True,
            "previous_revision": previous,
            "current_revision": current,
            "message": f"Would update '{server_id}' {previous} -> {current}.",
        }

    # Re-register: remove then add with identical fields + new metadata
    scope = (
        "project"
        if config.project_cfg
        and any(s.id == server_id for s in config.project_cfg.mcp_servers)
        else "global"
    )
    project_root = config.project_root if scope == "project" else None

    try:
        remove_mcp(server_id, scope=scope, project_root=project_root)
        add_mcp(
            id=server.id,
            type=server.type,
            command=server.command,
            args=list(server.args),
            url=server.url,
            env=dict(server.env) if server.env else None,
            scope=scope,
            project_root=project_root,
            enabled=server.enabled,
        )
    except Exception as exc:
        return {
            "id": server_id,
            "changed": False,
            "previous_revision": previous,
            "current_revision": current,
            "message": f"Failed to update '{server_id}': {exc}",
        }

    # Write updated metadata back into the config entry.
    # We reload and mutate the raw YAML so we don't drop extra keys.
    cfg_path = _config_path(scope, project_root)
    data = _read_yaml(cfg_path)
    for s in data.get("registry", {}).get("mcp_servers", []):
        if s.get("id") == server_id:
            s.setdefault("update_metadata", {}).update(
                {
                    "source_path": str(path),
                    "revision": current,
                }
            )
            break
    _write_yaml(cfg_path, data)

    return {
        "id": server_id,
        "changed": True,
        "previous_revision": previous,
        "current_revision": current,
        "message": f"Updated '{server_id}' {previous or 'unknown'} -> {current}.",
    }


def update_all_mcp_servers(config, *, dry_run: bool = False) -> list[dict]:
    """Update all MCP servers for which a source path can be inferred."""
    results = []
    for server in config.mcp_servers:
        result = update_mcp_server(config, server.id, dry_run=dry_run)
        results.append(result)
    return results
