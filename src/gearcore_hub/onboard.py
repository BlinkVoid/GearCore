from __future__ import annotations

import argparse
import dataclasses
import filecmp
import logging
import os
import tomllib
from collections import Counter
from pathlib import Path
from typing import Any

import yaml

from gearcore_hub.plugin import (
    PluginManifest,
    discover_support_components,
    load_plugin_manifest,
)
from gearcore_hub.sync import sync

logger = logging.getLogger("gearcore.onboard")


@dataclasses.dataclass(frozen=True)
class SkillBundleCandidate:
    name: str
    path: Path


@dataclasses.dataclass(frozen=True)
class OnboardStep:
    action: str
    target: str
    detail: str


@dataclasses.dataclass
class OnboardPlan:
    core_path: Path
    scope: str
    project_root: Path | None
    mcp_script: str | None
    mcp_id: str | None
    mcp_command: str | None
    mcp_action: str
    skill_actions: list[tuple[SkillBundleCandidate, str]]
    copy_skills: bool
    dry_run: bool
    tool_filter: list[str] | None
    no_sync: bool
    mcp_existing: dict[str, Any] | None = None
    plugin: PluginManifest | None = None
    plugin_action: str = "none"
    plugin_dest: Path | None = None
    plugin_support: list[str] = dataclasses.field(default_factory=list)


def discover_skill_bundles(core_path: Path) -> list[SkillBundleCandidate]:
    candidates: list[SkillBundleCandidate] = []
    skills_root = core_path / "skills"
    if skills_root.is_dir():
        for entry in sorted(skills_root.iterdir()):
            if not entry.is_dir():
                continue
            if (entry / "SKILL.md").exists():
                candidates.append(
                    SkillBundleCandidate(name=_extract_skill_name(entry), path=entry)
                )

    if (core_path / "SKILL.md").exists():
        candidates.append(
            SkillBundleCandidate(name=_extract_skill_name(core_path), path=core_path)
        )

    return candidates


def discover_mcp_scripts(core_path: Path) -> list[str]:
    path = core_path / "pyproject.toml"
    if not path.exists():
        return []

    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except Exception as exc:
        raise ValueError(f"Failed to parse {path}: {exc}") from exc

    scripts = data.get("project", {}).get("scripts", {})
    if not isinstance(scripts, dict):
        return []
    return sorted(
        name
        for name in scripts
        if isinstance(name, str) and (name.endswith("-mcp") or name.endswith("_mcp"))
    )


def select_mcp_script(scripts: list[str], requested_script: str | None = None) -> str:
    if requested_script is None:
        if len(scripts) == 1:
            return scripts[0]
        if not scripts:
            raise ValueError("No MCP script candidates discovered.")
        raise ValueError(
            "Multiple MCP script candidates discovered: "
            + ", ".join(scripts)
            + ". Use --mcp-script to choose one."
        )
    if requested_script not in scripts:
        raise ValueError(
            f"Requested MCP script '{requested_script}' not found. Candidates: {', '.join(scripts)}"
        )
    return requested_script


def derive_mcp_id(
    skill_bundles: list[SkillBundleCandidate],
    mcp_script: str,
    explicit_id: str | None = None,
) -> str:
    if explicit_id:
        return explicit_id
    if len(skill_bundles) == 1:
        return skill_bundles[0].name
    for suffix in ("-mcp", "_mcp"):
        if mcp_script.endswith(suffix):
            return mcp_script[: -len(suffix)]
    return mcp_script


def mcp_command(core_path: Path, mcp_script: str) -> str:
    local_exec = core_path / ".venv" / "bin" / mcp_script
    if local_exec.is_file() and os.access(local_exec, os.X_OK):
        return str(local_exec)
    return mcp_script


