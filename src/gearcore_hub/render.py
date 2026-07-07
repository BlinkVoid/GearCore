"""
Rendering helpers for skill instructions and level-0 disclosure.

Shared by the request-skill and list-skills CLI commands and by sync,
so the different surfaces cannot drift apart.
"""

from __future__ import annotations

import logging

from gearcore_hub.skill_manager import SkillBundle

logger = logging.getLogger("gearcore.render")


def render_skill_instructions(bundle: SkillBundle) -> str:
    """Full text an AI needs to use a skill: SKILL.md + `gearcore call` lines."""
    parts = [bundle.instructions]
    if bundle.manifest.mcp_servers:
        lines = ["", "---", "", "## Available tools (via `gearcore call`)", ""]
        for mcp_entry in bundle.manifest.mcp_servers:
            server_id = mcp_entry.get("server_id", "")
            for tool in mcp_entry.get("tools", []):
                lines.append(f"  gearcore call {server_id} {tool} '<json_args>'")
        parts.append("\n".join(lines))
    return "\n".join(parts)
