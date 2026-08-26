"""
Rendering helpers for skill instructions and level-0 disclosure.

Shared by the request-skill and list-skills CLI commands and by sync,
so the different surfaces cannot drift apart.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping

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


# ---------------------------------------------------------------------------
# Level-0 (default-reveal) disclosure
# ---------------------------------------------------------------------------

LEVEL0_MARKER = "<!-- GEARCORE:LEVEL0 -->"


def render_level0_section(
    core_skills: list[str], skills: Mapping[str, SkillBundle]
) -> str:
    """
    Markdown 'Default skills' section for the synced self-skill SKILL.md.
    Returns "" when no listed skill is registered.
    """
    bullets = []
    for name in core_skills:
        bundle = skills.get(name)
        if bundle is None:
            logger.warning("core_skills entry '%s' is not a registered skill", name)
            continue
        bullets.append(
            f"- **{name}** — {bundle.manifest.description}\n"
            f"  Load with: `gearcore request-skill {name}`"
        )
    if not bullets:
        return ""
    return (
        "## Default skills — always relevant\n\n"
        "These level-0 skills are revealed by default. `gearcore list-skills` prints\n"
        "their full instructions inline; load and follow them whenever their topic\n"
        "applies, before other project work:\n\n" + "\n".join(bullets) + "\n"
    )


def apply_level0_marker(content: str, section: str) -> str:
    """Replace the marker with *section*, or drop the marker line when empty."""
    if LEVEL0_MARKER not in content:
        return content
    if not section:
        lines = [line for line in content.splitlines() if line.strip() != LEVEL0_MARKER]
        return "\n".join(lines) + ("\n" if content.endswith("\n") else "")
    return content.replace(LEVEL0_MARKER, section)
