"""Tests for instruction rendering."""

from pathlib import Path

from gearcore_hub.render import (
    LEVEL0_MARKER,
    apply_level0_marker,
    render_level0_section,
    render_skill_instructions,
)
from gearcore_hub.skill_manager import SkillBundle, SkillManifest


def _bundle(name: str = "demo", mcp_servers: list | None = None) -> SkillBundle:
    return SkillBundle(
        path=Path("/nonexistent"),
        manifest=SkillManifest(
            name=name, description="a demo skill", mcp_servers=mcp_servers or []
        ),
        instructions=f"# {name}\n\nDo the thing.",
    )


class TestRenderSkillInstructions:
    def test_plain_skill_renders_instructions_and_bundle_location(self):
        out = render_skill_instructions(_bundle())
        assert out.startswith("# demo\n\nDo the thing.")
        assert "## Skill bundle location" in out

    def test_bundle_location_lists_registered_and_resolved_paths(self, tmp_path):
        real = tmp_path / "real" / "demo"
        real.mkdir(parents=True)
        registered = tmp_path / "skills" / "demo"
        registered.parent.mkdir()
        registered.symlink_to(real)
        bundle = _bundle()
        bundle.path = registered

        out = render_skill_instructions(bundle)

        assert f"Registered at: `{registered}`" in out
        assert f"Resolved bundle root: `{real}`" in out
        assert "resolve from the resolved bundle root" in out

    def test_bundle_location_works_without_symlink(self, tmp_path):
        bundle = _bundle()
        bundle.path = tmp_path / "skills" / "demo"

        out = render_skill_instructions(bundle)

        assert f"Registered at: `{bundle.path}`" in out
        assert f"Resolved bundle root: `{bundle.path.resolve()}`" in out

    def test_mcp_skill_appends_call_commands(self):
        bundle = _bundle(
            mcp_servers=[{"server_id": "fs", "tools": ["read_file", "write_file"]}]
        )
        out = render_skill_instructions(bundle)
        assert out.startswith("# demo")
        assert "## Available tools (via `gearcore call`)" in out
        assert "gearcore call fs read_file '<json_args>'" in out
        assert "gearcore call fs write_file '<json_args>'" in out


class TestRenderLevel0Section:
    def test_empty_core_skills_returns_empty(self):
        assert render_level0_section([], {}) == ""

    def test_unregistered_skill_skipped(self):
        assert render_level0_section(["ghost"], {}) == ""

    def test_section_lists_name_description_and_command(self):
        skills = {"continuity-core": _bundle("continuity-core")}
        out = render_level0_section(["continuity-core"], skills)
        assert "## Default skills — always relevant" in out
        assert "**continuity-core**" in out
        assert "a demo skill" in out
        assert "`gearcore request-skill continuity-core`" in out


class TestApplyLevel0Marker:
    CONTENT = "# GearCore\n\nintro text\n\n" + LEVEL0_MARKER + "\n\n## Workflow\n"

    def test_marker_replaced_with_section(self):
        out = apply_level0_marker(self.CONTENT, "## Default skills\n\n- x\n")
        assert LEVEL0_MARKER not in out
        assert "## Default skills" in out
        assert "## Workflow" in out

    def test_marker_line_dropped_when_section_empty(self):
        out = apply_level0_marker(self.CONTENT, "")
        assert LEVEL0_MARKER not in out
        assert "intro text" in out
        assert "## Workflow" in out

    def test_content_without_marker_unchanged(self):
        content = "# GearCore\n\nno marker here\n"
        assert apply_level0_marker(content, "anything") == content
