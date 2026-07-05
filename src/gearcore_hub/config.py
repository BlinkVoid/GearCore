"""
Layered configuration loader for GearCore.

Resolution order (highest priority last):
  1. Built-in defaults
  2. Global  — ~/.config/gearcore/config.yaml
  3. Project — <project>/.gearcore/config.yaml  (only when project context present)
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from gearcore_hub.vendor import bundled_superpowers_dir

logger = logging.getLogger("gearcore.config")

# ---------------------------------------------------------------------------
# Schema models
# ---------------------------------------------------------------------------


class McpServerConfig(BaseModel):
    id: str
    type: str = "stdio"
    command: str = ""
    args: list[str] = Field(default_factory=list)
    url: str = ""
    env: dict[str, str] | None = None
    enabled: bool = True


class ResolutionCategory(BaseModel):
    preferred: str = ""
    strategy: str = "namespace"  # suppress_others | namespace | unify
    namespace_prefix: str = ""
    unified_name: str = ""


class ResolutionConfig(BaseModel):
    auto_deduplicate: bool = True
    categories: dict[str, ResolutionCategory] = Field(default_factory=dict)


class DisclosureConfig(BaseModel):
    strategy: str = "manual"  # manual | semantic
    activation_threshold: float = 0.85
    core_skills: list[str] = Field(default_factory=list)


_DEFAULT_SKILLS_DIRS = [
    Path.home() / ".config" / "gearcore" / "skills",
    Path.home() / ".config" / "agents" / "skills",
]


def _default_skills_dirs() -> list[Path]:
    dirs = list(_DEFAULT_SKILLS_DIRS)
    bundled = bundled_superpowers_dir()
    if bundled is not None and bundled not in dirs:
        dirs.append(bundled)
    return dirs


class GlobalConfig(BaseModel):
    """Schema for ~/.config/gearcore/config.yaml"""

    version: int = 2
    registry: dict[str, Any] = Field(default_factory=dict)
    disclosure: DisclosureConfig = Field(default_factory=DisclosureConfig)
    resolution: ResolutionConfig = Field(default_factory=ResolutionConfig)

    @property
    def mcp_servers(self) -> list[McpServerConfig]:
        raw = self.registry.get("mcp_servers", [])
        return [McpServerConfig(**s) for s in raw]

    @property
    def skills_dirs(self) -> list[Path]:
        raw = self.registry.get("skills_dirs", [])
        dirs: list[Path] = [Path(os.path.expanduser(str(p))) for p in raw]
        if not dirs:
            dirs = _default_skills_dirs()
        bundled = bundled_superpowers_dir()
        if bundled is not None and bundled not in dirs:
            dirs.append(bundled)
        return dirs


class ProjectScope(BaseModel):
    mcp_servers: dict[str, list[str]] = Field(default_factory=dict)  # include: [ids]
    skills: dict[str, list[str]] = Field(default_factory=dict)  # include: [names]


class ProjectContext(BaseModel):
    name: str = ""
    description: str = ""


class ProjectConfig(BaseModel):
    """Schema for <project>/.gearcore/config.yaml"""

    version: int = 2
    context: ProjectContext = Field(default_factory=ProjectContext)
    scope: ProjectScope = Field(default_factory=ProjectScope)
    disclosure: DisclosureConfig | None = None  # overrides global if present

    @property
    def mcp_allowlist(self) -> list[str] | None:
        inc = self.scope.mcp_servers.get("include")
        return inc if inc is not None else None

    @property
    def skill_allowlist(self) -> list[str] | None:
        inc = self.scope.skills.get("include")
        return inc if inc is not None else None


class EffectiveConfig:
    """
    Merged view produced by the loader.  Consumers should use this only —
    never GlobalConfig / ProjectConfig directly.
    """

    def __init__(
        self,
        global_cfg: GlobalConfig,
        project_cfg: ProjectConfig | None,
        project_root: Path | None,
    ):
        self.global_cfg = global_cfg
        self.project_cfg = project_cfg
        self.project_root = project_root

    # --- MCP servers ---

    @property
    def mcp_servers(self) -> list[McpServerConfig]:
        servers = [s for s in self.global_cfg.mcp_servers if s.enabled]
        if self.project_cfg is None:
            return servers
        allowlist = self.project_cfg.mcp_allowlist
        if allowlist is None:
            return servers  # no scope key → keep all
        return [s for s in servers if s.id in allowlist]

    # --- Skills dirs (global first, then project-local) ---

    @property
    def skills_dirs(self) -> list[Path]:
        dirs: list[Path] = list(self.global_cfg.skills_dirs)
        if self.project_root is not None:
            local = self.project_root / ".gearcore" / "skills"
            if local not in dirs:
                dirs.append(local)
        return dirs

    @property
    def global_skill_allowlist(self) -> list[str] | None:
        """None means 'allow all globals'. A list is the explicit allowlist."""
        if self.project_cfg is None:
            return None
        return self.project_cfg.skill_allowlist

    # --- Disclosure ---

    @property
    def disclosure(self) -> DisclosureConfig:
        if self.project_cfg and self.project_cfg.disclosure:
            return self.project_cfg.disclosure
        return self.global_cfg.disclosure

    # --- Resolution ---

    @property
    def resolution(self) -> ResolutionConfig:
        return self.global_cfg.resolution

    # --- Context label ---

    @property
    def context_name(self) -> str:
        if self.project_cfg and self.project_cfg.context.name:
            return self.project_cfg.context.name
        return "global"


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------

GLOBAL_CONFIG_PATH = Path.home() / ".config" / "gearcore" / "config.yaml"
PROJECT_CONFIG_NAME = ".gearcore"


def _load_yaml(path: Path) -> dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        logger.debug("Loaded config from %s", path)
        return data
    except FileNotFoundError:
        logger.debug("Config not found at %s, skipping", path)
        return {}
    except Exception as exc:
        logger.error("Failed to parse config at %s: %s", path, exc)
        return {}


def find_project_root(start: Path | None = None) -> Path | None:
    """Walk up from *start* (default CWD) looking for a .gearcore/ directory."""
    current = (start or Path.cwd()).resolve()
    while True:
        if (current / PROJECT_CONFIG_NAME).is_dir():
            return current
        parent = current.parent
        if parent == current:
            return None
        current = parent


def load_config(
    project: Path | None = None,
    global_config_path: Path | None = None,
) -> EffectiveConfig:
    """
    Load and merge global + optional project config.

    Args:
        project: Explicit project root. If None, auto-detect via CWD walk-up.
        global_config_path: Override for global config file (testing / custom installs).
    """
    # --- Global ---
    g_path = global_config_path or GLOBAL_CONFIG_PATH
    g_data = _load_yaml(g_path)
    global_cfg = GlobalConfig(**g_data)

    # --- Project ---
    project_root: Path | None = None
    project_cfg: ProjectConfig | None = None

    project_root = project.resolve() if project is not None else find_project_root()

    if project_root is not None:
        p_file = project_root / PROJECT_CONFIG_NAME / "config.yaml"
        p_data = _load_yaml(p_file)
        if p_data:
            project_cfg = ProjectConfig(**p_data)
            logger.info(
                "Project context: %s (%s)",
                project_cfg.context.name or project_root.name,
                project_root,
            )
        else:
            logger.debug("No project config found at %s", p_file)

    return EffectiveConfig(global_cfg, project_cfg, project_root)