def build_onboard_plan(
    core_path: Path,
    *,
    scope: str = "global",
    project_root: Path | None = None,
    mcp_script: str | None = None,
    mcp_id: str | None = None,
    copy_skills: bool = False,
    tool_filter: list[str] | None = None,
    no_sync: bool = False,
) -> OnboardPlan:
    core_path = core_path.resolve()
    plugin = load_plugin_manifest(core_path)
    if plugin is not None:
        return _build_plugin_plan(
            core_path,
            plugin,
            scope=scope,
            project_root=project_root,
            copy_skills=copy_skills,
            tool_filter=tool_filter,
            no_sync=no_sync,
        )
    skill_bundles = discover_skill_bundles(core_path)
    skill_names = Counter(bundle.name for bundle in skill_bundles)
    duplicate_names = sorted(name for name, count in skill_names.items() if count > 1)
    if duplicate_names:
        raise ValueError(
            "Duplicate skill names discovered: " + ", ".join(duplicate_names)
        )
    scripts = discover_mcp_scripts(core_path)
    if not skill_bundles and not scripts:
        raise ValueError(f"No skill bundles or MCP scripts discovered in {core_path}.")
    if not scripts and (mcp_script is not None or mcp_id is not None):
        raise ValueError(
            "Cannot use --mcp-script or --mcp-id: no MCP scripts ending -mcp/_mcp "
            f"were discovered in {core_path}/pyproject.toml."
        )

    selected_script: str | None = None
    selected_id: str | None = None
    selected_command: str | None = None
    mcp_action = "none"
    mcp_existing: dict[str, Any] | None = None
    if scripts:
        selected_script = select_mcp_script(scripts, requested_script=mcp_script)
        selected_id = derive_mcp_id(skill_bundles, selected_script, explicit_id=mcp_id)
        selected_command = mcp_command(core_path, selected_script)
        mcp_action, mcp_existing = _plan_mcp_registration(
            scope=scope,
            project_root=project_root,
            mcp_id=selected_id,
            command=selected_command,
        )
    skill_actions = _plan_skills(
        scope=scope,
        project_root=project_root,
        skill_bundles=skill_bundles,
    )

    if mcp_action == "conflict":
        raise ValueError(f"Conflicting MCP entry for '{selected_id}': {mcp_existing}")
    conflicts = [name.name for name, action in skill_actions if action == "conflict"]
    if conflicts:
        raise ValueError(
            "Skill destination conflicts: "
            + ", ".join(conflicts)
            + ". Remove/rename those destinations before onboarding."
        )

    return OnboardPlan(
        core_path=core_path.resolve(),
        scope=scope,
        project_root=project_root,
        mcp_script=selected_script,
        mcp_id=selected_id,
        mcp_command=selected_command,
        mcp_action=mcp_action,
        skill_actions=skill_actions,
        copy_skills=copy_skills,
        dry_run=False,
        tool_filter=tool_filter,
        no_sync=no_sync,
        mcp_existing=mcp_existing,
    )


def _build_plugin_plan(
    core_path: Path,
    plugin: PluginManifest,
    *,
    scope: str,
    project_root: Path | None,
    copy_skills: bool,
    tool_filter: list[str] | None,
    no_sync: bool,
) -> OnboardPlan:
    plugins_root = _plugins_dir(scope, project_root)
    plugin_dest = plugins_root / plugin.name
    if plugin_dest.exists() or plugin_dest.is_symlink():
        if not _is_skill_equivalent(core_path, plugin_dest):
            raise ValueError(
                f"Plugin destination conflicts: {plugin_dest} already exists and "
                f"does not match {core_path}. Remove/rename it before onboarding."
            )
        plugin_action = "skip"
    else:
        plugin_action = "add"

    support = discover_support_components(core_path)

    skill_actions: list[tuple[SkillBundleCandidate, str]] = []
    if plugin.skills_dir.is_dir():
        # Scan the declared skills dir directly: unlike discover_skill_bundles
        # on the plugin root, this never treats the plugin root itself as a skill.
        skill_bundles = [
            SkillBundleCandidate(name=_extract_skill_name(entry), path=entry)
            for entry in sorted(plugin.skills_dir.iterdir())
            if entry.is_dir() and (entry / "SKILL.md").exists()
        ]
        counts = Counter(bundle.name for bundle in skill_bundles)
        duplicates = sorted(name for name, count in counts.items() if count > 1)
        if duplicates:
            raise ValueError(
                "Duplicate skill names discovered: " + ", ".join(duplicates)
            )
        skills_root = _skills_root(scope, project_root)
        conflicts: list[str] = []
        for bundle in skill_bundles:
            dest = _validated_skill_dest(bundle.name, skills_root)
            installed = _installed_plugin_skill_source(plugin_dest, plugin, bundle)
            if not dest.exists() and not dest.is_symlink():
                skill_actions.append((bundle, "add"))
            elif _is_skill_equivalent(installed, dest):
                skill_actions.append((bundle, "skip"))
            else:
                conflicts.append(bundle.name)
        if conflicts:
            raise ValueError(
                "Skill destination conflicts: "
                + ", ".join(conflicts)
                + ". Remove/rename those destinations before onboarding."
            )

    return OnboardPlan(
        core_path=core_path,
        scope=scope,
        project_root=project_root,
        mcp_script=None,
        mcp_id=None,
        mcp_command=None,
        mcp_action="none",
        skill_actions=skill_actions,
        copy_skills=copy_skills,
        dry_run=False,
        tool_filter=tool_filter,
        no_sync=no_sync,
        plugin=plugin,
        plugin_action=plugin_action,
        plugin_dest=plugin_dest,
        plugin_support=support,
    )


