"""Tests for the opt-in `gearcore list-skills --compact` output mode.

Compact mode emits deterministic minified JSON metadata only: it never
inlines SKILL.md bodies (unlike the legacy level-0 inline reveal) and
never leaks skills hidden by a project allowlist.
"""

import json
from pathlib import Path

import pytest

from gearcore_hub.config import (
    DisclosureConfig,
    EffectiveConfig,
    GlobalConfig,
    ProjectConfig,
)
from gearcore_hub.main import cmd_list_skills, cmd_request_skill

COMPACT_SCHEMA = "gearcore.list-skills/2"


def _decode_skills(payload):
    """Decode the documented version-2 columnar catalog shape."""
    assert payload["schema"] == COMPACT_SCHEMA
    fields = payload["skill_fields"]
    assert fields == ["name", "description", "scope", "status"]
    assert payload["source_identity"] == "{scope}:{name}"
    assert payload["request_template"] == "gearcore request-skill {name}"

    decoded = []
    for row in payload["skills"]:
        assert len(row) == len(fields)
        skill = dict(zip(fields, row, strict=True))
        skill["source"] = payload["source_identity"].format(**skill)
        skill["request"] = payload["request_template"].format(**skill)
        decoded.append(skill)
    return decoded


def _decode_broken(payload):
    fields = payload["broken_fields"]
    assert fields == ["name", "target"]
    return [dict(zip(fields, row, strict=True)) for row in payload["broken"]]


@pytest.fixture(autouse=True)
def _hermetic_skills_dirs(monkeypatch):
    """Exclude the vendored superpowers bundle so fixtures are exact."""
    monkeypatch.setattr("gearcore_hub.config.bundled_superpowers_dir", lambda: None)


ENGINEERING_BODY = (
    "# engineering\n\nLoad-bearing workflow:\n\n1. Read the config layer.\n"
    "2. Gate visibility before activation.\n3. Never inline this body in compact mode."
)
FICTION_BODY = (
    "# fiction\n\nNarrative continuity rules:\n\n- Track named characters.\n"
    "- Keep timeline state explicit across chapters."
)


def _make_skill(
    base: Path,
    name: str,
    description: str = "test skill",
    body: str = "Instructions body.",
) -> Path:
    d = base / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n{body}"
    )
    return d


def _effective(tmp_path, core_skills=(), project_cfg=None, project_root=None):
    skills_dir = tmp_path / "skills"
    global_cfg = GlobalConfig(
        registry={"skills_dirs": [str(skills_dir)]},
        disclosure=DisclosureConfig(core_skills=list(core_skills)),
    )
    return EffectiveConfig(global_cfg, project_cfg, project_root)


def _capture(config, compact, tmp_path):
    import contextlib
    import io

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        if compact:
            cmd_list_skills(config, compact=True)
        else:
            cmd_list_skills(config)
    return buf.getvalue()


