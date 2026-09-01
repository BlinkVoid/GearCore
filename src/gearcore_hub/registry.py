"""
Registry management commands: add-mcp, add-skill, add-cli.

All mutations target the global config (~/.config/gearcore/config.yaml) by default.
Pass scope="project" + project_root to target the project's .gearcore/config.yaml instead.

Note: add-cli requires CLI-Anything (https://github.com/HKUDS/CLI-Anything) to be
installed and available on PATH as `cli-anything`.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from pathlib import Path

import yaml

from gearcore_hub.config import GLOBAL_CONFIG_PATH

logger = logging.getLogger("gearcore.registry")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _read_yaml(path: Path) -> dict:
    if path.exists():
        try:
            with open(path, encoding="utf-8") as f:
                return yaml.safe_load(f) or {}
        except yaml.YAMLError as exc:
            # Fail loudly instead of treating the file as empty — silently
            # proceeding would overwrite the corrupt config on next write.
            raise ValueError(f"Config file {path} is not valid YAML: {exc}") from exc
    return {}


def _write_yaml(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(
            data, f, default_flow_style=False, allow_unicode=True, sort_keys=False
        )


def _config_path(scope: str, project_root: Path | None) -> Path:
    if scope == "project":
        if project_root is None:
            raise ValueError(
                "--scope project requires a project root (use --project <path>)"
            )
        return project_root / ".gearcore" / "config.yaml"
    # Resolve at call time so HOME overrides (tests, sandboxing) are honored.
    return Path.home() / ".config" / "gearcore" / "config.yaml"


def _skills_dir(scope: str, project_root: Path | None) -> Path:
    if scope == "project":
        if project_root is None:
            raise ValueError("--scope project requires a project root")
        return project_root / ".gearcore" / "skills"
    return Path.home() / ".config" / "gearcore" / "skills"


def _plugins_dir(scope: str, project_root: Path | None) -> Path:
    """Scope-specific plugins directory for whole-plugin registrations."""
    if scope == "project":
        if project_root is None:
            raise ValueError("--scope project requires a project root")
        return project_root / ".gearcore" / "plugins"
    return Path.home() / ".config" / "gearcore" / "plugins"


# ---------------------------------------------------------------------------
# add-mcp
# ---------------------------------------------------------------------------


def _server_entry(
    id: str,
    type: str,
    command: str = "",
    args: list[str] | None = None,
    url: str = "",
    env: dict[str, str] | None = None,
    enabled: bool = True,
) -> dict:
    entry: dict = {"id": id, "type": type, "enabled": enabled}
    if type == "stdio":
        entry["command"] = command
        if args:
            entry["args"] = args
        if env:
            entry["env"] = env
    else:
        entry["url"] = url
    return entry


def add_mcp(
    id: str,
    type: str,
    command: str = "",
    args: list[str] | None = None,
    url: str = "",
    env: dict[str, str] | None = None,
    scope: str = "global",
    project_root: Path | None = None,
    enabled: bool = True,
    allowlist: bool = False,
) -> Path:
    """
    Register a new MCP server in the config.

    With scope="project" + allowlist=True, no new definition is written:
    instead the id of an existing *global* server is appended to the
    project's scope.mcp_servers.include allowlist.

    Returns the path of the config file that was modified.
    Raises ValueError if an entry with the same id already exists.
    """
    cfg_path = _config_path(scope, project_root)
    data = _read_yaml(cfg_path)

    if allowlist:
        if scope != "project":
            raise ValueError("--allowlist requires --scope project")
        global_data = _read_yaml(GLOBAL_CONFIG_PATH)
        global_ids = {
            s.get("id") for s in global_data.get("registry", {}).get("mcp_servers", [])
        }
        if id not in global_ids:
            raise ValueError(
                f"MCP server '{id}' is not registered globally; "
                "omit --allowlist to write a project-local definition instead"
            )
        include = (
            data.setdefault("scope", {})
            .setdefault("mcp_servers", {})
            .setdefault("include", [])
        )
        if id in include:
            raise ValueError(f"MCP server '{id}' already allowlisted in project.")
        include.append(id)
        _write_yaml(cfg_path, data)
        logger.info("Allowlisted global MCP server '%s' in %s", id, cfg_path)
        return cfg_path

    registry_section = data.setdefault("registry", {})
    servers = registry_section.setdefault("mcp_servers", [])
    if any(s.get("id") == id for s in servers):
        where = "in project" if scope == "project" else ""
        raise ValueError(
            f"MCP server '{id}' already registered {where}. Remove it first."
        )

    servers.append(_server_entry(id, type, command, args, url, env, enabled))
    _write_yaml(cfg_path, data)
    logger.info("Registered MCP server '%s' (%s scope) in %s", id, scope, cfg_path)
    return cfg_path


# ---------------------------------------------------------------------------
# add-skill
# ---------------------------------------------------------------------------


def add_skill(
    source: Path,
    scope: str = "global",
    project_root: Path | None = None,
    symlink: bool = False,
) -> Path:
    """
    Register a skill bundle directory into the appropriate skills dir.

    If *symlink* is True, creates a symlink instead of copying (useful for
    skills still under active development).

    Returns the destination path.
    Raises FileNotFoundError if source doesn't have a SKILL.md.
    """
    source = source.resolve()
    if not (source / "SKILL.md").exists():
        raise FileNotFoundError(f"No SKILL.md found in {source}")

    dest_dir = _skills_dir(scope, project_root)
    dest = dest_dir / source.name

    if dest.exists() or dest.is_symlink():
        raise FileExistsError(
            f"Skill '{source.name}' already exists at {dest}. Remove it first."
        )

    dest_dir.mkdir(parents=True, exist_ok=True)

    if symlink:
        dest.symlink_to(source)
        logger.info("Symlinked skill '%s' → %s", source.name, dest)
    else:
        shutil.copytree(source, dest)
        logger.info("Copied skill '%s' → %s", source.name, dest)

    return dest


# ---------------------------------------------------------------------------
# add-cli (CLI-Anything integration)
# ---------------------------------------------------------------------------


def add_cli(
    program: str,
    scope: str = "global",
    project_root: Path | None = None,
    cli_anything_args: list[str] | None = None,
) -> Path:
    """
    Wrap a traditional CLI program into a GearCore skill via CLI-Anything.

    Requires `cli-anything` on PATH (https://github.com/HKUDS/CLI-Anything).

    Workflow:
      1. Run `cli-anything generate <program>` to produce an interface spec
      2. Scaffold a skill bundle (SKILL.md + manifest.json) from the output
      3. Register the bundle via add_skill()

    Returns the final skill destination path.
    """
    if shutil.which("cli-anything") is None:
        raise RuntimeError(
            "cli-anything not found on PATH. "
            "Install it from https://github.com/HKUDS/CLI-Anything"
        )

    # --- Step 1: generate CLI interface via CLI-Anything ---
    cmd = ["cli-anything", "generate", program] + (cli_anything_args or [])
    logger.info("Running: %s", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0:
        raise RuntimeError(f"cli-anything failed for '{program}':\n{result.stderr}")

    # cli-anything is expected to produce JSON describing the interface
    try:
        cli_spec = json.loads(result.stdout)
    except json.JSONDecodeError:
        # Fallback: treat stdout as raw description text
        cli_spec = {"description": result.stdout.strip(), "commands": []}

    # --- Step 2: scaffold skill bundle in a temp location ---
    skills_dir = _skills_dir(scope, project_root)
    skill_name = program.replace(" ", "-").lower()
    skill_path = skills_dir / skill_name

    if skill_path.exists():
        raise FileExistsError(
            f"Skill '{skill_name}' already exists at {skill_path}. Remove it first."
        )

    skill_path.mkdir(parents=True, exist_ok=True)

    # manifest.json
    manifest = {
        "name": skill_name,
        "version": "1.0.0",
        "description": cli_spec.get("description", f"CLI wrapper for {program}"),
        "category": "cli",
        "mcp_servers": [],
        "activation": {
            "strategy": "manual",
            "triggers": [skill_name, program],
        },
    }
    with open(skill_path / "manifest.json", "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)

    # SKILL.md — generate from CLI-Anything spec
    commands_section = ""
    for cmd_spec in cli_spec.get("commands", []):
        name = cmd_spec.get("name", "")
        desc = cmd_spec.get("description", "")
        usage = cmd_spec.get("usage", "")
        commands_section += f"\n### `{name}`\n{desc}\n```\n{usage}\n```\n"

    skill_md = f"""---
name: {skill_name}
description: {manifest["description"]}
---

# {skill_name}

{manifest["description"]}

## Usage

Invoke via shell command: `{program}`

## Commands
{commands_section if commands_section else "_Run `" + program + " --help` for available commands._"}

## Notes

- Generated by CLI-Anything from `{program}`
- Adjust this SKILL.md to add workflow guidance specific to your use case
"""
    (skill_path / "SKILL.md").write_text(skill_md, encoding="utf-8")

    logger.info("Scaffolded CLI skill '%s' at %s", skill_name, skill_path)
    return skill_path


# ---------------------------------------------------------------------------
# remove
# ---------------------------------------------------------------------------


def remove_mcp(
    id: str, scope: str = "global", project_root: Path | None = None
) -> Path:
    """Remove an MCP server entry from config."""
    cfg_path = _config_path(scope, project_root)
    data = _read_yaml(cfg_path)

    if scope == "global":
        servers = data.get("registry", {}).get("mcp_servers", [])
        before = len(servers)
        data["registry"]["mcp_servers"] = [s for s in servers if s.get("id") != id]
        if len(data["registry"]["mcp_servers"]) == before:
            raise KeyError(f"MCP server '{id}' not found in global config")
    else:
        removed = False
        servers = data.get("registry", {}).get("mcp_servers", [])
        remaining = [s for s in servers if s.get("id") != id]
        if len(remaining) != len(servers):
            data["registry"]["mcp_servers"] = remaining
            removed = True
        include = data.get("scope", {}).get("mcp_servers", {}).get("include", [])
        if id in include:
            include.remove(id)
            removed = True
        if not removed:
            raise KeyError(f"MCP server '{id}' not found in project config")

    _write_yaml(cfg_path, data)
    logger.info("Removed MCP server '%s' from %s", id, cfg_path)
    return cfg_path


def _validated_skill_dest(name: str, dest_dir: Path) -> Path:
    """Join *name* onto *dest_dir*, rejecting anything that could escape it.

    This is a lexical check only: symlinked skill bundles legitimately resolve
    outside the skills dir, so the final path must not be resolved here.
    Skill and plugin registrations are single path components; rejecting
    absolute, dotted, and nested names makes escape impossible for the join.
    """
    if not isinstance(name, str) or not name:
        raise ValueError(f"Invalid skill name: {name!r}")
    path = Path(name)
    if path.is_absolute() or len(path.parts) != 1 or path.parts[0] in {".", ".."}:
        raise ValueError(f"Invalid skill name: {name!r}")
    return dest_dir / name


def remove_skill(
    name: str,
    scope: str = "global",
    project_root: Path | None = None,
) -> Path:
    """Delete a skill bundle directory from the skills dir."""
    dest = _validated_skill_dest(name, _skills_dir(scope, project_root))
    if not dest.exists() and not dest.is_symlink():
        raise FileNotFoundError(f"Skill '{name}' not found at {dest}")
    if dest.is_symlink():
        dest.unlink()
    else:
        shutil.rmtree(dest)
    logger.info("Removed skill '%s' from %s", name, dest.parent)
    return dest.parent


def _skill_links_inside(
    plugin_path: Path, scope: str, project_root: Path | None
) -> list[Path]:
    """Skill symlinks in the skills dir that point inside *plugin_path*.

    Only symlinks qualify: a real directory in the skills dir is never
    touched, and only the link itself is ever deleted — never its target.
    """
    skills_root = _skills_dir(scope, project_root)
    if not skills_root.is_dir():
        return []
    try:
        inside_root = plugin_path.resolve()
    except OSError:
        inside_root = plugin_path
    links: list[Path] = []
    for entry in sorted(skills_root.iterdir()):
        if not entry.is_symlink():
            continue
        try:
            target = entry.resolve()
        except OSError:
            continue
        if target == inside_root or inside_root in target.parents:
            links.append(entry)
    return links


def remove_plugin(
    name: str,
    scope: str = "global",
    project_root: Path | None = None,
) -> Path:
    """Remove a registered plugin and the skill links pointing into it.

    Removes only the registered plugin path under the plugins dir plus skill
    symlinks whose targets resolve inside that registered plugin. External
    sources of symlinks are never deleted.
    """
    plugin_path = _validated_skill_dest(name, _plugins_dir(scope, project_root))
    if not plugin_path.exists() and not plugin_path.is_symlink():
        raise FileNotFoundError(f"Plugin '{name}' not found at {plugin_path}")

    links = _skill_links_inside(plugin_path, scope, project_root)
    if plugin_path.is_symlink():
        plugin_path.unlink()
    else:
        shutil.rmtree(plugin_path)
    for link in links:
        link.unlink()
    logger.info(
        "Removed plugin '%s' from %s (%d skill links)",
        name,
        plugin_path.parent,
        len(links),
    )
    return plugin_path.parent