def run_onboard(
    core_path: Path,
    *,
    scope: str = "global",
    project_root: Path | None = None,
    mcp_script: str | None = None,
    mcp_id: str | None = None,
    copy_skills: bool = False,
    dry_run: bool = False,
    tool_filter: list[str] | None = None,
    no_sync: bool = False,
) -> list[OnboardStep]:
    plan = build_onboard_plan(
        core_path,
        scope=scope,
        project_root=project_root,
        mcp_script=mcp_script,
        mcp_id=mcp_id,
        copy_skills=copy_skills,
        tool_filter=tool_filter,
        no_sync=no_sync,
    )

    if dry_run:
        return [
            OnboardStep(
                action="plan",
                target="dry-run",
                detail=_format_plan(plan),
            )
        ]

    steps = apply_plan(plan)
    if not plan.no_sync:
        sync(tools=plan.tool_filter, dry_run=False, remove=False)
    return steps


def apply_plan(plan: OnboardPlan) -> list[OnboardStep]:
    steps: list[OnboardStep] = []

    skills_root = _skills_root(plan.scope, plan.project_root)
    # Keep the mutation phase defensive if a caller hands us a plan that was
    # built before a skill bundle changed on disk.
    for bundle, _ in plan.skill_actions:
        _validated_skill_dest(bundle.name, skills_root)

    if plan.plugin is not None:
        assert plan.plugin_dest is not None
        plugin_dest = plan.plugin_dest
        if plan.plugin_action == "add":
            plugin_dest.parent.mkdir(parents=True, exist_ok=True)
            if plan.copy_skills:
                shutil_copytree(plan.core_path, plugin_dest, preserve_symlinks=True)
                detail = f"copied plugin '{plan.plugin.name}' to {plugin_dest}"
            else:
                plugin_dest.symlink_to(plan.core_path)
                detail = f"symlinked plugin '{plan.plugin.name}' to {plugin_dest}"
            steps.append(
                OnboardStep(action="plugin", target=plan.plugin.name, detail=detail)
            )
        else:
            steps.append(
                OnboardStep(
                    action="plugin",
                    target=plan.plugin.name,
                    detail=(f"skipped plugin '{plan.plugin.name}' (already matches)"),
                )
            )

    if plan.mcp_action == "add":
        assert plan.mcp_id is not None and plan.mcp_command is not None
        _write_mcp_entry(
            scope=plan.scope,
            project_root=plan.project_root,
            mcp_id=plan.mcp_id,
            command=plan.mcp_command,
        )
        steps.append(
            OnboardStep(
                action="mcp",
                target=plan.mcp_id,
                detail=f"registered MCP '{plan.mcp_id}' from {plan.mcp_script}",
            )
        )
    elif plan.mcp_action == "skip":
        assert plan.mcp_id is not None
        steps.append(
            OnboardStep(
                action="mcp",
                target=plan.mcp_id,
                detail=f"skipped MCP '{plan.mcp_id}' (already matches)",
            )
        )

    for bundle, action in plan.skill_actions:
        dest = _validated_skill_dest(bundle.name, skills_root)
        if plan.plugin is not None:
            assert plan.plugin_dest is not None
            # Register through the installed plugin root, never the original leaf.
            source = _installed_plugin_skill_source(
                plan.plugin_dest, plan.plugin, bundle
            )
        else:
            source = bundle.path
        if action == "add":
            if plan.copy_skills and plan.plugin is None:
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil_copytree(source, dest)
                detail = f"copied skill '{bundle.name}' to {dest}"
            else:
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.symlink_to(source)
                detail = f"symlinked skill '{bundle.name}' to {dest}"
            steps.append(OnboardStep(action="skill", target=bundle.name, detail=detail))
        else:
            steps.append(
                OnboardStep(
                    action="skill",
                    target=bundle.name,
                    detail=f"skipped skill '{bundle.name}' (already matches)",
                )
            )
    return steps