class TestCompactSchema:
    def test_compact_emits_schema_context_completeness_and_skills(
        self, tmp_path, capsys
    ):
        _make_skill(tmp_path / "skills", "engineering", description="build systems")
        _make_skill(tmp_path / "skills", "fiction", description="narrative rules")

        cmd_list_skills(_effective(tmp_path, []), compact=True)

        payload = json.loads(capsys.readouterr().out)
        assert payload["schema"] == COMPACT_SCHEMA
        assert payload["context"] == "global"
        assert payload["complete"] is True
        assert isinstance(payload["skills"], list)
        assert _decode_skills(payload)

    def test_compact_skill_entries_have_required_fields(self, tmp_path, capsys):
        skills_dir = tmp_path / "skills"
        _make_skill(skills_dir, "engineering", description="build systems")

        cmd_list_skills(_effective(tmp_path, []), compact=True)

        payload = json.loads(capsys.readouterr().out)
        assert [s["name"] for s in _decode_skills(payload)] == ["engineering"]
        entry = _decode_skills(payload)[0]
        assert entry["description"] == "build systems"
        assert entry["scope"] == "global"
        assert entry["source"] == "global:engineering"
        assert entry["status"] == "available"
        assert entry["request"] == "gearcore request-skill engineering"

    def test_compact_skills_sorted_deterministically_by_name(self, tmp_path, capsys):
        _make_skill(tmp_path / "skills", "fiction")
        _make_skill(tmp_path / "skills", "engineering")
        _make_skill(tmp_path / "skills", "archive")

        cmd_list_skills(_effective(tmp_path, []), compact=True)

        payload = json.loads(capsys.readouterr().out)
        names = [s["name"] for s in _decode_skills(payload)]
        assert names == sorted(names)

    def test_compact_output_is_deterministic_across_runs(self, tmp_path, capsys):
        _make_skill(tmp_path / "skills", "engineering")
        _make_skill(tmp_path / "skills", "fiction")
        config = _effective(tmp_path, [])

        cmd_list_skills(config, compact=True)
        first = capsys.readouterr().out
        cmd_list_skills(config, compact=True)
        second = capsys.readouterr().out

        assert first == second

    def test_compact_no_skills_reports_empty_and_complete(self, tmp_path, capsys):
        cmd_list_skills(_effective(tmp_path, []), compact=True)

        payload = json.loads(capsys.readouterr().out)
        assert _decode_skills(payload) == []
        assert _decode_broken(payload) == []
        assert payload["complete"] is True

    def test_compact_project_context_reported(self, tmp_path, capsys):
        project_root = tmp_path / "proj"
        (project_root / ".gearcore").mkdir(parents=True)
        project_cfg = ProjectConfig(context={"name": "demo-project"})

        cmd_list_skills(
            _effective(
                tmp_path,
                project_cfg=project_cfg,
                project_root=project_root,
            ),
            compact=True,
        )

        payload = json.loads(capsys.readouterr().out)
        assert payload["context"] == "demo-project"


class TestCompactNeverInlinesBodies:
    def test_compact_omits_skill_bodies(self, tmp_path, capsys):
        _make_skill(tmp_path / "skills", "engineering", body=ENGINEERING_BODY)

        cmd_list_skills(_effective(tmp_path, []), compact=True)

        out = capsys.readouterr().out
        assert "Load-bearing workflow" not in out

    def test_compact_omits_level0_inline_reveal(self, tmp_path, capsys):
        _make_skill(tmp_path / "skills", "engineering", body=ENGINEERING_BODY)
        _make_skill(tmp_path / "skills", "fiction", body=FICTION_BODY)
        config = _effective(tmp_path, ["engineering"])

        cmd_list_skills(config, compact=True)
        compact_out = capsys.readouterr().out
        cmd_list_skills(config)
        legacy_out = capsys.readouterr().out

        # Legacy inlines the level-0 body; compact must not.
        assert "=== LEVEL-0 SKILL: engineering ===" in legacy_out
        assert ENGINEERING_BODY in legacy_out
        assert "LEVEL-0" not in compact_out
        assert "Load-bearing workflow" not in compact_out
        # The skill itself is still listed as metadata.
        payload = json.loads(compact_out)
        assert [s["name"] for s in _decode_skills(payload)] == [
            "engineering",
            "fiction",
        ]

    def test_compact_never_prints_level0_for_hidden_core_skill(self, tmp_path, capsys):
        _make_skill(tmp_path / "skills", "continuity-core", body="CORE BODY")
        _make_skill(tmp_path / "skills", "other-skill")
        project_root = tmp_path / "proj"
        (project_root / ".gearcore").mkdir(parents=True)
        project_cfg = ProjectConfig(scope={"skills": {"include": ["other-skill"]}})
        config = _effective(
            tmp_path,
            ["continuity-core"],
            project_cfg=project_cfg,
            project_root=project_root,
        )

        cmd_list_skills(config, compact=True)

        out = capsys.readouterr().out
        payload = json.loads(out)
        assert "CORE BODY" not in out
        assert all(s["name"] != "continuity-core" for s in _decode_skills(payload))


