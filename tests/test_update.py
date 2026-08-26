"""Tests for gearcore_hub.update (skill/MCP refresh paths)."""

from pathlib import Path

import pytest

from gearcore_hub.config import (
    DisclosureConfig,
    EffectiveConfig,
    GlobalConfig,
)
from gearcore_hub.update import _find_installed_skill, update_skill


def _effective(skills_dirs: list[Path]) -> EffectiveConfig:
    global_cfg = GlobalConfig(
        registry={"skills_dirs": [str(d) for d in skills_dirs]},
        disclosure=DisclosureConfig(core_skills=[]),
    )
    return EffectiveConfig(global_cfg, None, None)


class TestFindInstalledSkillTraversal:
    def test_rejects_traversal_name(self, tmp_path):
        victim = tmp_path / "victim"
        victim.mkdir()
        (victim / "SKILL.md").write_text("# x")

        with pytest.raises(ValueError, match="Invalid skill name"):
            _find_installed_skill("../victim", _effective([tmp_path / "skills"]))

    def test_rejects_absolute_name(self, tmp_path):
        with pytest.raises(ValueError, match="Invalid skill name"):
            _find_installed_skill("/etc", _effective([tmp_path / "skills"]))

    def test_finds_legitimate_skill(self, tmp_path):
        skills = tmp_path / "skills"
        (skills / "my-skill").mkdir(parents=True)

        assert _find_installed_skill("my-skill", _effective([skills])) == (
            skills / "my-skill"
        )


class TestUpdateSkillTraversal:
    def test_refuses_to_delete_outside_skills_dir(self, tmp_path):
        skills = tmp_path / "skills"
        skills.mkdir()
        victim = tmp_path / "victim"
        victim.mkdir()
        (victim / "data.txt").write_text("do not delete")

        result = update_skill("../victim", _effective([skills]))

        assert victim.exists()
        assert "Invalid skill name" in result["message"]