def cmd_onboard(args: argparse.Namespace, project_path: Path | None) -> None:
    try:
        steps = run_onboard(
            core_path=Path(args.core_path),
            scope=args.scope,
            project_root=project_path,
            mcp_script=args.mcp_script,
            mcp_id=args.mcp_id,
            copy_skills=args.copy_skills,
            dry_run=args.dry_run,
            tool_filter=args.tool,
            no_sync=args.no_sync,
        )
    except ValueError as exc:
        print(f"Error: {exc}")
        raise SystemExit(1) from exc

    for step in steps:
        print(step.detail)


def _format_plan(plan: OnboardPlan) -> str:
    if plan.plugin is not None:
        return _format_plugin_plan(plan)
    lines = [
        "Plan: onboarding core",
        f"  Core path: {plan.core_path}",
    ]
    if plan.mcp_action == "none":
        lines.append("  MCP: none discovered")
    else:
        lines.extend(
            [
                f"  MCP script: {plan.mcp_script}",
                f"  MCP id: {plan.mcp_id}",
                f"  MCP command: {plan.mcp_command}",
                "  MCP action: add"
                if plan.mcp_action == "add"
                else "  MCP action: skip (already registered)",
            ]
        )

    lines.append("  Skills:")
    for bundle, action in plan.skill_actions:
        lines.append(f"    - {bundle.name}: {action}")

    if plan.no_sync:
        lines.append("  Sync: skipped (--no-sync)")
    elif plan.tool_filter:
        lines.append("  Sync targets: " + ", ".join(plan.tool_filter))
    else:
        lines.append("  Sync targets: auto-detect")
    return "\n".join(lines)


def _format_plugin_plan(plan: OnboardPlan) -> str:
    assert plan.plugin is not None and plan.plugin_dest is not None
    action = "add" if plan.plugin_action == "add" else "skip (already registered)"
    lines = [
        "Plan: onboarding plugin",
        f"  Plugin name: {plan.plugin.name}",
        f"  Plugin root: {plan.core_path}",
        f"  Plugin destination: {plan.plugin_dest}",
        f"  Plugin action: {action}",
    ]
    if plan.plugin_support:
        lines.append(
            "  Support components preserved: " + ", ".join(plan.plugin_support)
        )
    else:
        lines.append("  Support components preserved: (none detected)")

    lines.append("  Skills:")
    for bundle, skill_action in plan.skill_actions:
        lines.append(f"    - {bundle.name}: {skill_action}")

    if plan.no_sync:
        lines.append("  Sync: skipped (--no-sync)")
    elif plan.tool_filter:
        lines.append("  Sync targets: " + ", ".join(plan.tool_filter))
    else:
        lines.append("  Sync targets: auto-detect")
    return "\n".join(lines)


def _extract_skill_name(skill_path: Path) -> str:
    text = (skill_path / "SKILL.md").read_text(encoding="utf-8")
    if text.startswith("---\n"):
        parts = text.split("---", 2)
        if len(parts) >= 2:
            frontmatter = yaml.safe_load(parts[1])
            name = frontmatter.get("name") if isinstance(frontmatter, dict) else None
            if isinstance(name, str):
                return name
    return skill_path.name


def _installed_plugin_skill_source(
    plugin_dest: Path,
    plugin: PluginManifest,
    bundle: SkillBundleCandidate,
) -> Path:
    """Return a bundle's installed physical path while retaining its logical name."""
    try:
        relative_path = bundle.path.relative_to(plugin.skills_dir)
    except ValueError as exc:
        raise ValueError(
            f"Plugin skill {bundle.path} is outside declared skills directory "
            f"{plugin.skills_dir}."
        ) from exc
    return plugin_dest / plugin.skills_path / relative_path