class TestCompactVisibility:
    def test_compact_empty_project_allowlist_lists_nothing(self, tmp_path, capsys):
        _make_skill(tmp_path / "skills", "engineering")
        project_root = tmp_path / "proj"
        (project_root / ".gearcore").mkdir(parents=True)
        project_cfg = ProjectConfig(scope={"skills": {"include": []}})
        config = _effective(
            tmp_path, project_cfg=project_cfg, project_root=project_root
        )

        cmd_list_skills(config, compact=True)

        payload = json.loads(capsys.readouterr().out)
        assert payload["skills"] == []
        assert payload["complete"] is True

    def test_compact_hidden_global_skill_does_not_leak(self, tmp_path, capsys):
        skills_dir = tmp_path / "skills"
        _make_skill(skills_dir, "engineering")
        _make_skill(skills_dir, "secret-skill", description="hidden internal details")
        project_root = tmp_path / "proj"
        (project_root / ".gearcore").mkdir(parents=True)
        project_cfg = ProjectConfig(scope={"skills": {"include": ["engineering"]}})
        config = _effective(
            tmp_path, project_cfg=project_cfg, project_root=project_root
        )

        cmd_list_skills(config, compact=True)

        out = capsys.readouterr().out
        assert "secret-skill" not in out
        assert "hidden internal details" not in out
        assert str(skills_dir / "secret-skill") not in out

    def test_compact_hidden_broken_global_skill_does_not_leak(self, tmp_path, capsys):
        skills_dir = tmp_path / "skills"
        _make_skill(skills_dir, "visible-skill")
        hidden_target = tmp_path / "private" / "hidden-global-skill"
        skills_dir.mkdir(exist_ok=True)
        (skills_dir / "hidden-global-skill").symlink_to(hidden_target)

        project_root = tmp_path / "proj"
        (project_root / ".gearcore").mkdir(parents=True)
        project_cfg = ProjectConfig(scope={"skills": {"include": ["visible-skill"]}})
        config = _effective(
            tmp_path,
            project_cfg=project_cfg,
            project_root=project_root,
        )

        cmd_list_skills(config, compact=True)

        out = capsys.readouterr().out
        assert "hidden-global-skill" not in out
        assert str(hidden_target) not in out

    def test_compact_project_local_broken_skill_remains_visible(self, tmp_path, capsys):
        project_root = tmp_path / "proj"
        local_dir = project_root / ".gearcore" / "skills"
        local_dir.mkdir(parents=True)
        missing_target = tmp_path / "private" / "local-skill"
        (local_dir / "local-skill").symlink_to(missing_target)
        project_cfg = ProjectConfig(scope={"skills": {"include": []}})
        config = _effective(
            tmp_path,
            project_cfg=project_cfg,
            project_root=project_root,
        )

        cmd_list_skills(config, compact=True)

        payload = json.loads(capsys.readouterr().out)
        assert _decode_broken(payload) == [
            {"name": "local-skill", "target": str(missing_target)}
        ]

    def test_compact_unscoped_project_discovers_globals_and_locals(
        self, tmp_path, capsys
    ):
        _make_skill(tmp_path / "skills", "engineering")
        local_dir = tmp_path / "proj" / ".gearcore" / "skills"
        _make_skill(local_dir, "fiction")
        project_cfg = ProjectConfig(context={"name": "proj"})
        config = _effective(
            tmp_path, project_cfg=project_cfg, project_root=tmp_path / "proj"
        )

        cmd_list_skills(config, compact=True)

        payload = json.loads(capsys.readouterr().out)
        entries = {s["name"]: s for s in _decode_skills(payload)}
        assert entries["engineering"]["scope"] == "global"
        assert entries["fiction"]["scope"] == "project"

    def test_compact_project_shadowing_reports_single_local_entry(
        self, tmp_path, capsys
    ):
        skills_dir = tmp_path / "skills"
        _make_skill(skills_dir, "engineering", description="global desc")
        local_dir = tmp_path / "proj" / ".gearcore" / "skills"
        _make_skill(local_dir, "engineering", description="local desc")
        project_cfg = ProjectConfig(context={"name": "proj"})
        config = _effective(
            tmp_path, project_cfg=project_cfg, project_root=tmp_path / "proj"
        )

        cmd_list_skills(config, compact=True)

        out = capsys.readouterr().out
        payload = json.loads(out)
        engineering = [s for s in _decode_skills(payload) if s["name"] == "engineering"]
        assert len(engineering) == 1
        assert engineering[0]["scope"] == "project"
        assert engineering[0]["description"] == "local desc"
        assert engineering[0]["source"] == "project:engineering"
        assert str(local_dir / "engineering") not in out
        assert str(skills_dir / "engineering") not in out


