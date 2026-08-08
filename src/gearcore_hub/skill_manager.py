"""
Skill manager with two-phase loading and project-scope visibility gating.

Loading sequence:
  Phase 1 — global skills dirs (from EffectiveConfig.skills_dirs, excluding project-local)
  Phase 2 — project-local skills dir (.gearcore/skills/) appended unconditionally

Visibility rules:
  - Global skills: selected by the effective profile and project allowlist
  - Project-local skills: visible in project context unless denied or omitted by
    a version-3 profile overlay
  - Protected global skills: cannot be hidden or replaced by project context
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

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
        self._diagnostic_codes: set[str] = set()
        self._load()
        self._auto_activate_core()

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------

    def _load(self):
        self.skills.clear()
        self.broken_skills.clear()
        self._diagnostic_codes.clear()
        if self.config.diagnostic_only:
            return
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

        # manifest.json is optional — synthesise a minimal one from SKILL.md name
        manifest_file = skill_path / "manifest.json"
        if manifest_file.exists():
            try:
                with open(manifest_file, encoding="utf-8") as f:
                    manifest = SkillManifest(**json.load(f))
            except Exception as exc:
                logger.error("Bad manifest at %s: %s", manifest_file, exc)
                return
        else:
            manifest = SkillManifest(name=skill_path.name, description="")

        if (
            is_project_local
            and manifest.name in self.config.protected_skill_names
        ):
            self._diagnostic_codes.add("protected_capability_override")
            self.config._record_diagnostic_code("protected_capability_override")
            logger.warning(
                "Ignoring project-local override of protected skill: %s",
                manifest.name,
            )
            return

        try:
            instructions = instructions_file.read_text(encoding="utf-8")
        except Exception as exc:
            logger.error("Cannot read SKILL.md at %s: %s", instructions_file, exc)
            return

        bundle = SkillBundle(
            path=skill_path,
            manifest=manifest,
            instructions=instructions,
            is_project_local=is_project_local,
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
        - Version 2 keeps legacy global filtering + project-local visibility
        - Version 3 resolves profile includes, project overlays, and denies
        - Protected globals survive project filtering and collisions
        """
        global_names = tuple(
            name
            for name, bundle in self.skills.items()
            if not bundle.is_project_local
        )
        project_names = tuple(
            name
            for name, bundle in self.skills.items()
            if bundle.is_project_local and self.config.project_root is not None
        )
        resolved = self.config.resolve_skill_capabilities(
            global_names, project_names
        )
        return set(resolved.active)

    @property
    def diagnostic_codes(self) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                (*self.config.diagnostic_codes, *sorted(self._diagnostic_codes))
            )
        )

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
