"""Tests for registry mutation commands (add-mcp / remove-mcp)."""

from pathlib import Path

import pytest
import yaml

from gearcore_hub import registry
from gearcore_hub.config import load_config


def _write_global(path: Path, servers: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.dump({"version": 2, "registry": {"mcp_servers": servers}}))


def _read_project(project_root: Path) -> dict:
    return yaml.safe_load(
        (project_root / ".gearcore" / "config.yaml").read_text()
    )


class TestAddMcpProjectAllowlist:
    def test_allowlist_appends_global_id_to_scope_include(self, tmp_path, monkeypatch):
        global_cfg = tmp_path / "global.yaml"
        _write_global(global_cfg, [{"id": "fs", "type": "stdio", "command": "npx"}])
        monkeypatch.setattr(registry, "GLOBAL_CONFIG_PATH", global_cfg)
        project = tmp_path / "proj"
        (project / ".gearcore").mkdir(parents=True)

        registry.add_mcp(
            id="fs", type="stdio", scope="project", project_root=project, allowlist=True
        )

        data = _read_project(project)
        assert data["scope"]["mcp_servers"]["include"] == ["fs"]
        assert "registry" not in data

    def test_allowlist_rejects_unknown_global_id(self, tmp_path, monkeypatch):
        global_cfg = tmp_path / "global.yaml"
        _write_global(global_cfg, [])
        monkeypatch.setattr(registry, "GLOBAL_CONFIG_PATH", global_cfg)
        project = tmp_path / "proj"
        (project / ".gearcore").mkdir(parents=True)

        with pytest.raises(ValueError, match="not registered globally"):
            registry.add_mcp(
                id="ghost",
                type="stdio",
                scope="project",
                project_root=project,
                allowlist=True,
            )

    def test_allowlist_rejects_duplicate(self, tmp_path, monkeypatch):
        global_cfg = tmp_path / "global.yaml"
        _write_global(global_cfg, [{"id": "fs", "type": "stdio", "command": "npx"}])
        monkeypatch.setattr(registry, "GLOBAL_CONFIG_PATH", global_cfg)
        project = tmp_path / "proj"
        (project / ".gearcore").mkdir(parents=True)
        (project / ".gearcore" / "config.yaml").write_text(
            yaml.dump({"scope": {"mcp_servers": {"include": ["fs"]}}})
        )

        with pytest.raises(ValueError, match="already allowlisted"):
            registry.add_mcp(
                id="fs",
                type="stdio",
                scope="project",
                project_root=project,
                allowlist=True,
            )

    def test_allowlist_requires_project_scope(self, tmp_path):
        with pytest.raises(ValueError, match="--scope project"):
            registry.add_mcp(id="fs", type="stdio", scope="global", allowlist=True)


class TestAddMcpRoundTrip:
    def test_project_def_round_trips_through_load_config(self, tmp_path, monkeypatch):
        global_cfg = tmp_path / "global.yaml"
        _write_global(global_cfg, [])
        monkeypatch.setattr(registry, "GLOBAL_CONFIG_PATH", global_cfg)
        project = tmp_path / "proj"
        (project / ".gearcore").mkdir(parents=True)

        registry.add_mcp(
            id="gw",
            type="sse",
            url="http://127.0.0.1:8765/sse",
            scope="project",
            project_root=project,
        )

        cfg = load_config(project=project, global_config_path=global_cfg)
        assert [s.id for s in cfg.mcp_servers] == ["gw"]
        assert cfg.mcp_servers[0].url == "http://127.0.0.1:8765/sse"


class TestRemoveMcpProject:
    def test_removes_from_both_registry_def_and_include(self, tmp_path):
        project = tmp_path / "proj"
        (project / ".gearcore").mkdir(parents=True)
        (project / ".gearcore" / "config.yaml").write_text(
            yaml.dump(
                {
                    "scope": {"mcp_servers": {"include": ["gw"]}},
                    "registry": {
                        "mcp_servers": [
                            {"id": "gw", "type": "sse", "url": "http://x"}
                        ]
                    },
                }
            )
        )

        registry.remove_mcp("gw", scope="project", project_root=project)

        data = _read_project(project)
        assert data["registry"]["mcp_servers"] == []
        assert data["scope"]["mcp_servers"]["include"] == []

    def test_missing_id_raises(self, tmp_path):
        project = tmp_path / "proj"
        (project / ".gearcore").mkdir(parents=True)
        (project / ".gearcore" / "config.yaml").write_text("version: 2\n")

        with pytest.raises(KeyError):
            registry.remove_mcp("nope", scope="project", project_root=project)
