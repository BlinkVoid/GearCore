"""Version detection and source-path inference for `gearcore update`."""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from gearcore_hub.config import McpServerConfig
from gearcore_hub.registry import (
    _config_path,
    _read_yaml,
    _validated_skill_dest,
    _write_yaml,
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

    scope = (
        "project"
        if config.project_cfg
        and any(s.id == server_id for s in config.project_cfg.mcp_servers)
        else "global"
    )
    project_root = config.project_root if scope == "project" else None

    # Update the raw YAML entry in place: single write, no remove/add window
    # where the server would be deregistered, and unknown keys are preserved.
    cfg_path = _config_path(scope, project_root)
    try:
        data = _read_yaml(cfg_path)
        servers = data.get("registry", {}).get("mcp_servers", [])
        entry = next((s for s in servers if s.get("id") == server_id), None)
        if entry is None:
            return {
                "id": server_id,
                "changed": False,
                "previous_revision": previous,
                "current_revision": current,
                "message": f"Entry '{server_id}' vanished from {cfg_path}; "
                "not updating.",
            }
        entry.update(
            {
                "id": server.id,
                "type": server.type,
                "command": server.command,
                "url": server.url,
                "enabled": server.enabled,
            }
        )
        if server.args:
            entry["args"] = list(server.args)
        else:
            entry.pop("args", None)
        if server.env:
            entry["env"] = dict(server.env)
        else:
            entry.pop("env", None)
        entry.setdefault("update_metadata", {}).update(
            {
                "source_path": str(path),
                "revision": current,
            }
        )
        _write_yaml(cfg_path, data)
    except Exception as exc:
        return {
            "id": server_id,
            "changed": False,
            "previous_revision": previous,
            "current_revision": current,
            "message": f"Failed to update '{server_id}': {exc}",
        }

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


def _skill_update_metadata_path(skills_dir: Path, name: str) -> Path:
    return skills_dir / f".{name}.gearcore-update.json"


def _load_skill_update_metadata(skills_dir: Path, name: str) -> dict[str, Any]:
    path = _skill_update_metadata_path(skills_dir, name)
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception as exc:
            logger.debug("Failed to read update metadata for %s: %s", name, exc)
    return {}


def _save_skill_update_metadata(
    skills_dir: Path, name: str, metadata: dict[str, Any]
) -> None:
    path = _skill_update_metadata_path(skills_dir, name)
    path.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")


def _find_installed_skill(name: str, config) -> Path | None:
    """Find installed skill directory by name across skills_dirs."""
    for directory in config.skills_dirs:
        candidate = _validated_skill_dest(name, Path(directory))
        if candidate.exists():
            return candidate
    return None


def update_skill(
    name: str,
    config,
    *,
    dry_run: bool = False,
    source_path: Path | None = None,
) -> dict:
    """Refresh a skill bundle if its source manifest/git version changed.

    For symlinked skills, *source_path* defaults to the symlink target.
    For copied skills, the original source is unknown; pass --source-path
    or the skill will be reported as "source unknown".
    """
    try:
        installed = _find_installed_skill(name, config)
    except ValueError as exc:
        return {
            "name": name,
            "changed": False,
            "message": str(exc),
        }
    if installed is None:
        return {
            "name": name,
            "changed": False,
            "message": f"Skill '{name}' not found in any skills dir.",
        }

    # Determine the authoritative source path
    src = source_path
    if src is None and installed.is_symlink():
        src = installed.readlink().resolve()
    if src is None:
        return {
            "name": name,
            "changed": False,
            "message": (
                f"Source of copied skill '{name}' unknown; "
                "pass --source-path to enable updates."
            ),
        }

    manifest = load_skill_manifest(installed)
    version = manifest.get("version") if manifest else None
    git_rev = get_git_revision(src) if src else None

    # The skills_dir that owns this installation
    skills_dir = installed.parent
    previous = _load_skill_update_metadata(skills_dir, name)
    current = {"version": version, "revision": git_rev}

    if previous == current and current != {"version": None, "revision": None}:
        return {
            "name": name,
            "changed": False,
            "message": f"Skill '{name}' is up to date ({version or git_rev}).",
        }

    if dry_run:
        return {
            "name": name,
            "changed": True,
            "message": f"Would update skill '{name}'.",
        }

    try:
        if installed.is_symlink():
            installed.unlink()
            installed.symlink_to(src)
        else:
            shutil.rmtree(installed)
            shutil.copytree(src, installed)
        _save_skill_update_metadata(skills_dir, name, current)
    except Exception as exc:
        return {
            "name": name,
            "changed": False,
            "message": f"Failed to update skill '{name}': {exc}",
        }

    return {
        "name": name,
        "changed": True,
        "message": f"Updated skill '{name}'.",
    }


def update_all_skills(config, *, dry_run: bool = False) -> list[dict]:
    """Update all skill bundles found in the active skills_dirs."""
    results = []
    seen: set[str] = set()
    for directory in config.skills_dirs:
        directory = Path(directory)
        if not directory.exists():
            continue
        for entry in sorted(directory.iterdir()):
            if entry.is_dir() and not entry.name.startswith("."):
                if entry.name in seen:
                    continue
                seen.add(entry.name)
                results.append(update_skill(entry.name, config, dry_run=dry_run))
    return results


def cmd_update(config, args) -> None:
    """Entry point for `gearcore update`."""
    dry_run = args.dry_run
    source_path = args.source_path
    resource = args.resource
    name = args.name

    if resource == "superpowers":
        from gearcore_hub.vendor import update_superpowers

        try:
            result = update_superpowers(dry_run=dry_run)
        except RuntimeError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            sys.exit(1)
        upstream = result.get("upstream", "")
        upstream_short = upstream[:12] if upstream else ""
        if result.get("changed"):
            if dry_run:
                print(f"Update available: superpowers {upstream_short}")
            else:
                print(f"Updated superpowers to {upstream_short}")
        else:
            print(f"superpowers is up to date ({upstream_short})")
        return

    if resource == "mcp":
        if name:
            results = [
                update_mcp_server(
                    config, name, dry_run=dry_run, source_path=source_path
                )
            ]
        else:
            results = update_all_mcp_servers(config, dry_run=dry_run)
        for r in results:
            print(f"  {r['id']:20s} {r['message']}")
        return

    if resource == "skill":
        if name:
            results = [
                update_skill(name, config, dry_run=dry_run, source_path=source_path)
            ]
        else:
            results = update_all_skills(config, dry_run=dry_run)
        for r in results:
            print(f"  {r['name']:20s} {r['message']}")
        return

    if resource is None:
        # Update everything
        print("Updating MCP servers...")
        for r in update_all_mcp_servers(config, dry_run=dry_run):
            print(f"  {r['id']:20s} {r['message']}")
        print("Updating skills...")
        for r in update_all_skills(config, dry_run=dry_run):
            print(f"  {r['name']:20s} {r['message']}")
        print("Updating superpowers...")
        from gearcore_hub.vendor import update_superpowers

        try:
            sp_result = update_superpowers(dry_run=dry_run)
        except RuntimeError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            sys.exit(1)
        upstream = sp_result.get("upstream", "")
        upstream_short = upstream[:12] if upstream else ""
        if sp_result.get("changed"):
            if dry_run:
                print(f"  {'superpowers':20s} update available ({upstream_short})")
            else:
                print(f"  {'superpowers':20s} updated to {upstream_short}")
        else:
            print(f"  {'superpowers':20s} up to date ({upstream_short})")
        if not dry_run:
            print("Syncing GearCore self-skill...")
            from gearcore_hub.sync import sync

            sync_results = sync()
            for target, sync_result in sync_results.items():
                print(f"  {target:20s} {sync_result}")
        return

    # argparse choices should prevent reaching here
    print(f"Error: unknown resource '{resource}'", file=sys.stderr)
    sys.exit(1)
