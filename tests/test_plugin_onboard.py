"""Whole-plugin onboarding for Codex-compatible plugin roots.

A plugin root is identified by `.codex-plugin/plugin.json`. Onboarding must
register the whole plugin (skills plus sibling support components) under the
scope-specific plugins directory and register discovered skills through the
installed plugin root.
"""

import json
import os
from pathlib import Path

import pytest

from gearcore_hub import onboard
from gearcore_hub.main import build_parser
from gearcore_hub.plugin import (
    discover_support_components,
    load_plugin_manifest,
)


def _write_skill_dir(path: Path, name: str) -> None:
    skill = path / name
    skill.mkdir(parents=True, exist_ok=True)
    (skill / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: plugin skill\n---\n# {name}\n",
        encoding="utf-8",
    )


def _write_plugin(
    root: Path,
    name: str = "my-plugin",
    skills: list[str] | None = None,
    skills_path: str = "skills",
    manifest_extra: dict | None = None,
    support: bool = True,
) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    data = {"name": name}
    if manifest_extra:
        data.update(manifest_extra)
    marker = root / ".codex-plugin"
    marker.mkdir(exist_ok=True)
    (marker / "plugin.json").write_text(json.dumps(data), encoding="utf-8")
    for skill_name in skills if skills is not None else ["alpha"]:
        _write_skill_dir(root / skills_path, skill_name)
    if support:
        for comp, filename in {
            "commands": "run.toml",
            "orchestration": "flow.yaml",
            "scripts": "helper.sh",
            "config": "settings.json",
            "configs": "extra.yaml",
            "tests": "test_x.py",
            "docs": "guide.md",
        }.items():
            comp_dir = root / comp
            comp_dir.mkdir(exist_ok=True)
            (comp_dir / filename).write_text("placeholder\n", encoding="utf-8")
    return root


class TestPluginManifest:
    def test_detects_plugin_root_and_parses_name(self, tmp_path):
        root = _write_plugin(tmp_path / "plugin", name="my-plugin")

        manifest = load_plugin_manifest(root)

        assert manifest is not None
        assert manifest.name == "my-plugin"
        assert manifest.skills_path == "skills"
        assert manifest.skills_dir == root.resolve() / "skills"

    def test_custom_skills_path_is_parsed(self, tmp_path):
        root = _write_plugin(
            tmp_path / "plugin",
            manifest_extra={"skills": "./bundled-skills"},
            skills_path="bundled-skills",
        )

        manifest = load_plugin_manifest(root)

        assert manifest is not None
        assert manifest.skills_path == "bundled-skills"
        assert manifest.skills_dir == root.resolve() / "bundled-skills"

    def test_non_plugin_root_returns_none(self, tmp_path):
        (tmp_path / "skills" / "solo").mkdir(parents=True)
        _write_skill_dir(tmp_path / "skills", "solo")

        assert load_plugin_manifest(tmp_path) is None

    @pytest.mark.parametrize(
        "name",
        [
            "",
            "..",
            ".",
            "../escape",
            "/etc/passwd",
            "nested/name",
            "with whitespace",
            "with\ncontrol",
            "punctuation!",
            ".leading",
            "trailing.",
            "double..dot",
            42,
            None,
        ],
    )
    def test_invalid_plugin_name_rejected(self, tmp_path, name):
        root = _write_plugin(tmp_path / "plugin", manifest_extra={"name": name})

        with pytest.raises(ValueError, match="name"):
            load_plugin_manifest(root)

    def test_dotted_plugin_name_is_valid(self, tmp_path):
        root = _write_plugin(tmp_path / "plugin", name="vendor.tool")

        manifest = load_plugin_manifest(root)

        assert manifest is not None
        assert manifest.name == "vendor.tool"

    def test_skills_path_symlink_resolving_outside_root_is_rejected(self, tmp_path):
        root = _write_plugin(
            tmp_path / "plugin",
            skills=[],
            manifest_extra={"skills": "bundled-skills"},
        )
        outside = tmp_path / "outside-skills"
        outside.mkdir()
        (root / "bundled-skills").symlink_to(outside, target_is_directory=True)

        with pytest.raises(ValueError, match="inside the plugin root"):
            load_plugin_manifest(root)

    def test_missing_skills_path_inside_root_is_valid(self, tmp_path):
        root = _write_plugin(
            tmp_path / "plugin",
            skills=[],
            manifest_extra={"skills": "not-yet-created"},
        )

        manifest = load_plugin_manifest(root)

        assert manifest is not None
        assert manifest.skills_dir == root.resolve() / "not-yet-created"

    @pytest.mark.parametrize("skills", ["../outside", "/etc"])
    def test_skills_path_must_stay_inside_root(self, tmp_path, skills):
        root = _write_plugin(tmp_path / "plugin", manifest_extra={"skills": skills})

        with pytest.raises(ValueError, match="inside the plugin root"):
            load_plugin_manifest(root)

    def test_malformed_manifest_json_rejected(self, tmp_path):
        root = tmp_path / "plugin"
        marker = root / ".codex-plugin"
        marker.mkdir(parents=True)
        (marker / "plugin.json").write_text("{not json", encoding="utf-8")

        with pytest.raises(ValueError, match="plugin.json"):
            load_plugin_manifest(root)

    def test_support_components_detected(self, tmp_path):
        root = _write_plugin(tmp_path / "plugin")
        (root / "docs").mkdir(exist_ok=True)
        (root / "random-dir").mkdir()

        components = discover_support_components(root)

        assert components == [
            "commands",
            "orchestration",
            "scripts",
            "config",
            "configs",
            "tests",
            "docs",
        ]


