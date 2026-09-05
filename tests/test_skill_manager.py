"""Tests for the skill manager."""

import shutil
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


def _make_raw_skill_dir(
    base: Path, name: str, skill_md: str, manifest: dict = None
) -> Path:
    skill_dir = base / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(skill_md)
    if manifest is not None:
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


class TestMetadataFallback:
    def test_manifest_less_skill_uses_frontmatter_name_and_description(self):
        with tempfile.TemporaryDirectory() as tmp:
            skills_dir = Path(tmp) / "skills"
            _make_raw_skill_dir(
                skills_dir,
                "dir-name",
                "---\nname: front-name\ndescription: from frontmatter\n---\n\n# body",
            )

            global_cfg = GlobalConfig(registry={"skills_dirs": [str(skills_dir)]})
            effective = EffectiveConfig(global_cfg, None, None)
            sm = SkillManager(effective)

            bundle = sm.get_skill("front-name")
            assert bundle is not None
            assert bundle.manifest.name == "front-name"
            assert bundle.manifest.description == "from frontmatter"
            assert "dir-name" not in sm.skills

    def test_multiline_frontmatter_description(self):
        with tempfile.TemporaryDirectory() as tmp:
            skills_dir = Path(tmp) / "skills"
            _make_raw_skill_dir(
                skills_dir,
                "dir-name",
                "---\nname: multi-desc\ndescription: |\n  First line.\n  Second line.\n---\n\nbody",
            )

            global_cfg = GlobalConfig(registry={"skills_dirs": [str(skills_dir)]})
            effective = EffectiveConfig(global_cfg, None, None)
            sm = SkillManager(effective)

            bundle = sm.get_skill("multi-desc")
            assert bundle is not None
            assert bundle.manifest.description == "First line.\nSecond line.\n"

    def test_malformed_frontmatter_falls_back_to_path_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            skills_dir = Path(tmp) / "skills"
            _make_raw_skill_dir(
                skills_dir, "dir-name", "---\nname: [unclosed\n---\n\nbody"
            )

            global_cfg = GlobalConfig(registry={"skills_dirs": [str(skills_dir)]})
            effective = EffectiveConfig(global_cfg, None, None)
            sm = SkillManager(effective)

            bundle = sm.get_skill("dir-name")
            assert bundle is not None
            assert bundle.manifest.name == "dir-name"
            assert bundle.manifest.description == ""

    def test_non_mapping_frontmatter_falls_back_to_path_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            skills_dir = Path(tmp) / "skills"
            _make_raw_skill_dir(
                skills_dir, "dir-name", "---\n- just\n- a list\n---\n\nbody"
            )

            global_cfg = GlobalConfig(registry={"skills_dirs": [str(skills_dir)]})
            effective = EffectiveConfig(global_cfg, None, None)
            sm = SkillManager(effective)

            bundle = sm.get_skill("dir-name")
            assert bundle is not None
            assert bundle.manifest.name == "dir-name"
            assert bundle.manifest.description == ""

    def test_non_string_frontmatter_values_fall_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            skills_dir = Path(tmp) / "skills"
            _make_raw_skill_dir(
                skills_dir,
                "dir-name",
                "---\nname: 123\ndescription: [nope]\n---\n\nbody",
            )

            global_cfg = GlobalConfig(registry={"skills_dirs": [str(skills_dir)]})
            effective = EffectiveConfig(global_cfg, None, None)
            sm = SkillManager(effective)

            bundle = sm.get_skill("dir-name")
            assert bundle is not None
            assert bundle.manifest.name == "dir-name"
            assert bundle.manifest.description == ""

    def test_empty_skill_md_falls_back_to_path_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            skills_dir = Path(tmp) / "skills"
            _make_raw_skill_dir(skills_dir, "dir-name", "")

            global_cfg = GlobalConfig(registry={"skills_dirs": [str(skills_dir)]})
            effective = EffectiveConfig(global_cfg, None, None)
            sm = SkillManager(effective)

            bundle = sm.get_skill("dir-name")
            assert bundle is not None
            assert bundle.manifest.name == "dir-name"
            assert bundle.manifest.description == ""

    def test_broken_symlink_reported_not_catalogued(self):
        with tempfile.TemporaryDirectory() as tmp:
            skills_dir = Path(tmp) / "skills"
            real_dir = Path(tmp) / "elsewhere" / "gone-skill"
            real_dir.mkdir(parents=True)
            (real_dir / "SKILL.md").write_text(
                "---\nname: gone-skill\ndescription: temp\n---\n\nbody"
            )
            skills_dir.mkdir()
            link = skills_dir / "gone-skill"
            link.symlink_to(real_dir)
            shutil.rmtree(real_dir)

            global_cfg = GlobalConfig(registry={"skills_dirs": [str(skills_dir)]})
            effective = EffectiveConfig(global_cfg, None, None)
            sm = SkillManager(effective)

            assert "gone-skill" in sm.broken_skills
            assert sm.get_skill("gone-skill") is None
            assert sm.get_skill("dir-name") is None

    def test_malformed_manifest_skips_skill_despite_frontmatter(self):
        with tempfile.TemporaryDirectory() as tmp:
            skills_dir = Path(tmp) / "skills"
            skill_dir = _make_raw_skill_dir(
                skills_dir,
                "dir-name",
                "---\nname: front-name\ndescription: from frontmatter\n---\n\nbody",
            )
            (skill_dir / "manifest.json").write_text("{not valid json")

            global_cfg = GlobalConfig(registry={"skills_dirs": [str(skills_dir)]})
            effective = EffectiveConfig(global_cfg, None, None)
            sm = SkillManager(effective)

            assert sm.get_skill("front-name") is None
            assert sm.get_skill("dir-name") is None

    def test_explicit_manifest_metadata_wins_over_frontmatter(self):
        with tempfile.TemporaryDirectory() as tmp:
            skills_dir = Path(tmp) / "skills"
            _make_raw_skill_dir(
                skills_dir,
                "dir-name",
                "---\nname: front-name\ndescription: front desc\n---\n\nbody",
                manifest={
                    "name": "manifest-name",
                    "description": "manifest desc",
                },
            )

            global_cfg = GlobalConfig(registry={"skills_dirs": [str(skills_dir)]})
            effective = EffectiveConfig(global_cfg, None, None)
            sm = SkillManager(effective)

            bundle = sm.get_skill("manifest-name")
            assert bundle is not None
            assert bundle.manifest.name == "manifest-name"
            assert bundle.manifest.description == "manifest desc"
            assert sm.get_skill("front-name") is None

    def test_blank_manifest_description_falls_back_to_frontmatter(self):
        with tempfile.TemporaryDirectory() as tmp:
            skills_dir = Path(tmp) / "skills"
            _make_raw_skill_dir(
                skills_dir,
                "dir-name",
                "---\nname: front-name\ndescription: front desc\n---\n\nbody",
                manifest={"name": "manifest-name", "description": ""},
            )

            global_cfg = GlobalConfig(registry={"skills_dirs": [str(skills_dir)]})
            effective = EffectiveConfig(global_cfg, None, None)
            sm = SkillManager(effective)

            bundle = sm.get_skill("manifest-name")
            assert bundle is not None
            assert bundle.manifest.name == "manifest-name"
            assert bundle.manifest.description == "front desc"

    def test_missing_manifest_description_falls_back_to_frontmatter(self):
        with tempfile.TemporaryDirectory() as tmp:
            skills_dir = Path(tmp) / "skills"
            _make_raw_skill_dir(
                skills_dir,
                "dir-name",
                "---\nname: front-name\ndescription: front desc\n---\n\nbody",
                manifest={"name": "manifest-name"},
            )

            global_cfg = GlobalConfig(registry={"skills_dirs": [str(skills_dir)]})
            effective = EffectiveConfig(global_cfg, None, None)
            sm = SkillManager(effective)

            bundle = sm.get_skill("manifest-name")
            assert bundle is not None
            assert bundle.manifest.description == "front desc"

    def test_blank_manifest_name_falls_back_to_frontmatter(self):
        with tempfile.TemporaryDirectory() as tmp:
            skills_dir = Path(tmp) / "skills"
            _make_raw_skill_dir(
                skills_dir,
                "dir-name",
                "---\nname: front-name\ndescription: front desc\n---\n\nbody",
                manifest={"name": "", "description": "manifest desc"},
            )

            global_cfg = GlobalConfig(registry={"skills_dirs": [str(skills_dir)]})
            effective = EffectiveConfig(global_cfg, None, None)
            sm = SkillManager(effective)

            bundle = sm.get_skill("front-name")
            assert bundle is not None
            assert bundle.manifest.name == "front-name"
            assert bundle.manifest.description == "manifest desc"

    def test_blank_manifest_name_without_frontmatter_uses_path_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            skills_dir = Path(tmp) / "skills"
            _make_raw_skill_dir(
                skills_dir,
                "dir-name",
                "# no frontmatter here",
                manifest={"name": "", "description": "manifest desc"},
            )

            global_cfg = GlobalConfig(registry={"skills_dirs": [str(skills_dir)]})
            effective = EffectiveConfig(global_cfg, None, None)
            sm = SkillManager(effective)

            bundle = sm.get_skill("dir-name")
            assert bundle is not None
            assert bundle.manifest.name == "dir-name"
            assert bundle.manifest.description == "manifest desc"

    def test_fallback_metadata_respects_allowlist_and_shadowing(self):
        with tempfile.TemporaryDirectory() as tmp:
            global_dir = Path(tmp) / "global-skills"
            project_dir = Path(tmp) / "proj" / ".gearcore" / "skills"
            _make_raw_skill_dir(
                global_dir,
                "dir-name",
                "---\nname: fm-skill\ndescription: global fm\n---\n\nbody",
            )
            _make_raw_skill_dir(
                project_dir,
                "dir-name",
                "---\nname: fm-skill\ndescription: local fm\n---\n\nbody",
            )

            global_cfg = GlobalConfig(registry={"skills_dirs": [str(global_dir)]})
            project_cfg = ProjectConfig(scope={"skills": {"include": ["fm-skill"]}})
            effective = EffectiveConfig(global_cfg, project_cfg, Path(tmp) / "proj")
            sm = SkillManager(effective)

            assert "fm-skill" in sm.visible_skill_names
            bundle = sm.get_skill("fm-skill")
            assert bundle is not None
            assert bundle.is_project_local is True
            assert bundle.manifest.description == "local fm"


