"""Version detection and source-path inference for `gearcore update`."""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path
from typing import Any

from gearcore_hub.config import McpServerConfig

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
        return data.get("version")
    except Exception as exc:
        logger.debug("Failed to read manifest at %s: %s", manifest, exc)
        return None


def _split_kv(arg: str) -> tuple[str, str] | None:
    if "=" in arg:
        return arg.split("=", 1)
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
        return json.loads(manifest.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.debug("Failed to parse manifest at %s: %s", manifest, exc)
        return None
