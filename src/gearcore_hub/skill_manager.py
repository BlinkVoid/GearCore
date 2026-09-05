"""
Skill manager with two-phase loading and project-scope visibility gating.

Loading sequence:
  Phase 1 — global skills dirs (from EffectiveConfig.skills_dirs, excluding project-local)
  Phase 2 — project-local skills dir (.gearcore/skills/) appended unconditionally

Visibility rules:
  - Global skills: visible always; when project context present, only if in allowlist
  - Project-local skills: invisible with no project context; always visible with project context
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel

from gearcore_hub.config import EffectiveConfig

logger = logging.getLogger("gearcore.skill_manager")


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class SkillManifest(BaseModel):
    name: str
    version: str = "1.0.0"
    description: str = ""
    category: str = "general"
    mcp_servers: list[dict[str, Any]] = []
    scripts: list[dict[str, Any]] | None = []
    activation: dict[str, Any] = {}


class SkillBundle:
    def __init__(
        self,
        path: Path,
        manifest: SkillManifest,
        instructions: str,
        is_project_local: bool = False,
    ):
        self.path = path
        self.manifest = manifest
        self.instructions = instructions
        self.is_project_local = is_project_local
        self.active = False


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------


def _frontmatter_metadata(instructions: str) -> dict[str, str]:
    """
    Extract name/description from SKILL.md YAML frontmatter.

    Mirrors onboard._extract_skill_name: only a mapping frontmatter with
    non-blank string scalars yields metadata; malformed or non-conforming
    frontmatter yields nothing so callers fall back to manifest or path name.
    """
    if not instructions.startswith("---\n"):
        return {}
    parts = instructions.split("---", 2)
    if len(parts) < 2:
        return {}
    try:
        frontmatter = yaml.safe_load(parts[1])
    except Exception:
        return {}
    if not isinstance(frontmatter, dict):
        return {}
    metadata: dict[str, str] = {}
    for key in ("name", "description"):
        value = frontmatter.get(key)
        if isinstance(value, str) and value.strip():
            metadata[key] = value
    return metadata


class SkillManager:
    """
    Loads and gates skills based on EffectiveConfig.

    skills registry holds ALL discovered skills (global + project-local).
    visible_skills is the filtered set the caller may see and activate.
    active_skills tracks what has been explicitly unlocked this session.
    """

    def __init__(self, config: EffectiveConfig):
        self.config = config
        self.skills: dict[str, SkillBundle] = {}
        self.active_skills: set[str] = set()
        self.broken_skills: dict[str, str] = {}  # name → broken target path
        self.broken_skill_scopes: dict[str, bool] = {}  # name → project-local
        self._load()
        self._auto_activate_core()

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------

    def _load(self):
        self.skills.clear()
        self.broken_skills.clear()
        self.broken_skill_scopes.clear()
        project_local_dir: Path | None = None
        if self.config.project_root is not None:
            project_local_dir = self.config.project_root / ".gearcore" / "skills"

        for skills_dir in self.config.skills_dirs:
            is_local = project_local_dir is not None and skills_dir == project_local_dir
            self._scan_dir(skills_dir, is_project_local=is_local)

        if self.broken_skills:
            logger.warning(
                "Broken skill symlinks detected (%d): %s",
                len(self.broken_skills),
                ", ".join(self.broken_skills),
            )

        logger.info(
            "Skills loaded: %d total (%d project-local, %d broken)",
            len(self.skills),
            sum(1 for s in self.skills.values() if s.is_project_local),
            len(self.broken_skills),
        )

    def _scan_dir(self, skills_dir: Path, *, is_project_local: bool):
        if not skills_dir.exists():
            logger.debug("Skills dir not found, skipping: %s", skills_dir)
            return

        for skill_path in sorted(skills_dir.iterdir()):
            # Detect broken symlinks: is_symlink() is True but exists() is False
            # when the target has been moved or deleted.
            if skill_path.is_symlink() and not skill_path.exists():
                target = str(skill_path.resolve())
                self.broken_skills[skill_path.name] = target
                self.broken_skill_scopes[skill_path.name] = is_project_local
                logger.warning(
                    "Broken symlink for skill '%s' → %s (target missing)",
                    skill_path.name,
                    target,
                )
                continue
            if not skill_path.is_dir():
                continue
            self._load_bundle(skill_path, is_project_local=is_project_local)

    def _load_bundle(self, skill_path: Path, *, is_project_local: bool):
        instructions_file = skill_path / "SKILL.md"
        if not instructions_file.exists():
            return

        # manifest.json is optional — metadata falls back to SKILL.md
        # frontmatter, then path name (resolved below)
        manifest_file = skill_path / "manifest.json"
        if manifest_file.exists():
            try:
                with open(manifest_file, encoding="utf-8") as f:
                    manifest = SkillManifest(**json.load(f))
            except Exception as exc:
                logger.error("Bad manifest at %s: %s", manifest_file, exc)
                return
        else:
            # No manifest: start blank so SKILL.md frontmatter and then the
            # path directory name supply identity and description below.
            manifest = SkillManifest(name="", description="")

        try:
            instructions = instructions_file.read_text(encoding="utf-8")
        except Exception as exc:
            logger.error("Cannot read SKILL.md at %s: %s", instructions_file, exc)
            return

        # SKILL.md frontmatter fills in missing or blank manifest metadata;
        # explicit manifest values win and the path directory name is the
        # final identity fallback.
        frontmatter = _frontmatter_metadata(instructions)
        if not manifest.name.strip():
            manifest.name = frontmatter.get("name") or skill_path.name
        if not manifest.description.strip():
            manifest.description = frontmatter.get("description", "")

        bundle = SkillBundle(
            path=skill_path,
            manifest=manifest,
            instructions=instructions,
            is_project_local=is_project_local,
        )
        if manifest.name in self.skills:
            previous = self.skills[manifest.name]
            logger.warning(
                "Skill '%s' from %s shadows definition from %s "
                "(project-local definitions override global ones)",
                manifest.name,
                skill_path,
                previous.path,
            )
        self.skills[manifest.name] = bundle
        logger.debug(
            "Loaded %sskill: %s",
            "project-local " if is_project_local else "",
            manifest.name,
        )

    def _auto_activate_core(self):
        for name in self.config.disclosure.core_skills:
            if name in self.visible_skill_names:
                self.activate_skill(name)
                logger.info("Auto-activated core skill: %s", name)

    # ------------------------------------------------------------------
    # Visibility
    # ------------------------------------------------------------------

    @property
    def visible_skill_names(self) -> set[str]:
        """
        Names the current context is allowed to see.

        - No project: all global skills visible, no project-locals
        - With project + allowlist: only allowlisted globals + all project-locals
        - With project + no allowlist key: all globals + all project-locals
        """
        result: set[str] = set()
        allowlist = self.config.global_skill_allowlist  # None = allow all

        for name, bundle in self.skills.items():
            if bundle.is_project_local:
                # project-locals only visible when project context present
                if self.config.project_root is not None:
                    result.add(name)
            else:
                # global skill
                if allowlist is None or name in allowlist:
                    result.add(name)

        return result

    @property
    def visible_broken_skill_names(self) -> set[str]:
        """Names of broken symlinks visible in the current context."""
        allowlist = self.config.global_skill_allowlist
        return {
            name
            for name, is_project_local in self.broken_skill_scopes.items()
            if (
                (is_project_local and self.config.project_root is not None)
                or (not is_project_local and (allowlist is None or name in allowlist))
            )
        }

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def refresh(self):
        """Re-scan all skill dirs (e.g. after add-skill)."""
        active_before = set(self.active_skills)
        self._load()
        # Re-activate previously active skills that still exist
        for name in active_before:
            if name in self.skills:
                self.active_skills.add(name)
        self._auto_activate_core()

    def list_available_skills(self) -> list[dict[str, str]]:
        visible = self.visible_skill_names
        result = [
            {
                "name": s.manifest.name,
                "description": s.manifest.description,
                "category": s.manifest.category,
                "scope": "project" if s.is_project_local else "global",
                "status": "active"
                if s.manifest.name in self.active_skills
                else "available",
            }
            for name, s in self.skills.items()
            if name in visible
        ]
        # Append broken symlinks so users are aware and can fix them
        for name, target in self.broken_skills.items():
            result.append(
                {
                    "name": name,
                    "description": f"BROKEN SYMLINK → {target}",
                    "category": "broken",
                    "scope": "unknown",
                    "status": "broken",
                }
            )
        return result

    def get_skill(self, name: str) -> SkillBundle | None:
        if name not in self.visible_skill_names:
            return None
        return self.skills.get(name)

    def activate_skill(self, name: str) -> bool:
        if name not in self.visible_skill_names:
            logger.warning("Attempt to activate non-visible skill: %s", name)
            return False
        self.active_skills.add(name)
        return True

    def is_tool_active(self, server_id: str, tool_name: str) -> bool:
        """True if *tool_name* from *server_id* is unlocked via an active skill."""
        for skill_name in self.active_skills:
            bundle = self.skills.get(skill_name)
            if bundle is None:
                continue
            for mcp_entry in bundle.manifest.mcp_servers:
                if mcp_entry.get(
                    "server_id"
                ) == server_id and tool_name in mcp_entry.get("tools", []):
                    return True
        return False
