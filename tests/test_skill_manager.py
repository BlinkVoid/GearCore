"""Tests for the skill manager."""

import tempfile
from pathlib import Path

from gearcore_hub.config import EffectiveConfig, GlobalConfig, ProjectConfig
from gearcore_hub.profiles import ProfileConfig
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


def _v3_skills_config(skills_dir: Path) -> GlobalConfig:
    return GlobalConfig(
        version=3,
        registry={"skills_dirs": [str(skills_dir)]},
        profiles={
            "default": "operator",
            "entries": {
                "operator": {
                    "scope": {
                        "skills": {
                            "include": [
                                "hive-dispatcher",
                                "safe-skill",
                                "legacy-skill",
                            ],
                            "deny": ["legacy-skill"],
                            "protected": ["hive-dispatcher"],
                        }
                    }
                },
                "hive-worker": {
                    "constrained": True,
                    "scope": {
                        "skills": {
                            "include": ["hive-worker", "hive-dispatcher"],
                            "deny": ["hive-dispatcher"],
                        }
                    },
                },
            },
        },
    )


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

    def test_protected_global_skill_survives_v2_project_collision(self, tmp_path):
        global_skills = tmp_path / "global-skills"
        project_root = tmp_path / "project"
        local_skills = project_root / ".gearcore" / "skills"
        trusted = _make_skill_dir(global_skills, "hive-dispatcher")
        _make_skill_dir(global_skills, "safe-skill")
        _make_skill_dir(global_skills, "legacy-skill")
        _make_skill_dir(local_skills, "hive-dispatcher")
        _make_skill_dir(local_skills, "legacy-skill")
        project_cfg = ProjectConfig(
            version=2,
            scope={
                "skills": {
                    "include": ["safe-skill"],
                    "deny": ["hive-dispatcher", "legacy-skill"],
                }
            },
        )

        effective = EffectiveConfig(
            _v3_skills_config(global_skills), project_cfg, project_root
        )
        manager = SkillManager(effective)

        dispatcher = manager.get_skill("hive-dispatcher")
        assert dispatcher is not None
        assert dispatcher.path == trusted
        assert dispatcher.is_project_local is False
        assert manager.get_skill("legacy-skill") is None
        assert effective.diagnostic_codes == ("protected_capability_override",)

    def test_worker_profile_cannot_see_denied_dispatcher_skill(self, tmp_path):
        global_skills = tmp_path / "global-skills"
        project_root = tmp_path / "project"
        local_skills = project_root / ".gearcore" / "skills"
        _make_skill_dir(global_skills, "hive-dispatcher")
        _make_skill_dir(global_skills, "hive-worker")
        _make_skill_dir(local_skills, "hive-dispatcher")

        effective = EffectiveConfig(
            _v3_skills_config(global_skills),
            ProjectConfig(version=2),
            project_root,
            profile_name="hive-worker",
        )
        manager = SkillManager(effective)

        assert manager.get_skill("hive-dispatcher") is None
        worker = manager.get_skill("hive-worker")
        assert worker is not None
        assert worker.is_project_local is False

    def test_protected_skill_collision_alone_records_diagnostic(self, tmp_path):
        global_skills = tmp_path / "global-skills"
        project_root = tmp_path / "project"
        local_skills = project_root / ".gearcore" / "skills"
        _make_skill_dir(global_skills, "hive-dispatcher")
        _make_skill_dir(local_skills, "hive-dispatcher")
        effective = EffectiveConfig(
            _v3_skills_config(global_skills),
            ProjectConfig(
                version=2,
                scope={"skills": {"include": ["hive-dispatcher"]}},
            ),
            project_root,
        )

        manager = SkillManager(effective)

        assert effective.diagnostic_codes == ("protected_capability_override",)
        assert manager.diagnostic_codes == ("protected_capability_override",)

    def test_v3_project_include_filters_project_local_nonprotected_skill(
        self, tmp_path
    ):
        global_skills = tmp_path / "global-skills"
        project_root = tmp_path / "project"
        local_skills = project_root / ".gearcore" / "skills"
        _make_skill_dir(global_skills, "safe-skill")
        _make_skill_dir(local_skills, "project-only")
        effective = EffectiveConfig(
            _v3_skills_config(global_skills),
            ProjectConfig(
                version=3,
                profiles={
                    "entries": {
                        "operator": {
                            "scope": {"skills": {"include": ["safe-skill"]}}
                        }
                    }
                },
            ),
            project_root,
        )

        manager = SkillManager(effective)

        assert "safe-skill" in manager.visible_skill_names
        assert "project-only" not in manager.visible_skill_names

    def test_effective_config_snapshots_selected_project_overlay(self, tmp_path):
        global_skills = tmp_path / "global-skills"
        project_root = tmp_path / "project"
        local_skills = project_root / ".gearcore" / "skills"
        _make_skill_dir(global_skills, "safe-skill")
        _make_skill_dir(local_skills, "project-only")
        project_cfg = ProjectConfig(
            version=3,
            profiles={
                "entries": {
                    "operator": {
                        "scope": {"skills": {"include": ["safe-skill"]}}
                    }
                }
            },
        )
        effective = EffectiveConfig(
            _v3_skills_config(global_skills), project_cfg, project_root
        )
        assert project_cfg.profiles is not None
        project_cfg.profiles.entries["operator"] = ProfileConfig.model_validate(
            {"scope": {"skills": {"include": ["project-only"]}}}
        )
        assert effective.project_cfg is not None
        assert effective.project_cfg.profiles is not None
        effective.project_cfg.profiles.entries[
            "operator"
        ] = ProfileConfig.model_validate(
            {"scope": {"skills": {"include": ["project-only"]}}}
        )

        manager = SkillManager(effective)

        assert "safe-skill" in manager.visible_skill_names
        assert "project-only" not in manager.visible_skill_names

    def test_constrained_profile_rejects_project_local_skill_expansion(
        self, tmp_path
    ):
        global_skills = tmp_path / "global-skills"
        project_root = tmp_path / "project"
        local_skills = project_root / ".gearcore" / "skills"
        _make_skill_dir(global_skills, "hive-worker")
        _make_skill_dir(local_skills, "shell-root")
        effective = EffectiveConfig(
            _v3_skills_config(global_skills),
            ProjectConfig(
                version=2,
                scope={"skills": {"include": ["hive-worker", "shell-root"]}},
            ),
            project_root,
            profile_name="hive-worker",
        )

        manager = SkillManager(effective)

        assert manager.visible_skill_names == {"hive-worker"}


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