# Shared config/skills-path helpers live in registry; onboard uses the same
# global/project resolution rules so both modules can never drift apart.
from gearcore_hub.registry import (  # noqa: E402
    _config_path,
    _plugins_dir,
    _read_yaml,
    _skills_dir,
    _validated_skill_dest,
    _write_yaml,
)

_skills_root = _skills_dir


def _normalize_mcp_entry(entry: dict[str, Any]) -> dict[str, Any]:
    data = {
        "id": entry.get("id"),
        "type": entry.get("type", "stdio"),
        "enabled": entry.get("enabled", True),
    }
    if data["type"] == "stdio":
        data["command"] = entry.get("command", "")
        if entry.get("args"):
            data["args"] = entry["args"]
        if entry.get("env"):
            data["env"] = entry["env"]
    else:
        data["url"] = entry.get("url", "")
    return data


def _plan_mcp_registration(
    scope: str,
    project_root: Path | None,
    mcp_id: str,
    command: str,
) -> tuple[str, dict[str, Any] | None]:
    data = _read_yaml(_config_path(scope, project_root))
    registry_section = data.setdefault("registry", {})
    servers = registry_section.setdefault("mcp_servers", [])
    existing = next((s for s in servers if s.get("id") == mcp_id), None)
    if existing is None:
        return "add", None
    expected = {"id": mcp_id, "type": "stdio", "command": command, "enabled": True}
    if _normalize_mcp_entry(existing) == expected:
        return "skip", existing
    return "conflict", existing


def _plan_skills(
    scope: str,
    project_root: Path | None,
    skill_bundles: list[SkillBundleCandidate],
) -> list[tuple[SkillBundleCandidate, str]]:
    skills_root = _skills_root(scope, project_root)
    out: list[tuple[SkillBundleCandidate, str]] = []
    for bundle in skill_bundles:
        dest = _validated_skill_dest(bundle.name, skills_root)
        if not dest.exists() and not dest.is_symlink():
            out.append((bundle, "add"))
            continue
        if _is_skill_equivalent(bundle.path, dest):
            out.append((bundle, "skip"))
            continue
        out.append((bundle, "conflict"))
    return out


def _is_skill_equivalent(source: Path, dest: Path) -> bool:
    try:
        # Strict: a not-yet-installed source (e.g. plugin pending registration)
        # can never be equivalent to an existing destination.
        source_resolved = source.resolve(strict=True)
    except (FileNotFoundError, OSError):
        return False

    if not dest.exists():
        return False

    if dest.is_symlink():
        try:
            return dest.resolve() == source_resolved
        except OSError:
            return False
    return dest.is_dir() and _dirs_equal(source, dest)


def _dirs_equal(left: Path, right: Path) -> bool:
    cmp = filecmp.dircmp(left, right, ignore=[".git"])
    if cmp.left_only or cmp.right_only or cmp.funny_files or cmp.common_funny:
        return False
    if cmp.diff_files:
        return False
    for filename in cmp.common_files:
        if not filecmp.cmp(left / filename, right / filename, shallow=False):
            return False
    return all(
        _dirs_equal(Path(sub.left), Path(sub.right)) for sub in cmp.subdirs.values()
    )


def _write_mcp_entry(
    scope: str,
    project_root: Path | None,
    mcp_id: str,
    command: str,
) -> None:
    path = _config_path(scope, project_root)
    data = _read_yaml(path)
    registry_section = data.setdefault("registry", {})
    servers = registry_section.setdefault("mcp_servers", [])
    entry = {"id": mcp_id, "type": "stdio", "command": command, "enabled": True}
    for idx, existing in enumerate(servers):
        if existing.get("id") == mcp_id:
            servers[idx] = entry
            _write_yaml(path, data)
            return
    servers.append(entry)
    _write_yaml(path, data)


def shutil_copytree(
    source: Path, dest: Path, *, preserve_symlinks: bool = False
) -> None:
    import shutil

    if dest.exists():
        raise FileExistsError(f"Cannot copy to existing destination {dest}")
    shutil.copytree(source, dest, symlinks=preserve_symlinks)
