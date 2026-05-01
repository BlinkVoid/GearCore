"""Tests for the skill manager."""

import tempfile
from pathlib import Path

from gearcore_hub.config import EffectiveConfig, GlobalConfig, ProjectConfig
from gearcore_hub.skill_manager import SkillManager


def _make_skill_dir(base: Path, name: str, manifest: dict = None) -> Path:
    skill_dir = base / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: test\n---\n\n# {name}"
    )
    if manifest:
        import json

        (skill_dir / "manifest.json").write_text(json.dumps(manifest))
    return skill_dir


class TestVisibility:
    def test_global_skills_visible_without_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            skills_dir = Path(tmp) / "skills"
            _make_skill_dir(skills_dir, "skill-a")

            global_cfg = GlobalConfig(registry={"skills_dirs": [str(skills_dir)]})
            effective = EffectiveConfig(global_cfg, None, None)
            sm = SkillManager(effective)

            assert "skill-a" in sm.visible_skill_names
            assert sm.get_skill("skill-a") is not None

    def test_project_locals_hidden_without_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "myproject"
            local_skills = project_root / ".gearcore" / "skills"
            _make_skill_dir(local_skills, "local-skill")

            global_cfg = GlobalConfig()
            project_cfg = ProjectConfig()
            effective = EffectiveConfig(global_cfg, project_cfg, project_root)
            sm = SkillManager(effective)

            # With project context, local skills ARE visible
            assert "local-skill" in sm.visible_skill_names

    def test_project_locals_hidden_without_project_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "myproject"
            local_skills = project_root / ".gearcore" / "skills"
            _make_skill_dir(local_skills, "local-skill")

            global_cfg = GlobalConfig()
            effective = EffectiveConfig(global_cfg, None, None)
            sm = SkillManager(effective)

            # Without project context, local skills are NOT visible
            assert "local-skill" not in sm.visible_skill_names

    def test_global_skills_filtered_by_allowlist(self):
        with tempfile.TemporaryDirectory() as tmp:
            skills_dir = Path(tmp) / "skills"
            _make_skill_dir(skills_dir, "allowed")
            _make_skill_dir(skills_dir, "blocked")

            global_cfg = GlobalConfig(registry={"skills_dirs": [str(skills_dir)]})
            project_cfg = ProjectConfig(scope={"skills": {"include": ["allowed"]}})
            effective = EffectiveConfig(global_cfg, project_cfg, Path(tmp))
            sm = SkillManager(effective)

            assert "allowed" in sm.visible_skill_names
            assert "blocked" not in sm.visible_skill_names


class TestActivation:
    def test_activation_makes_skill_active(self):
        with tempfile.TemporaryDirectory() as tmp:
            skills_dir = Path(tmp) / "skills"
            _make_skill_dir(skills_dir, "skill-a")

            global_cfg = GlobalConfig(registry={"skills_dirs": [str(skills_dir)]})
            effective = EffectiveConfig(global_cfg, None, None)
            sm = SkillManager(effective)

            assert sm.activate_skill("skill-a") is True
            assert "skill-a" in sm.active_skills
            available = sm.list_available_skills()
            assert any(
                s["name"] == "skill-a" and s["status"] == "active" for s in available
            )

    def test_cannot_activate_non_visible_skill(self):
        with tempfile.TemporaryDirectory():
            global_cfg = GlobalConfig()
            effective = EffectiveConfig(global_cfg, None, None)
            sm = SkillManager(effective)

            assert sm.activate_skill("missing") is False

    def test_core_skills_auto_activated(self):
        with tempfile.TemporaryDirectory() as tmp:
            skills_dir = Path(tmp) / "skills"
            _make_skill_dir(skills_dir, "core-reasoning")

            global_cfg = GlobalConfig(
                registry={"skills_dirs": [str(skills_dir)]},
                disclosure={"core_skills": ["core-reasoning"]},
            )
            effective = EffectiveConfig(global_cfg, None, None)
            sm = SkillManager(effective)

            assert "core-reasoning" in sm.active_skills


class TestToolActivation:
    def test_tool_active_when_skill_active(self):
        with tempfile.TemporaryDirectory() as tmp:
            skills_dir = Path(tmp) / "skills"
            _make_skill_dir(
                skills_dir,
                "web-research",
                manifest={
                    "name": "web-research",
                    "mcp_servers": [
                        {"server_id": "playwright", "tools": ["browser_navigate"]}
                    ],
                },
            )

            global_cfg = GlobalConfig(registry={"skills_dirs": [str(skills_dir)]})
            effective = EffectiveConfig(global_cfg, None, None)
            sm = SkillManager(effective)
            sm.activate_skill("web-research")

            assert sm.is_tool_active("playwright", "browser_navigate") is True
            assert sm.is_tool_active("playwright", "other_tool") is False
            assert sm.is_tool_active("other_server", "browser_navigate") is False
