"""Tests for the bundled superpowers vendor module."""

import json
import stat
import subprocess
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


def _shebang_scripts() -> list[Path]:
    skills_dir = bundled_superpowers_dir()
    if skills_dir is None:
        return []
    return [
        p
        for p in sorted(skills_dir.rglob("*"))
        if p.is_file() and p.read_bytes()[:2] == b"#!"
    ]


def test_vendored_shebang_scripts_are_executable():
    scripts = _shebang_scripts()
    assert scripts, "expected at least one shebang script in the vendored bundle"
    for script in scripts:
        mode = script.stat().st_mode
        assert mode & stat.S_IXUSR, f"vendored script not executable: {script.name}"


def test_git_tracks_vendored_scripts_as_executable():
    scripts = _shebang_scripts()
    assert scripts, "expected at least one shebang script in the vendored bundle"
    try:
        result = subprocess.run(
            ["git", "ls-files", "-s", *[str(p) for p in scripts]],
            capture_output=True,
            text=True,
            check=True,
            timeout=30.0,
        )
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        pytest.skip("not running inside a git checkout")
    tracked = {
        Path(line.split(maxsplit=3)[-1]): line.split()[0]
        for line in result.stdout.splitlines()
        if line.strip()
    }
    assert len(tracked) == len(scripts), "some vendored scripts are untracked"
    for script, mode in tracked.items():
        assert mode == "100755", f"git tracks {script} as {mode}, expected 100755"


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


def test_get_upstream_commit_prefers_branch_over_same_named_tag(monkeypatch):
    def fake_run(cmd, **kwargs):
        class Result:
            stdout = "aaaaaaa\trefs/tags/main\nbbbbbbb\trefs/heads/main\n"
            stderr = ""
            returncode = 0

        return Result()

    monkeypatch.setattr("gearcore_hub.vendor.subprocess.run", fake_run)
    assert get_upstream_commit("https://example.com/repo.git", "main") == "bbbbbbb"


def test_get_upstream_commit_falls_back_to_tag_when_no_branch(monkeypatch):
    def fake_run(cmd, **kwargs):
        class Result:
            stdout = "ccccccc\trefs/tags/v1.0\n"
            stderr = ""
            returncode = 0

        return Result()

    monkeypatch.setattr("gearcore_hub.vendor.subprocess.run", fake_run)
    assert get_upstream_commit("https://example.com/repo.git", "v1.0") == "ccccccc"


def test_get_upstream_commit_accepts_explicit_ref(monkeypatch):
    def fake_run(cmd, **kwargs):
        class Result:
            stdout = "ddddddd\trefs/pull/42/head\n"
            stderr = ""
            returncode = 0

        return Result()

    monkeypatch.setattr("gearcore_hub.vendor.subprocess.run", fake_run)
    assert (
        get_upstream_commit("https://example.com/repo.git", "refs/pull/42/head")
        == "ddddddd"
    )
