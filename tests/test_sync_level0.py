"""Tests for level-0 section embedding during sync."""

from pathlib import Path

import pytest

from gearcore_hub.config import (
    EffectiveConfig,
    GlobalConfig,
    SkillBindingCeiling,
    load_config,
)
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
    (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: continuity stuff\n---\n\n# {name}")
    (d / "manifest.json").write_text(
        f'{{"name": "{name}", "description": "continuity stuff"}}'
    )


def _write_v3_global_config(tmp_path: Path, skills_dir: Path) -> Path:
    cfg = tmp_path / "config-v3.yaml"
    cfg.write_text(
        f"""\
version: 3
registry:
  skills_dirs: [{skills_dir}]
disclosure:
  core_skills: []
profiles:
  default: operator
  entries:
    operator:
      scope:
        skills:
          include: [hive-dispatcher]
          protected: [hive-dispatcher]
      disclosure:
        core_skills: [hive-dispatcher]
    hive-worker:
      constrained: true
      scope:
        skills:
          include: [hive-worker]
          deny: [hive-dispatcher]
      disclosure:
        core_skills: [hive-worker]
""",
        encoding="utf-8",
    )
    return cfg


class TestEmbedLevel0Section:
    def test_visible_core_skills_preserve_configured_order(self, tmp_path):
        skills_dir = tmp_path / "skills"
        _make_skill(skills_dir, "second-core")
        _make_skill(skills_dir, "first-core")
        config_path = _write_global_config(
            tmp_path, skills_dir, ["second-core", "first-core"]
        )
        effective = load_config(
            project=tmp_path / "isolated", global_config_path=config_path
        )
        skill_md = tmp_path / "SKILL.md"
        skill_md.write_text(f"{LEVEL0_MARKER}\n", encoding="utf-8")

        embed_level0_section(skill_md, effective)

        text = skill_md.read_text(encoding="utf-8")
        assert text.index("**second-core**") < text.index("**first-core**")

    def test_v3_denied_core_skill_is_not_embedded(self, tmp_path):
        skills_dir = tmp_path / "skills"
        _make_skill(skills_dir, "visible-core")
        _make_skill(skills_dir, "denied-core")
        config_path = tmp_path / "denied-v3.yaml"
        config_path.write_text(
            f"""\
version: 3
registry:
  skills_dirs: [{skills_dir}]
profiles:
  default: operator
  entries:
    operator:
          scope:
            skills:
              include: [visible-core, denied-core, unavailable-core]
              deny: [denied-core]
          disclosure:
            core_skills: [denied-core, unavailable-core, visible-core]
""",
            encoding="utf-8",
        )
        effective = load_config(
            project=tmp_path / "isolated", global_config_path=config_path
        )
        skill_md = tmp_path / "SKILL.md"
        skill_md.write_text(f"{LEVEL0_MARKER}\n", encoding="utf-8")

        embed_level0_section(skill_md, effective)

        text = skill_md.read_text(encoding="utf-8")
        assert "**visible-core**" in text
        assert "**denied-core**" not in text
        assert "**unavailable-core**" not in text

    def test_v2_project_allowlist_filters_embedded_global_core_skills(
        self, tmp_path
    ):
        skills_dir = tmp_path / "skills"
        _make_skill(skills_dir, "visible-core")
        _make_skill(skills_dir, "hidden-core")
        config_path = _write_global_config(
            tmp_path, skills_dir, ["hidden-core", "visible-core"]
        )
        project = tmp_path / "project"
        project_config = project / ".gearcore" / "config.yaml"
        project_config.parent.mkdir(parents=True)
        project_config.write_text(
            "version: 2\nscope:\n  skills:\n    include: [visible-core]\n",
            encoding="utf-8",
        )
        effective = load_config(project=project, global_config_path=config_path)
        skill_md = tmp_path / "SKILL.md"
        skill_md.write_text(f"{LEVEL0_MARKER}\n", encoding="utf-8")

        embed_level0_section(skill_md, effective)

        text = skill_md.read_text(encoding="utf-8")
        assert "**visible-core**" in text
        assert "**hidden-core**" not in text

    def test_envelope_alternate_binding_ceiling_filters_embedded_core_skills(
        self, tmp_path
    ):
        skills_dir = tmp_path / "skills"
        _make_skill(skills_dir, "visible-core")
        _make_skill(skills_dir, "narrowed-core")
        global_config = GlobalConfig.model_validate(
            {
                "version": 3,
                "registry": {"skills_dirs": [str(skills_dir)]},
                "profiles": {
                    "default": "worker",
                    "entries": {
                        "worker": {
                            "constrained": True,
                            "scope": {
                                "skills": {
                                    "include": ["visible-core", "narrowed-core"]
                                }
                            },
                        },
                        "alternate": {
                            "scope": {
                                "skills": {
                                    "include": ["visible-core", "narrowed-core"]
                                }
                            },
                            "disclosure": {
                                "core_skills": ["narrowed-core", "visible-core"]
                            },
                        },
                    },
                },
            }
        )
        effective = EffectiveConfig(
            global_config,
            None,
            None,
            profile_name="alternate",
            profile_source="envelope",
            enforced_profile_name="worker",
            enforced_skill_bindings=frozenset(
                {
                    SkillBindingCeiling(
                        "visible-core",
                        (skills_dir / "visible-core").resolve(),
                        False,
                    )
                }
            ),
        )
        skill_md = tmp_path / "SKILL.md"
        skill_md.write_text(f"{LEVEL0_MARKER}\n", encoding="utf-8")

        embed_level0_section(skill_md, effective)

        text = skill_md.read_text(encoding="utf-8")
        assert "**visible-core**" in text
        assert "**narrowed-core**" not in text

    @pytest.mark.parametrize(
        ("profile_name", "expected", "excluded"),
        [
            (None, "hive-dispatcher", "hive-worker"),
            ("hive-worker", "hive-worker", "hive-dispatcher"),
        ],
    )
    def test_v3_embeds_effective_selected_profile_core_skills(
        self, tmp_path, profile_name, expected, excluded
    ):
        skills_dir = tmp_path / "skills"
        _make_skill(skills_dir, "hive-dispatcher")
        _make_skill(skills_dir, "hive-worker")
        config_path = _write_v3_global_config(tmp_path, skills_dir)
        effective = load_config(
            project=tmp_path / "isolated",
            global_config_path=config_path,
            profile_name=profile_name,
        )
        skill_md = tmp_path / "SKILL.md"
        skill_md.write_text(f"# GearCore\n\n{LEVEL0_MARKER}\n", encoding="utf-8")

        changed = embed_level0_section(skill_md, effective)

        assert changed is True
        text = skill_md.read_text(encoding="utf-8")
        assert f"**{expected}**" in text
        assert f"**{excluded}**" not in text

    def test_marker_replaced_with_core_skill(self, tmp_path):
        skills_dir = tmp_path / "skills"
        _make_skill(skills_dir, "continuity-core")
        cfg = _write_global_config(tmp_path, skills_dir, ["continuity-core"])

        skill_md = tmp_path / "SKILL.md"
        skill_md.write_text(f"# GearCore\n\n{LEVEL0_MARKER}\n\n## Workflow\n")

        effective = load_config(
            project=tmp_path / "isolated", global_config_path=cfg
        )
        changed = embed_level0_section(skill_md, effective)

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

        effective = load_config(
            project=tmp_path / "isolated", global_config_path=cfg
        )
        changed = embed_level0_section(skill_md, effective)

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

        effective = load_config(
            project=tmp_path / "isolated", global_config_path=cfg
        )
        changed = embed_level0_section(skill_md, effective)

        assert changed is False
        assert skill_md.read_text() == original
