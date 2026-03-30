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
from typing import Dict, List, Optional, Set, Any

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
    mcp_servers: List[Dict[str, Any]] = []
    scripts: Optional[List[Dict[str, Any]]] = []
    activation: Dict[str, Any] = {}


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
        self.skills: Dict[str, SkillBundle] = {}
        self.active_skills: Set[str] = set()
        self.broken_skills: Dict[str, str] = {}  # name → broken target path
        self._load()
        self._auto_activate_core()

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------

    def _load(self):
        self.skills.clear()
        self.broken_skills.clear()
        project_local_dir: Optional[Path] = None
        if self.config.project_root is not None:
            project_local_dir = self.config.project_root / ".gearcore" / "skills"

        for skills_dir in self.config.skills_dirs:
            is_local = (project_local_dir is not None and skills_dir == project_local_dir)
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
                    skill_path.name, target,
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
                with open(manifest_file, "r", encoding="utf-8") as f:
                    manifest = SkillManifest(**json.load(f))
            except Exception as exc:
                logger.error("Bad manifest at %s: %s", manifest_file, exc)
                return
        else:
            manifest = SkillManifest(name=skill_path.name, description="")

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
        logger.debug("Loaded %sskill: %s", "project-local " if is_project_local else "", manifest.name)

    def _auto_activate_core(self):
        for name in self.config.disclosure.core_skills:
            if name in self.visible_skill_names:
                self.activate_skill(name)
                logger.info("Auto-activated core skill: %s", name)

    # ------------------------------------------------------------------
    # Visibility
    # ------------------------------------------------------------------

    @property
    def visible_skill_names(self) -> Set[str]:
        """
        Names the current context is allowed to see.

        - No project: all global skills visible, no project-locals
        - With project + allowlist: only allowlisted globals + all project-locals
        - With project + no allowlist key: all globals + all project-locals
        """
        result: Set[str] = set()
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

    def list_available_skills(self) -> List[Dict[str, str]]:
        visible = self.visible_skill_names
        result = [
            {
                "name": s.manifest.name,
                "description": s.manifest.description,
                "category": s.manifest.category,
                "scope": "project" if s.is_project_local else "global",
                "status": "active" if s.manifest.name in self.active_skills else "available",
            }
            for name, s in self.skills.items()
            if name in visible
        ]
        # Append broken symlinks so users are aware and can fix them
        for name, target in self.broken_skills.items():
            result.append({
                "name": name,
                "description": f"BROKEN SYMLINK → {target}",
                "category": "broken",
                "scope": "unknown",
                "status": "broken",
            })
        return result

    def get_skill(self, name: str) -> Optional[SkillBundle]:
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
                if mcp_entry.get("server_id") == server_id:
                    if tool_name in mcp_entry.get("tools", []):
                        return True
        return False