class TestShadowing:
    def test_project_local_shadowing_global_logs_warning(self, caplog):
        import logging

        with tempfile.TemporaryDirectory() as tmp:
            global_dir = Path(tmp) / "global-skills"
            project_dir = Path(tmp) / "proj" / ".gearcore" / "skills"
            _make_skill_dir(global_dir, "dup-skill")
            _make_skill_dir(project_dir, "dup-skill")

            global_cfg = GlobalConfig(registry={"skills_dirs": [str(global_dir)]})
            effective = EffectiveConfig(global_cfg, ProjectConfig(), Path(tmp) / "proj")

            with caplog.at_level(logging.WARNING):
                sm = SkillManager(effective)

        assert sm.get_skill("dup-skill").is_project_local is True
        assert any("shadows" in r.message for r in caplog.records)

    def test_no_warning_for_distinct_skills(self, caplog):
        import logging

        with tempfile.TemporaryDirectory() as tmp:
            skills_dir = Path(tmp) / "skills"
            _make_skill_dir(skills_dir, "skill-a")
            _make_skill_dir(skills_dir, "skill-b")

            global_cfg = GlobalConfig(registry={"skills_dirs": [str(skills_dir)]})
            effective = EffectiveConfig(global_cfg, None, None)

            with caplog.at_level(logging.WARNING):
                SkillManager(effective)

        assert not any("shadows" in r.message for r in caplog.records)
