"""Tests for the layered configuration loader."""

from pathlib import Path

from gearcore_hub.config import (
    EffectiveConfig,
    GlobalConfig,
    ProjectConfig,
    _default_skills_dirs,
    load_config,
)
from gearcore_hub.vendor import bundled_superpowers_dir


class TestGlobalConfig:
    def test_empty_config(self):
        cfg = GlobalConfig()
        assert cfg.version == 2
        assert cfg.mcp_servers == []
        assert cfg.skills_dirs == _default_skills_dirs()

    def test_mcp_servers_parsing(self):
        data = {
            "registry": {
                "mcp_servers": [
                    {"id": "fs", "type": "stdio", "command": "npx", "args": ["-y"]}
                ]
            }
        }
        cfg = GlobalConfig(**data)
        assert len(cfg.mcp_servers) == 1
        assert cfg.mcp_servers[0].id == "fs"
        assert cfg.mcp_servers[0].args == ["-y"]

    def test_disabled_server_filtered_in_effective(self):
        data = {
            "registry": {
                "mcp_servers": [
                    {"id": "fs", "type": "stdio", "command": "npx", "enabled": True},
                    {"id": "old", "type": "stdio", "command": "npx", "enabled": False},
                ]
            }
        }
        global_cfg = GlobalConfig(**data)
        effective = EffectiveConfig(global_cfg, None, None)
        assert len(effective.mcp_servers) == 1
        assert effective.mcp_servers[0].id == "fs"


class TestProjectConfig:
    def test_allowlist_parsing(self):
        data = {
            "scope": {
                "mcp_servers": {"include": ["fs"]},
                "skills": {"include": ["web-research"]},
            }
        }
        cfg = ProjectConfig(**data)
        assert cfg.mcp_allowlist == ["fs"]
        assert cfg.skill_allowlist == ["web-research"]

    def test_no_allowlist_means_allow_all(self):
        cfg = ProjectConfig()
        assert cfg.mcp_allowlist is None
        assert cfg.skill_allowlist is None


class TestEffectiveConfig:
    def test_global_only(self):
        global_cfg = GlobalConfig(
            registry={
                "mcp_servers": [
                    {"id": "fs", "type": "stdio", "command": "npx"},
                    {"id": "web", "type": "sse", "url": "http://localhost"},
                ]
            }
        )
        effective = EffectiveConfig(global_cfg, None, None)
        assert len(effective.mcp_servers) == 2
        assert effective.context_name == "global"

    def test_project_filters_mcp_servers(self):
        global_cfg = GlobalConfig(
            registry={
                "mcp_servers": [
                    {"id": "fs", "type": "stdio", "command": "npx"},
                    {"id": "web", "type": "sse", "url": "http://localhost"},
                ]
            }
        )
        project_cfg = ProjectConfig(scope={"mcp_servers": {"include": ["fs"]}})
        effective = EffectiveConfig(global_cfg, project_cfg, Path("/tmp/fake"))
        assert len(effective.mcp_servers) == 1
        assert effective.mcp_servers[0].id == "fs"
        assert effective.context_name == "global"  # no project name set

    def test_project_local_skills_dir_appended(self):
        global_cfg = GlobalConfig()
        project_cfg = ProjectConfig()
        effective = EffectiveConfig(global_cfg, project_cfg, Path("/tmp/fake"))
        assert any(".gearcore/skills" in str(d) for d in effective.skills_dirs)


class TestLoadConfig:
    def test_loads_from_explicit_global_path(self, tmp_path: Path):
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            "version: 2\nregistry:\n  mcp_servers:\n    - id: test\n      type: stdio\n      command: echo\n"
        )
        cfg = load_config(global_config_path=config_file)
        assert cfg.context_name == "global"
        assert len(cfg.mcp_servers) == 1

    def test_missing_config_is_graceful(self, tmp_path: Path):
        config_file = tmp_path / "nonexistent.yaml"
        cfg = load_config(global_config_path=config_file)
        assert cfg.context_name == "global"
        assert cfg.mcp_servers == []


def test_default_skills_dirs_include_bundled_superpowers(monkeypatch, tmp_path):
    fake_root = tmp_path / "third_party" / "superpowers"
    (fake_root / "skills").mkdir(parents=True)
    monkeypatch.setattr("gearcore_hub.vendor.VENDOR_ROOT", fake_root)
    cfg = GlobalConfig()
    assert bundled_superpowers_dir() in cfg.skills_dirs
