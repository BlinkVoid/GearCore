"""
Sync command — install the GearCore self-skill into AI CLI tool discovery paths.

Canonical location:  ~/.config/agents/skills/gearcore/  (owns the actual files)
Symlinked from:      ~/.claude/skills/gearcore/
                     ~/.codex/skills/gearcore/
                     ~/.kimi/skills/gearcore/
                     ~/.config/opencode/skills/gearcore/

Kimi already scans ~/.config/agents/skills/ as its highest-priority user path,
so no extra step is needed for kimi beyond the canonical install.

OpenCode scans {skill,skills}/**/SKILL.md under its config dir
(~/.config/opencode/); it also reads ~/.claude/skills/ unless the user has
disabled Claude Code skill discovery, so the dedicated symlink keeps GearCore
visible either way.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

logger = logging.getLogger("gearcore.sync")

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

CANONICAL_DIR = Path.home() / ".config" / "agents" / "skills" / "gearcore"

TOOL_LINK_PATHS: dict[str, Path] = {
    "claude": Path.home() / ".claude" / "skills" / "gearcore",
    "codex": Path.home() / ".codex" / "skills" / "gearcore",
    "kimi": Path.home() / ".kimi" / "skills" / "gearcore",
    "opencode": Path.home() / ".config" / "opencode" / "skills" / "gearcore",
}

# Self-skill source lives next to this file inside the package
SELF_SKILL_SOURCE = Path(__file__).parent / "self_skill"


# ---------------------------------------------------------------------------
# Level-0 disclosure embedding
# ---------------------------------------------------------------------------


def embed_level0_section(
    skill_md: Path, global_config_path: Path | None = None
) -> bool:
    """
    Replace the LEVEL0 marker in *skill_md* with the default-skills section
    generated from the global config. Global scope only — the canonical
    self-skill is shared by every project. Returns True if the file changed.
    """
    from gearcore_hub.config import EffectiveConfig, load_global_config
    from gearcore_hub.render import (
        LEVEL0_MARKER,
        apply_level0_marker,
        render_level0_section,
    )
    from gearcore_hub.skill_manager import SkillManager

    content = skill_md.read_text(encoding="utf-8")
    if LEVEL0_MARKER not in content:
        return False

    global_cfg = load_global_config(global_config_path)
    effective = EffectiveConfig(global_cfg, None, None)
    sm = SkillManager(effective)
    section = render_level0_section(global_cfg.disclosure.core_skills, sm.skills)

    new_content = apply_level0_marker(content, section)
    if new_content == content:
        return False
    skill_md.write_text(new_content, encoding="utf-8")
    return True


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


def _detect_installed_tools() -> list[str]:
    """Return names of AI CLI tools currently installed on PATH."""
    installed = []
    for tool in TOOL_LINK_PATHS:
        if shutil.which(tool) or shutil.which(f"{tool}-cli"):
            installed.append(tool)
    return installed


# ---------------------------------------------------------------------------
# Core operations
# ---------------------------------------------------------------------------


def _install_canonical(dry_run: bool = False) -> bool:
    """
    Copy self-skill bundle to canonical location.
    Returns True if an action was taken (or would be taken in dry_run).
    """
    if not SELF_SKILL_SOURCE.exists():
        logger.error("Self-skill source not found at %s", SELF_SKILL_SOURCE)
        return False

    if CANONICAL_DIR.exists() or CANONICAL_DIR.is_symlink():
        logger.info("Canonical skill already exists at %s — updating", CANONICAL_DIR)
        if not dry_run:
            if CANONICAL_DIR.is_symlink():
                CANONICAL_DIR.unlink()
            else:
                shutil.rmtree(CANONICAL_DIR)
    else:
        logger.info("Installing canonical skill → %s", CANONICAL_DIR)

    if not dry_run:
        CANONICAL_DIR.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(SELF_SKILL_SOURCE, CANONICAL_DIR)

        if embed_level0_section(CANONICAL_DIR / "SKILL.md"):
            logger.info("Embedded level-0 default-skills section into canonical SKILL.md")

    return True


def _link_tool(tool: str, dry_run: bool = False) -> Path | None:
    """
    Create a symlink from a tool's skills dir to the canonical location.
    Returns the link path, or None if no action was needed.
    """
    link = TOOL_LINK_PATHS[tool]

    if link.is_symlink():
        if link.resolve() == CANONICAL_DIR.resolve():
            logger.debug("[%s] symlink already correct, skipping", tool)
            return None
        logger.info("[%s] updating existing symlink → %s", tool, CANONICAL_DIR)
        if not dry_run:
            link.unlink()
    elif link.exists():
        logger.info("[%s] replacing directory with symlink → %s", tool, CANONICAL_DIR)
        if not dry_run:
            shutil.rmtree(link)
    else:
        logger.info("[%s] creating symlink → %s", tool, CANONICAL_DIR)

    if not dry_run:
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(CANONICAL_DIR)

    return link


def _remove_link(tool: str, dry_run: bool = False) -> bool:
    link = TOOL_LINK_PATHS[tool]
    if link.is_symlink():
        logger.info("[%s] removing symlink %s", tool, link)
        if not dry_run:
            link.unlink()
        return True
    elif link.exists():
        logger.warning(
            "[%s] %s is a real directory, not a symlink — skipping removal "
            "(remove manually if intended)",
            tool,
            link,
        )
    return False


def _remove_canonical(dry_run: bool = False) -> bool:
    if not CANONICAL_DIR.exists() and not CANONICAL_DIR.is_symlink():
        logger.info("Canonical skill not installed, nothing to remove")
        return False
    logger.info("Removing canonical skill at %s", CANONICAL_DIR)
    if not dry_run:
        if CANONICAL_DIR.is_symlink():
            CANONICAL_DIR.unlink()
        else:
            shutil.rmtree(CANONICAL_DIR)
    return True


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def sync(
    tools: list[str] | None = None,
    dry_run: bool = False,
    remove: bool = False,
) -> dict[str, str]:
    """
    Install or remove the GearCore self-skill on AI CLI tools.

    Args:
        tools:   Explicit list of tool names to target. None = auto-detect.
        dry_run: Print what would happen without making changes.
        remove:  Unlink instead of install.

    Returns:
        Dict of tool → result string for display.
    """
    prefix = "[DRY RUN] " if dry_run else ""

    if remove:
        results: dict[str, str] = {}
        target_tools = tools or list(TOOL_LINK_PATHS.keys())
        for tool in target_tools:
            if tool not in TOOL_LINK_PATHS:
                results[tool] = "unknown tool"
                continue
            acted = _remove_link(tool, dry_run=dry_run)
            results[tool] = f"{prefix}removed" if acted else f"{prefix}not linked"
        _remove_canonical(dry_run=dry_run)
        results["canonical"] = f"{prefix}removed"
        return results

    # --- Install ---
    target_tools = tools or _detect_installed_tools()

    if not target_tools:
        logger.warning(
            "No AI CLI tools detected on PATH (claude, codex, kimi, opencode). "
            "Use --tool <name> to force install."
        )

    results = {}

    # 1. Install canonical
    ok = _install_canonical(dry_run=dry_run)
    results["canonical"] = f"{prefix}installed" if ok else "error (see logs)"

    # 2. Symlink each tool
    for tool in target_tools:
        if tool not in TOOL_LINK_PATHS:
            results[tool] = "unknown tool"
            continue
        link = _link_tool(tool, dry_run=dry_run)
        results[tool] = f"{prefix}linked" if link is not None else "already linked"

    # kimi reads ~/.config/agents/skills/ natively — note it even if not in target_tools
    if "kimi" not in results and shutil.which("kimi"):
        results["kimi"] = "covered by canonical path (no symlink needed)"

    return results