class TestCompactBrokenAndMalformed:
    def test_compact_broken_symlink_reported_without_content(self, tmp_path, capsys):
        import shutil

        skills_dir = tmp_path / "skills"
        real_dir = tmp_path / "elsewhere" / "gone-skill"
        real_dir.mkdir(parents=True)
        (real_dir / "SKILL.md").write_text("---\nname: gone-skill\n---\n\nbody")
        skills_dir.mkdir()
        (skills_dir / "gone-skill").symlink_to(real_dir)
        shutil.rmtree(real_dir)
        _make_skill(skills_dir, "engineering")

        cmd_list_skills(_effective(tmp_path, []), compact=True)

        payload = json.loads(capsys.readouterr().out)
        broken = _decode_broken(payload)
        assert [b["name"] for b in broken] == ["gone-skill"]
        assert broken[0]["target"]
        assert all(s["name"] != "gone-skill" for s in _decode_skills(payload))

    def test_compact_malformed_manifest_skill_absent(self, tmp_path, capsys):
        skills_dir = tmp_path / "skills"
        _make_skill(skills_dir, "engineering")
        broken_dir = skills_dir / "bad-manifest"
        broken_dir.mkdir(parents=True)
        (broken_dir / "SKILL.md").write_text(
            "---\nname: bad-manifest\ndescription: x\n---\n\nSECRET BODY"
        )
        (broken_dir / "manifest.json").write_text("{not valid json")

        cmd_list_skills(_effective(tmp_path, []), compact=True)

        out = capsys.readouterr().out
        payload = json.loads(out)
        assert all(s["name"] != "bad-manifest" for s in _decode_skills(payload))
        assert "SECRET BODY" not in out

    def test_compact_malformed_frontmatter_uses_path_identity_without_body(
        self, tmp_path, capsys
    ):
        skills_dir = tmp_path / "skills"
        raw = skills_dir / "dir-name"
        raw.mkdir(parents=True)
        (raw / "SKILL.md").write_text("---\nname: [unclosed\n---\n\nSECRET BODY")

        cmd_list_skills(_effective(tmp_path, []), compact=True)

        out = capsys.readouterr().out
        payload = json.loads(out)
        entry = _decode_skills(payload)[0]
        assert entry["name"] == "dir-name"
        assert entry["description"] == ""
        assert "SECRET BODY" not in out

    def test_compact_binary_skill_md_skipped_without_dump(self, tmp_path, capsys):
        skills_dir = tmp_path / "skills"
        binary = skills_dir / "binary-skill"
        binary.mkdir(parents=True)
        (binary / "SKILL.md").write_bytes(b"\xff\xfe\x00\x01binary")

        cmd_list_skills(_effective(tmp_path, []), compact=True)

        out = capsys.readouterr().out
        payload = json.loads(out)
        assert _decode_skills(payload) == []
        assert "binary-skill" not in out


