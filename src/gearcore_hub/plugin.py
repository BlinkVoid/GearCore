"""Codex-compatible plugin root detection and manifest parsing.

A plugin root is a directory containing ``.codex-plugin/plugin.json``. The
manifest declares the plugin ``name`` and an optional ``skills`` path
(default ``./skills``). GearCore treats the plugin root as the unit of
registration: skills plus sibling support components (commands, orchestration,
scripts, config, configs, tests, docs) are preserved as-is.

Safety boundary: GearCore registers and preserves plugin content but never
auto-executes arbitrary plugin files.
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
from pathlib import Path

PLUGIN_MARKER_DIR = ".codex-plugin"
PLUGIN_MANIFEST_NAME = "plugin.json"
DEFAULT_SKILLS_PATH = "skills"

# Top-level sibling components preserved during whole-plugin onboarding.
SUPPORT_COMPONENTS = (
    "commands",
    "orchestration",
    "scripts",
    "config",
    "configs",
    "tests",
    "docs",
)

PLUGIN_NAME_PATTERN = re.compile(r"[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*\Z")


@dataclasses.dataclass(frozen=True)
class PluginManifest:
    name: str
    root: Path
    skills_path: str  # normalized path relative to the plugin root

    @property
    def skills_dir(self) -> Path:
        return self.root / self.skills_path


def validate_plugin_name(name: object) -> str:
    """Validate a plugin identifier using the Codex plugin name grammar."""
    if not isinstance(name, str) or not PLUGIN_NAME_PATTERN.fullmatch(name):
        raise ValueError(
            "Plugin manifest 'name' must match [A-Za-z0-9_-]+(?:\\.[A-Za-z0-9_-]+)*"
        )
    return name


def _skills_dir_inside_root(root: Path, declared: object) -> str:
    if not isinstance(declared, str) or not declared:
        raise ValueError("Plugin manifest 'skills' must be a non-empty string path")
    relative = Path(declared)
    if relative.is_absolute():
        raise ValueError(
            f"Plugin 'skills' path must stay inside the plugin root: {declared!r}"
        )
    root_norm = Path(os.path.normpath(root))
    candidate = Path(os.path.normpath(root_norm / relative))
    try:
        resolved_candidate = candidate.resolve(strict=False)
    except (OSError, ValueError) as exc:
        raise ValueError(
            f"Plugin 'skills' path must stay inside the plugin root: {declared!r}"
        ) from exc
    if (
        candidate == root_norm
        or root_norm not in candidate.parents
        or resolved_candidate == root_norm
        or root_norm not in resolved_candidate.parents
    ):
        raise ValueError(
            f"Plugin 'skills' path must stay inside the plugin root: {declared!r}"
        )
    return candidate.relative_to(root_norm).as_posix()


def load_plugin_manifest(root: Path) -> PluginManifest | None:
    """Parse the plugin manifest at *root*, or return None for a plain core.

    Raises ValueError when a manifest exists but is malformed or declares
    unsafe names/paths.
    """
    manifest_path = root / PLUGIN_MARKER_DIR / PLUGIN_MANIFEST_NAME
    if not manifest_path.is_file():
        return None

    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Failed to parse {manifest_path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"Plugin manifest {manifest_path} must be a JSON object")

    name = validate_plugin_name(data.get("name"))
    resolved_root = root.resolve()
    skills_path = _skills_dir_inside_root(
        resolved_root, data.get("skills", DEFAULT_SKILLS_PATH)
    )
    return PluginManifest(name=name, root=resolved_root, skills_path=skills_path)


def discover_support_components(root: Path) -> list[str]:
    """Existing top-level support components, in canonical order."""
    return [name for name in SUPPORT_COMPONENTS if (root / name).exists()]
