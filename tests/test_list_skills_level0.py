"""Tests for level-0 inline reveal in cmd_list_skills."""

from pathlib import Path

from gearcore_hub.config import (
    DisclosureConfig,
    EffectiveConfig,
    GlobalConfig,
    ProjectConfig,
)
from gearcore_hub.main import cmd_list_skills


def _make_skill(base: Path, name: str) -> None:
    d = base / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(f"---\nname: {name}\n---\n\n# {name}\n\nInstructions body.")
    (d / "manifest.json").write_text(f'{{"name": "{name}", "description": "desc"}}')


def _effective(tmp_path, core_skills, project_cfg=None, project_root=None):
    skills_dir = tmp_path / "skills"
    global_cfg = GlobalConfig(
        registry={"skills_dirs": [str(skills_dir)]},
        disclosure=DisclosureConfig(core_skills=core_skills),
    )
    return EffectiveConfig(global_cfg, project_cfg, project_root)


class TestListSkillsLevel0:
    def test_core_skill_instructions_inlined(self, tmp_path, capsys):
        _make_skill(tmp_path / "skills", "continuity-core")
        _make_skill(tmp_path / "skills", "other-skill")

        cmd_list_skills(_effective(tmp_path, ["continuity-core"]))

        out = capsys.readouterr().out
        assert "=== LEVEL-0 SKILL: continuity-core ===" in out
        assert "Instructions body." in out
        assert "=== END LEVEL-0 SKILL: continuity-core ===" in out
        # regular listing still present, for both skills
        assert "other-skill" in out

    def test_no_core_skills_no_blocks(self, tmp_path, capsys):
        _make_skill(tmp_path / "skills", "other-skill")

        cmd_list_skills(_effective(tmp_path, []))

        out = capsys.readouterr().out
        assert "LEVEL-0" not in out
        assert "other-skill" in out

    def test_core_skill_hidden_by_allowlist_skipped_silently(self, tmp_path, capsys):
        _make_skill(tmp_path / "skills", "continuity-core")
        project_root = tmp_path / "proj"
        (project_root / ".gearcore").mkdir(parents=True)
        project_cfg = ProjectConfig(scope={"skills": {"include": []}})

        cmd_list_skills(
            _effective(
                tmp_path,
                ["continuity-core"],
                project_cfg=project_cfg,
                project_root=project_root,
            )
        )

        out = capsys.readouterr().out
        assert "LEVEL-0" not in out