class TestPluginOnboard:
    def test_symlink_onboard_uses_physical_skill_path_for_frontmatter_alias(
        self, tmp_path, monkeypatch
    ):
        home = tmp_path / "home"
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setattr(onboard, "sync", lambda **kwargs: {})
        root = _write_plugin(tmp_path / "plugin", name="my-plugin", skills=[])
        physical_skill = root / "skills" / "physical"
        physical_skill.mkdir(parents=True)
        (physical_skill / "SKILL.md").write_text(
            "---\nname: logical-alias\ndescription: plugin skill\n---\n# logical-alias\n",
            encoding="utf-8",
        )

        steps = onboard.run_onboard(root, no_sync=True)

        assert [step.action for step in steps] == ["plugin", "skill"]
        installed = home / ".config" / "gearcore" / "plugins" / "my-plugin"
        skill_link = home / ".config" / "gearcore" / "skills" / "logical-alias"
        assert skill_link.is_symlink()
        assert Path(os.readlink(skill_link)) == installed / "skills" / "physical"
        assert skill_link.resolve() == physical_skill.resolve()

        repeat_steps = onboard.run_onboard(root, no_sync=True)

        assert [step.detail for step in repeat_steps] == [
            "skipped plugin 'my-plugin' (already matches)",
            "skipped skill 'logical-alias' (already matches)",
        ]

    def test_symlink_onboard_registers_whole_plugin_and_skills_through_installed_root(
        self, tmp_path, monkeypatch
    ):
        home = tmp_path / "home"
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setattr(onboard, "sync", lambda **kwargs: {})
        root = _write_plugin(tmp_path / "plugin", name="my-plugin")

        steps = onboard.run_onboard(root, no_sync=True)

        assert [step.action for step in steps] == ["plugin", "skill"]
        plugins_root = home / ".config" / "gearcore" / "plugins"
        installed = plugins_root / "my-plugin"
        assert installed.is_symlink()
        assert installed.resolve() == root.resolve()
        # Whole plugin preserved: manifest, skills, and sibling components.
        for rel in (
            ".codex-plugin/plugin.json",
            "skills/alpha/SKILL.md",
            "commands/run.toml",
            "orchestration/flow.yaml",
            "scripts/helper.sh",
            "config/settings.json",
            "configs/extra.yaml",
            "tests/test_x.py",
            "docs/guide.md",
        ):
            assert (installed / rel).exists(), rel
        # Skill registered through the installed plugin root, not the leaf.
        skill_link = home / ".config" / "gearcore" / "skills" / "alpha"
        assert skill_link.is_symlink()
        assert Path(os.readlink(skill_link)) == installed / "skills" / "alpha"
        assert skill_link.resolve() == root / "skills" / "alpha"

    def test_project_scope_registers_under_project_gearcore(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(onboard, "sync", lambda **kwargs: {})
        project = tmp_path / "project"
        root = _write_plugin(tmp_path / "plugin", name="my-plugin")

        onboard.run_onboard(root, scope="project", project_root=project, no_sync=True)

        installed = project / ".gearcore" / "plugins" / "my-plugin"
        assert installed.is_symlink()
        skill_link = project / ".gearcore" / "skills" / "alpha"
        assert skill_link.is_symlink()
        assert Path(os.readlink(skill_link)) == installed / "skills" / "alpha"

    def test_copy_skills_copies_whole_plugin_root(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setattr(onboard, "sync", lambda **kwargs: {})
        root = _write_plugin(tmp_path / "plugin", name="my-plugin")

        steps = onboard.run_onboard(root, copy_skills=True, no_sync=True)

        assert [step.action for step in steps] == ["plugin", "skill"]
        installed = home / ".config" / "gearcore" / "plugins" / "my-plugin"
        assert installed.is_dir() and not installed.is_symlink()
        for rel in (
            ".codex-plugin/plugin.json",
            "skills/alpha/SKILL.md",
            "commands/run.toml",
            "orchestration/flow.yaml",
            "scripts/helper.sh",
            "config/settings.json",
            "configs/extra.yaml",
            "tests/test_x.py",
            "docs/guide.md",
        ):
            assert (installed / rel).exists(), rel
        skill_link = home / ".config" / "gearcore" / "skills" / "alpha"
        assert skill_link.is_symlink()
        assert Path(os.readlink(skill_link)) == installed / "skills" / "alpha"

    def test_copy_plugin_root_preserves_external_symlink_without_ingesting_tree(
        self, tmp_path, monkeypatch
    ):
        home = tmp_path / "home"
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setattr(onboard, "sync", lambda **kwargs: {})
        root = _write_plugin(tmp_path / "plugin", name="my-plugin")
        external = tmp_path / "external-support"
        external.mkdir()
        (external / "secret.txt").write_text("outside", encoding="utf-8")
        (root / "scripts" / "external").symlink_to(external, target_is_directory=True)

        onboard.run_onboard(root, copy_skills=True, no_sync=True)

        installed = home / ".config" / "gearcore" / "plugins" / "my-plugin"
        copied_link = installed / "scripts" / "external"
        assert copied_link.is_symlink()
        assert Path(os.readlink(copied_link)) == external

    @pytest.mark.parametrize(
        "frontmatter_name", ["/absolute", "nested/name", "../escape"]
    )
    def test_plugin_skill_frontmatter_name_is_rejected_before_mutation(
        self, tmp_path, monkeypatch, frontmatter_name
    ):
        home = tmp_path / "home"
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setattr(onboard, "sync", lambda **kwargs: {})
        root = _write_plugin(tmp_path / "plugin", name="my-plugin", skills=[])
        skill = root / "skills" / "physical"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(
            f"---\nname: {frontmatter_name}\ndescription: unsafe\n---\n",
            encoding="utf-8",
        )

        with pytest.raises(ValueError, match="Invalid skill name"):
            onboard.run_onboard(root, no_sync=True)

        assert not (home / ".config" / "gearcore" / "plugins").exists()
        assert not (home / ".config" / "gearcore" / "skills").exists()

    def test_legacy_skill_frontmatter_name_is_rejected(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        monkeypatch.setenv("HOME", str(home))
        core = tmp_path / "core"
        skill = core / "skills" / "physical"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(
            "---\nname: nested/name\ndescription: unsafe\n---\n",
            encoding="utf-8",
        )

        with pytest.raises(ValueError, match="Invalid skill name"):
            onboard.run_onboard(core, no_sync=True)

        assert not (home / ".config" / "gearcore" / "skills").exists()

    def test_copy_skills_skill_only_core_keeps_legacy_behavior(
        self, tmp_path, monkeypatch
    ):
        home = tmp_path / "home"
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setattr(onboard, "sync", lambda **kwargs: {})
        core = tmp_path / "core"
        _write_skill_dir(core / "skills", "solo")

        steps = onboard.run_onboard(core, copy_skills=True, no_sync=True)

        assert [step.action for step in steps] == ["skill"]
        dest = home / ".config" / "gearcore" / "skills" / "solo"
        assert dest.is_dir() and not dest.is_symlink()
        assert not (home / ".config" / "gearcore" / "plugins").exists()

    def test_idempotent_rerun_skips_equivalent_roots_and_links(
        self, tmp_path, monkeypatch
    ):
        home = tmp_path / "home"
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setattr(onboard, "sync", lambda **kwargs: {})
        root = _write_plugin(tmp_path / "plugin", name="my-plugin")

        onboard.run_onboard(root, no_sync=True)
        skills_root = home / ".config" / "gearcore" / "skills"
        link_before = os.readlink(skills_root / "alpha")

        steps = onboard.run_onboard(root, no_sync=True)

        assert [step.action for step in steps] == ["plugin", "skill"]
        assert "already matches" in steps[0].detail
        assert "already matches" in steps[1].detail
        assert os.readlink(skills_root / "alpha") == link_before

    def test_dry_run_identifies_plugin_action_and_support_components(
        self, tmp_path, monkeypatch
    ):
        home = tmp_path / "home"
        monkeypatch.setenv("HOME", str(home))
        called = []
        monkeypatch.setattr(
            onboard, "sync", lambda **kwargs: called.append(kwargs) or {}
        )
        root = _write_plugin(tmp_path / "plugin", name="my-plugin")

        steps = onboard.run_onboard(root, dry_run=True)

        assert steps and steps[0].action == "plan"
        detail = steps[0].detail
        assert "plugin" in detail
        assert "Plugin action: add" in detail
        for component in (
            "commands",
            "orchestration",
            "scripts",
            "config",
            "configs",
            "tests",
            "docs",
        ):
            assert component in detail, component
        assert not (home / ".config" / "gearcore" / "plugins").exists()
        assert not (home / ".config" / "gearcore" / "skills").exists()
        assert not called

    def test_conflicting_plugin_destination_aborts_without_mutations(
        self, tmp_path, monkeypatch
    ):
        home = tmp_path / "home"
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setattr(onboard, "sync", lambda **kwargs: {})
        root = _write_plugin(tmp_path / "plugin", name="my-plugin")
        foreign = home / ".config" / "gearcore" / "plugins" / "my-plugin"
        foreign.mkdir(parents=True)
        (foreign / "unrelated.txt").write_text("foreign", encoding="utf-8")

        with pytest.raises(ValueError, match="Plugin destination conflicts"):
            onboard.run_onboard(root, no_sync=True)

        assert (foreign / "unrelated.txt").read_text(encoding="utf-8") == "foreign"
        assert not (home / ".config" / "gearcore" / "skills").exists()

    def test_broken_plugin_symlink_is_a_conflict(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setattr(onboard, "sync", lambda **kwargs: {})
        root = _write_plugin(tmp_path / "plugin", name="my-plugin")
        dest = home / ".config" / "gearcore" / "plugins" / "my-plugin"
        dest.parent.mkdir(parents=True)
        dest.symlink_to(tmp_path / "missing")

        with pytest.raises(ValueError, match="Plugin destination conflicts"):
            onboard.run_onboard(root, no_sync=True)

        assert dest.is_symlink()

    def test_conflicting_skill_destination_prevents_plugin_registration(
        self, tmp_path, monkeypatch
    ):
        home = tmp_path / "home"
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setattr(onboard, "sync", lambda **kwargs: {})
        root = _write_plugin(tmp_path / "plugin", name="my-plugin")
        skills_root = home / ".config" / "gearcore" / "skills"
        (skills_root / "alpha").mkdir(parents=True)
        (skills_root / "alpha" / "SKILL.md").write_text(
            "---\nname: alpha\ndescription: different\n---\n", encoding="utf-8"
        )

        with pytest.raises(ValueError, match="Skill destination conflicts"):
            onboard.run_onboard(root, no_sync=True)

        assert not (home / ".config" / "gearcore" / "plugins").exists()

    def test_broken_skill_symlink_is_a_preflight_conflict(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setattr(onboard, "sync", lambda **kwargs: {})
        root = _write_plugin(tmp_path / "plugin", name="my-plugin")
        dest = home / ".config" / "gearcore" / "skills" / "alpha"
        dest.parent.mkdir(parents=True)
        dest.symlink_to(tmp_path / "missing")

        with pytest.raises(ValueError, match="Skill destination conflicts"):
            onboard.run_onboard(root, no_sync=True)

        assert dest.is_symlink()
        assert not (home / ".config" / "gearcore" / "plugins").exists()

    def test_plugin_without_skills_registers_plugin_only(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setattr(onboard, "sync", lambda **kwargs: {})
        root = _write_plugin(tmp_path / "plugin", name="my-plugin", skills=[])

        steps = onboard.run_onboard(root, no_sync=True)

        assert [step.action for step in steps] == ["plugin"]
        assert (home / ".config" / "gearcore" / "plugins" / "my-plugin").is_symlink()


class TestRemovePlugin:
    def test_remove_plugin_unlinks_registration_and_internal_skill_links(
        self, tmp_path, monkeypatch
    ):
        home = tmp_path / "home"
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setattr(onboard, "sync", lambda **kwargs: {})
        root = _write_plugin(tmp_path / "plugin", name="my-plugin")
        onboard.run_onboard(root, no_sync=True)

        from gearcore_hub.registry import remove_plugin

        remove_plugin("my-plugin")

        assert not (home / ".config" / "gearcore" / "plugins" / "my-plugin").exists()
        skill_link = home / ".config" / "gearcore" / "skills" / "alpha"
        assert not skill_link.is_symlink()
        assert not skill_link.exists()
        # The external source of the symlink must survive removal.
        assert root.is_dir()
        assert (root / "skills" / "alpha" / "SKILL.md").exists()
        assert (root / "commands" / "run.toml").exists()

    def test_remove_plugin_copy_mode_removes_copied_root(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setattr(onboard, "sync", lambda **kwargs: {})
        root = _write_plugin(tmp_path / "plugin", name="my-plugin")
        onboard.run_onboard(root, copy_skills=True, no_sync=True)

        from gearcore_hub.registry import remove_plugin

        remove_plugin("my-plugin")

        assert not (home / ".config" / "gearcore" / "plugins" / "my-plugin").exists()
        skill_link = home / ".config" / "gearcore" / "skills" / "alpha"
        assert not skill_link.is_symlink()
        assert not skill_link.exists()
        assert root.is_dir()

    def test_remove_plugin_leaves_unrelated_skills(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setattr(onboard, "sync", lambda **kwargs: {})
        root = _write_plugin(tmp_path / "plugin", name="my-plugin")
        onboard.run_onboard(root, no_sync=True)
        external = tmp_path / "external-skill"
        _write_skill_dir(tmp_path, "external-skill")
        (home / ".config" / "gearcore" / "skills" / "external-skill").symlink_to(
            external
        )

        from gearcore_hub.registry import remove_plugin

        remove_plugin("my-plugin")

        assert (
            home / ".config" / "gearcore" / "skills" / "external-skill"
        ).is_symlink()

    def test_remove_missing_plugin_errors(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path / "home"))

        from gearcore_hub.registry import remove_plugin

        with pytest.raises(FileNotFoundError, match="not found"):
            remove_plugin("nope")


def test_remove_parser_accepts_plugin_type():
    parser = build_parser()
    args = parser.parse_args(["remove", "plugin", "my-plugin", "--scope", "project"])
    assert args.command == "remove"
    assert args.type == "plugin"
    assert args.name == "my-plugin"
    assert args.scope == "project"
