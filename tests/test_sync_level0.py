"""Tests for level-0 section embedding during sync."""

from pathlib import Path

from gearcore_hub.render import LEVEL0_MARKER
from gearcore_hub.sync import embed_level0_section


def _write_global_config(tmp_path: Path, skills_dir: Path, core: list[str]) -> Path:
    cfg = tmp_path / "config.yaml"
    lines = [
        "version: 2",
        "registry:",
        "  skills_dirs:",
        f"    - {skills_dir}",
        "disclosure:",
        f"  core_skills: [{', '.join(core)}]",
    ]
    cfg.write_text("\n".join(lines) + "\n")
    return cfg


def _make_skill(base: Path, name: str) -> None:
    d = base / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: continuity stuff\n---\n\n# {name}"
    )
    (d / "manifest.json").write_text(
        f'{{"name": "{name}", "description": "continuity stuff"}}'
    )


class TestEmbedLevel0Section:
    def test_marker_replaced_with_core_skill(self, tmp_path):
        skills_dir = tmp_path / "skills"
        _make_skill(skills_dir, "continuity-core")
        cfg = _write_global_config(tmp_path, skills_dir, ["continuity-core"])

        skill_md = tmp_path / "SKILL.md"
        skill_md.write_text(f"# GearCore\n\n{LEVEL0_MARKER}\n\n## Workflow\n")

        changed = embed_level0_section(skill_md, global_config_path=cfg)

        assert changed is True
        text = skill_md.read_text()
        assert LEVEL0_MARKER not in text
        assert "**continuity-core**" in text
        assert "continuity stuff" in text

    def test_marker_dropped_when_no_core_skills(self, tmp_path):
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        cfg = _write_global_config(tmp_path, skills_dir, [])

        skill_md = tmp_path / "SKILL.md"
        skill_md.write_text(f"# GearCore\n\n{LEVEL0_MARKER}\n\n## Workflow\n")

        changed = embed_level0_section(skill_md, global_config_path=cfg)

        assert changed is True
        text = skill_md.read_text()
        assert LEVEL0_MARKER not in text
        assert "Default skills" not in text
        assert "## Workflow" in text

    def test_file_without_marker_untouched(self, tmp_path):
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        cfg = _write_global_config(tmp_path, skills_dir, ["anything"])

        skill_md = tmp_path / "SKILL.md"
        original = "# GearCore\n\nno marker\n"
        skill_md.write_text(original)

        changed = embed_level0_section(skill_md, global_config_path=cfg)

        assert changed is False
        assert skill_md.read_text() == original
