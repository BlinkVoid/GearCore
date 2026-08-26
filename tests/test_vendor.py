"""Tests for the bundled superpowers vendor module."""

import json
import stat
from pathlib import Path

import pytest

from gearcore_hub.vendor import (
    VendorManifest,
    bundled_superpowers_dir,
    get_upstream_commit,
    load_vendor_manifest,
    sync_vendor_bundle,
    update_superpowers,
)


def test_bundled_superpowers_dir_returns_path_when_skills_exist(tmp_path, monkeypatch):
    fake_root = tmp_path / "third_party" / "superpowers"
    fake_root.mkdir(parents=True)
    (fake_root / "skills").mkdir()
    monkeypatch.setattr("gearcore_hub.vendor.VENDOR_ROOT", fake_root)
    assert bundled_superpowers_dir() == fake_root / "skills"


def test_bundled_superpowers_dir_returns_none_when_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "gearcore_hub.vendor.VENDOR_ROOT", tmp_path / "third_party" / "superpowers"
    )
    assert bundled_superpowers_dir() is None


def test_load_vendor_manifest_parses_json(tmp_path, monkeypatch):
    fake_root = tmp_path / "third_party" / "superpowers"
    fake_root.mkdir(parents=True)
    manifest = {
        "name": "superpowers",
        "source": "https://github.com/obra/superpowers.git",
        "source_ref": "main",
        "vendored_commit": "abc123",
        "vendored_at": "2026-07-05",
        "paths": ["skills/*"],
    }
    (fake_root / ".vendor.json").write_text(json.dumps(manifest))
    monkeypatch.setattr("gearcore_hub.vendor.VENDOR_ROOT", fake_root)
    result = load_vendor_manifest()
    assert isinstance(result, VendorManifest)
    assert result.vendored_commit == "abc123"


def test_load_vendor_manifest_returns_none_when_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "gearcore_hub.vendor.VENDOR_ROOT", tmp_path / "third_party" / "superpowers"
    )
    assert load_vendor_manifest() is None


def test_get_upstream_commit_returns_sha(monkeypatch):
    def fake_run(cmd, **kwargs):
        class Result:
            stdout = "abc123\trefs/heads/main\n"
            stderr = ""
            returncode = 0

        return Result()

    monkeypatch.setattr("gearcore_hub.vendor.subprocess.run", fake_run)
    assert get_upstream_commit("https://example.com/repo.git", "main") == "abc123"


def test_get_upstream_commit_returns_none_on_failure(monkeypatch):
    def fake_run(cmd, **kwargs):
        raise RuntimeError("network error")

    monkeypatch.setattr("gearcore_hub.vendor.subprocess.run", fake_run)
    assert get_upstream_commit("https://example.com/repo.git", "main") is None


def test_sync_vendor_bundle_dry_run():
    manifest = VendorManifest(
        name="superpowers",
        source="https://example.com/repo.git",
        source_ref="main",
        vendored_commit="abc123",
        vendored_at="2026-07-05",
        paths=["skills/*"],
    )
    result = sync_vendor_bundle(manifest, Path("/src"), Path("/dest"), dry_run=True)
    assert result == {"changed": True, "dry_run": True}


def test_sync_vendor_bundle_copies_paths_and_writes_manifest(tmp_path):
    source_dir = tmp_path / "source"
    dest_root = tmp_path / "dest"
    (source_dir / "skills" / "foo").mkdir(parents=True)
    (source_dir / "skills" / "foo" / "SKILL.md").write_text("hello")

    manifest = VendorManifest(
        name="superpowers",
        source="https://example.com/repo.git",
        source_ref="main",
        vendored_commit="abc123",
        vendored_at="2026-07-05",
        paths=["skills/*"],
    )
    result = sync_vendor_bundle(manifest, source_dir, dest_root)
    assert result["changed"] is True
    assert (dest_root / "skills" / "foo" / "SKILL.md").exists()
    assert (dest_root / ".vendor.json").exists()


