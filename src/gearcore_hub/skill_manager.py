import os
import json
import logging
import yaml
from pathlib import Path
from typing import Dict, List, Optional, Set, Any
from pydantic import BaseModel

logger = logging.getLogger("gearcore.skill_manager")

class SkillManifest(BaseModel):
    name: str
    version: str
    description: str
    category: str
    mcp_servers: List[Dict[str, Any]]
    scripts: Optional[List[Dict[str, Any]]] = []
    activation: Dict[str, Any]

class SkillBundle:
    """Represents a single Agent Skill bundle."""
    def __init__(self, path: Path, manifest: SkillManifest, instructions: str):
        self.path = path
        self.manifest = manifest
        self.instructions = instructions
        self.active = False

class SkillManager:
    """Manages loading and discovery of Skill Bundles."""
    def __init__(self, skills_dir: str):
        self.skills_dir = Path(skills_dir)
        self.skills: Dict[str, SkillBundle] = {}
        self.active_skills: Set[str] = set()
        self.refresh_skills()

    def refresh_skills(self):
        """Scan the skills directory for valid bundles."""
        if not self.skills_dir.exists():
            logger.warning(f"Skills directory not found: {self.skills_dir}")
            return

        self.skills.clear()
        for skill_path in self.skills_dir.iterdir():
            if skill_path.is_dir():
                manifest_file = skill_path / "manifest.json"
                instructions_file = skill_path / "SKILL.md"
                
                if manifest_file.exists() and instructions_file.exists():
                    try:
                        with open(manifest_file, "r") as f:
                            manifest_data = json.load(f)
                            manifest = SkillManifest(**manifest_data)
                        
                        with open(instructions_file, "r", encoding="utf-8") as f:
                            instructions = f.read()
                        
                        self.skills[manifest.name] = SkillBundle(
                            path=skill_path,
                            manifest=manifest,
                            instructions=instructions
                        )
                        logger.info(f"Loaded skill bundle: {manifest.name}")
                    except Exception as e:
                        logger.error(f"Failed to load skill bundle at {skill_path}: {e}")

    def list_available_skills(self) -> List[Dict[str, str]]:
        """Return a list of available skill summaries for the discovery layer."""
        return [
            {
                "name": s.manifest.name,
                "description": s.manifest.description,
                "category": s.manifest.category,
                "status": "active" if s.manifest.name in self.active_skills else "available"
            }
            for s in self.skills.values()
        ]

    def get_skill(self, name: str) -> Optional[SkillBundle]:
        return self.skills.get(name)

    def activate_skill(self, name: str) -> bool:
        """Unlock a skill for the current session."""
        if name in self.skills:
            self.active_skills.add(name)
            return True
        return False

    def is_tool_active(self, server_id: str, tool_name: str) -> bool:
        """Check if a specific tool from a specific server is currently unlocked."""
        # Always allow core tools (if we define any later)
        
        for skill_name in self.active_skills:
            skill = self.skills[skill_name]
            for mcp_server in skill.manifest.mcp_servers:
                if mcp_server["server_id"] == server_id:
                    if tool_name in mcp_server["tools"]:
                        return True
        return False
