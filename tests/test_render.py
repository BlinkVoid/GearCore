"""Tests for instruction rendering."""

from pathlib import Path

from gearcore_hub.render import render_skill_instructions
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
    def test_plain_skill_renders_instructions_only(self):
        out = render_skill_instructions(_bundle())
        assert out == "# demo\n\nDo the thing."

    def test_mcp_skill_appends_call_commands(self):
        bundle = _bundle(
            mcp_servers=[{"server_id": "fs", "tools": ["read_file", "write_file"]}]
        )
        out = render_skill_instructions(bundle)
        assert out.startswith("# demo")
        assert "## Available tools (via `gearcore call`)" in out
        assert "gearcore call fs read_file '<json_args>'" in out
        assert "gearcore call fs write_file '<json_args>'" in out
