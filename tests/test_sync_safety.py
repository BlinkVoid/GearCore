"""Tests for the destructive paths in the sync command."""

from pathlib import Path

import pytest

from gearcore_hub import sync as sync_mod


@pytest.fixture()
def isolated_paths(tmp_path, monkeypatch):
    """Point all sync module paths at a temp dir."""
    canonical = tmp_path / "canonical" / "gearcore"
    source = tmp_path / "self-skill-src"
    links = {
        "claude": tmp_path / "claude" / "skills" / "gearcore",
        "codex": tmp_path / "codex" / "skills" / "gearcore",
        "kimi": tmp_path / "kimi" / "skills" / "gearcore",
        "opencode": tmp_path / "opencode" / "skills" / "gearcore",
    }
    monkeypatch.setattr(sync_mod, "CANONICAL_DIR", canonical)
    monkeypatch.setattr(sync_mod, "TOOL_LINK_PATHS", links)
    monkeypatch.setattr(sync_mod, "SELF_SKILL_SOURCE", source)
    return canonical, source, links


def _make_self_skill_source(source: Path) -> None:
    source.mkdir(parents=True)
    (source / "SKILL.md").write_text("# gearcore\n")
    (source / "manifest.json").write_text('{"name": "gearcore"}')


class TestInstallCanonicalSafety:
    def test_fresh_install_copies_bundle(self, isolated_paths):
        canonical, source, _ = isolated_paths
        _make_self_skill_source(source)

        ok = sync_mod._install_canonical()

        assert ok is True
        assert (canonical / "SKILL.md").exists()
        assert (canonical / "manifest.json").exists()

    def test_refuses_to_rmtree_non_gearcore_directory(self, isolated_paths):
        canonical, source, _ = isolated_paths
        _make_self_skill_source(source)
        canonical.mkdir(parents=True)
        (canonical / "user-data.txt").write_text("precious")

        ok = sync_mod._install_canonical()

        assert ok is False
        assert (canonical / "user-data.txt").read_text() == "precious"

    def test_replaces_existing_gearcore_install(self, isolated_paths):
        canonical, source, _ = isolated_paths
        _make_self_skill_source(source)
        canonical.mkdir(parents=True)
        (canonical / "SKILL.md").write_text("# stale gearcore")
        (canonical / "manifest.json").write_text('{"name": "gearcore"}')
        (canonical / "stale-file.txt").write_text("old")

        ok = sync_mod._install_canonical()

        assert ok is True
        assert not (canonical / "stale-file.txt").exists()
        assert (canonical / "manifest.json").exists()


class TestLinkToolSafety:
    def test_refuses_to_replace_unrelated_real_directory(self, isolated_paths):
        _, source, links = isolated_paths
        _make_self_skill_source(source)
        link = links["claude"]
        link.parent.mkdir(parents=True)
        link.mkdir()
        (link / "unrelated.txt").write_text("mine")

        with pytest.raises(sync_mod.UnsafeTargetError):
            sync_mod._link_tool("claude")

        assert (link / "unrelated.txt").read_text() == "mine"

    def test_replaces_stale_gearcore_directory(self, isolated_paths):
        _, source, links = isolated_paths
        _make_self_skill_source(source)
        link = links["claude"]
        link.parent.mkdir(parents=True)
        link.mkdir()
        (link / "SKILL.md").write_text("# old copy")
        (link / "manifest.json").write_text('{"name": "gearcore"}')

        result = sync_mod._link_tool("claude")

        assert result == link
        assert link.is_symlink()


class TestSyncReportsRefusals:
    def test_sync_surfaces_link_refusal(self, isolated_paths, monkeypatch):
        _, source, links = isolated_paths
        _make_self_skill_source(source)
        link = links["codex"]
        link.parent.mkdir(parents=True)
        link.mkdir()
        (link / "unrelated.txt").write_text("mine")
        monkeypatch.setattr(sync_mod, "_detect_installed_tools", lambda: ["codex"])

        results = sync_mod.sync()

        assert results["canonical"] == "installed"
        assert "refused" in results["codex"]
        assert (link / "unrelated.txt").read_text() == "mine"