class TestCompactVsLegacySize:
    def test_compact_is_materially_smaller_for_representative_catalog(
        self, tmp_path, capsys
    ):
        """A catalog should remain useful without per-skill JSON key overhead."""
        skills_dir = tmp_path / "skills"
        descriptions = {
            "access-control": "Authorize project actions using explicit scoped policy.",
            "build-systems": "Build and validate reproducible software artifacts.",
            "data-contracts": "Define stable schemas and compatibility boundaries.",
            "design-review": "Review user-interface changes against product intent.",
            "incident-response": "Coordinate evidence-led incident investigation.",
            "migration-guide": "Plan staged migrations with reversible checkpoints.",
            "release-management": "Prepare releases with versioned validation evidence.",
            "service-operations": "Operate services with observable failure handling.",
        }
        for name, description in descriptions.items():
            _make_skill(
                skills_dir,
                name,
                description=description,
                body=(
                    "# " + name + "\n\n"
                    "Use this workflow to make a concrete decision, retain the "
                    "evidence, and validate the result before handoff.\n"
                )
                * 4,
            )
        config = _effective(
            tmp_path,
            ["access-control", "build-systems", "data-contracts", "design-review"],
        )

        legacy_out = _capture(config, compact=False, tmp_path=tmp_path)
        compact_out = _capture(config, compact=True, tmp_path=tmp_path)
        decoded = {s["name"]: s for s in _decode_skills(json.loads(compact_out))}

        assert set(decoded) == set(descriptions)
        for name, description in descriptions.items():
            entry = decoded[name]
            assert entry["description"] == description
            assert entry["scope"] == "global"
            assert entry["source"] == f"global:{name}"
            assert entry["status"] == (
                "active"
                if name
                in {
                    "access-control",
                    "build-systems",
                    "data-contracts",
                    "design-review",
                }
                else "available"
            )
            assert entry["request"] == f"gearcore request-skill {name}"

        # Byte-length comparison only. This fixture is deliberately ordinary:
        # several frontmatter-described skills and four active workflows.
        assert len(compact_out.encode("utf-8")) <= len(legacy_out.encode("utf-8")) * 0.4

    def test_compact_bytes_smaller_than_legacy_for_fixed_fixture(
        self, tmp_path, capsys
    ):
        _make_skill(tmp_path / "skills", "engineering", body=ENGINEERING_BODY)
        _make_skill(tmp_path / "skills", "fiction", body=FICTION_BODY)
        config = _effective(tmp_path, ["engineering"])

        legacy_out = _capture(config, compact=False, tmp_path=tmp_path)
        compact_out = _capture(config, compact=True, tmp_path=tmp_path)

        legacy_bytes = legacy_out.encode("utf-8")
        compact_bytes = compact_out.encode("utf-8")
        # Byte-length comparison only — bytes are never translated into
        # token or cost estimates in this ticket.
        assert len(compact_bytes) < len(legacy_bytes)
        # Legacy output shape unchanged (level-0 inline + header intact).
        assert legacy_out.startswith("GearCore skills (")
        assert "=== LEVEL-0 SKILL: engineering ===" in legacy_out


class TestLegacyAndRequestSkillUnchanged:
    def test_request_skill_still_returns_full_body(self, tmp_path, capsys):
        _make_skill(tmp_path / "skills", "engineering", body=ENGINEERING_BODY)

        cmd_request_skill(_effective(tmp_path, []), "engineering")

        out = capsys.readouterr().out
        assert ENGINEERING_BODY in out
        assert "## Skill bundle location" in out

    def test_legacy_mode_default_unchanged(self, tmp_path, capsys):
        _make_skill(tmp_path / "skills", "engineering", body=ENGINEERING_BODY)

        cmd_list_skills(_effective(tmp_path, []))

        out = capsys.readouterr().out
        assert (
            out == "GearCore skills (global context):\n\n  engineering — test skill\n"
        )