def test_sync_vendor_bundle_copies_skills_and_updates_manifest(tmp_path):
    source_dir = tmp_path / "source"
    skills_dir = source_dir / "skills"
    skills_dir.mkdir(parents=True)
    (skills_dir / "using-superpowers").mkdir()
    (skills_dir / "using-superpowers" / "SKILL.md").write_text("# Using Superpowers")
    (skills_dir / "using-superpowers" / "manifest.json").write_text(
        '{"name": "using-superpowers"}'
    )

    dest_root = tmp_path / "dest"
    dest_root.mkdir()
    manifest = VendorManifest(
        name="superpowers",
        source="https://example.com/superpowers.git",
        source_ref="main",
        vendored_commit="old123",
        vendored_at="2026-01-01",
        paths=["skills/*"],
    )

    sync_vendor_bundle(
        manifest.model_copy(update={"vendored_commit": "new456"}),
        source_dir,
        dest_root,
    )

    assert (dest_root / "skills" / "using-superpowers" / "SKILL.md").exists()
    updated = json.loads((dest_root / ".vendor.json").read_text())
    assert updated["vendored_commit"] == "new456"
    assert updated["vendored_at"] != "2026-01-01"


VENDORED_EXECUTABLE_SCRIPTS = [
    "subagent-driven-development/scripts/sdd-workspace",
    "subagent-driven-development/scripts/task-brief",
    "subagent-driven-development/scripts/review-package",
    "brainstorming/scripts/start-server.sh",
    "brainstorming/scripts/stop-server.sh",
    "systematic-debugging/find-polluter.sh",
]


@pytest.mark.parametrize("rel_path", VENDORED_EXECUTABLE_SCRIPTS)
def test_vendored_scripts_are_executable(rel_path):
    script = bundled_superpowers_dir() / rel_path
    assert script.exists(), f"missing vendored script: {rel_path}"
    mode = script.stat().st_mode
    assert mode & stat.S_IXUSR, f"vendored script not executable: {rel_path}"


def test_update_superpowers_raises_when_no_manifest(tmp_path, monkeypatch):
    monkeypatch.setattr("gearcore_hub.vendor.VENDOR_ROOT", tmp_path)
    with pytest.raises(RuntimeError, match="No superpowers vendor manifest found"):
        update_superpowers()


def test_update_superpowers_returns_unchanged_when_commit_matches(
    tmp_path, monkeypatch
):
    manifest = VendorManifest(
        name="superpowers",
        source="https://example.com/repo.git",
        source_ref="main",
        vendored_commit="abc123",
        vendored_at="2026-07-05",
        paths=["skills/*"],
    )
    (tmp_path / ".vendor.json").write_text(manifest.model_dump_json())
    monkeypatch.setattr("gearcore_hub.vendor.VENDOR_ROOT", tmp_path)

    def fake_get_upstream_commit(source, ref):
        return "abc123"

    monkeypatch.setattr(
        "gearcore_hub.vendor.get_upstream_commit", fake_get_upstream_commit
    )
    result = update_superpowers()
    assert result == {"changed": False, "upstream": "abc123"}


def test_update_superpowers_dry_run_reports_change(tmp_path, monkeypatch):
    manifest = VendorManifest(
        name="superpowers",
        source="https://example.com/repo.git",
        source_ref="main",
        vendored_commit="abc123",
        vendored_at="2026-07-05",
        paths=["skills/*"],
    )
    (tmp_path / ".vendor.json").write_text(manifest.model_dump_json())
    monkeypatch.setattr("gearcore_hub.vendor.VENDOR_ROOT", tmp_path)

    def fake_get_upstream_commit(source, ref):
        return "def456"

    monkeypatch.setattr(
        "gearcore_hub.vendor.get_upstream_commit", fake_get_upstream_commit
    )
    result = update_superpowers(dry_run=True)
    assert result == {"changed": True, "upstream": "def456", "dry_run": True}
