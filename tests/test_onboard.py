from pathlib import Path

import pytest
import yaml

from gearcore_hub import main as cli_main
from gearcore_hub import onboard
from gearcore_hub.main import build_parser


def _write_skill_dir(path: Path, name: str) -> None:
    skill = path / "skills" / name
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: test\n---\n# {name}\n",
        encoding="utf-8",
    )


def _write_root_skill(path: Path, name: str) -> None:
    (path / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: root skill\n---\n",
        encoding="utf-8",
    )


def _write_pyproject(path: Path, scripts: dict[str, str]) -> None:
    path.mkdir(parents=True, exist_ok=True)
    body = ", ".join([f'"{k}" = "{v}"' for k, v in scripts.items()])
    (path / "pyproject.toml").write_text(
        f'[project]\nname = "core"\nscripts = {{{body}}}\n',
        encoding="utf-8",
    )


def _make_executable(path: Path) -> None:
    path.write_text("#!/bin/sh\n", encoding="utf-8")
    path.chmod(0o755)


class TestDiscovery:
    def test_discover_skill_bundles_and_scripts(self, tmp_path):
        core = tmp_path / "core"
        _write_skill_dir(core, "alpha")
        _write_skill_dir(core, "beta")
        _write_root_skill(core, "root")
        (core / ".venv" / "bin").mkdir(parents=True)
        (core / ".venv" / "bin" / "mycore-mcp").write_text("#!/usr/bin/env python3\n")
        _write_pyproject(
            core,
            {
                "mycore-mcp": "python -m core.mcp",
                "legacy_mcp": "python -m core.legacy",
            },
        )

        skills = onboard.discover_skill_bundles(core)
        names = sorted([s.name for s in skills])
        assert names == ["alpha", "beta", "root"]
        assert onboard.discover_mcp_scripts(core) == ["legacy_mcp", "mycore-mcp"]

    def test_detect_mcp_script(self):
        with pytest.raises(ValueError, match="No MCP script candidates"):
            onboard.select_mcp_script([], None)
        with pytest.raises(ValueError):
            onboard.select_mcp_script(["a-mcp", "b-mcp"], None)
        assert onboard.select_mcp_script(["a-mcp", "b-mcp"], "b-mcp") == "b-mcp"

    def test_mcp_command_requires_regular_executable(self, tmp_path):
        core = tmp_path / "core"
        local = core / ".venv" / "bin" / "core-mcp"
        local.parent.mkdir(parents=True)
        local.write_text("not executable", encoding="utf-8")
        assert onboard.mcp_command(core, "core-mcp") == "core-mcp"
        _make_executable(local)
        assert onboard.mcp_command(core, "core-mcp") == str(local)

    def test_duplicate_discovered_skill_names_are_rejected(self, tmp_path):
        core = tmp_path / "core"
        _write_skill_dir(core, "first")
        _write_skill_dir(core, "second")
        for skill in (core / "skills").iterdir():
            (skill / "SKILL.md").write_text(
                "---\nname: duplicate\n---\n", encoding="utf-8"
            )
        _write_pyproject(core, {"core-mcp": "core:main"})

        with pytest.raises(ValueError, match="Duplicate skill names"):
            onboard.build_onboard_plan(core)


class TestOnboardExecution:
    def test_skill_only_core_registers_skills_without_mcp(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        monkeypatch.setenv("HOME", str(home))
        called = []
        monkeypatch.setattr(
            onboard, "sync", lambda **kwargs: called.append(kwargs) or {}
        )
        core = tmp_path / "core"
        _write_skill_dir(core, "solo")

        steps = onboard.run_onboard(core)

        assert [step.action for step in steps] == ["skill"]
        assert (home / ".config" / "gearcore" / "skills" / "solo").is_symlink()
        assert not (home / ".config" / "gearcore" / "config.yaml").exists()
        assert called

    def test_mcp_only_core_registers_mcp_without_skills(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setattr(onboard, "sync", lambda **kwargs: {})
        core = tmp_path / "core"
        _write_pyproject(core, {"solo-mcp": "solo:main"})

        steps = onboard.run_onboard(core)

        assert [step.action for step in steps] == ["mcp"]
        assert (
            yaml.safe_load((home / ".config" / "gearcore" / "config.yaml").read_text())[
                "registry"
            ]["mcp_servers"][0]["id"]
            == "solo"
        )

    def test_core_without_skills_or_mcp_fails(self, tmp_path):
        with pytest.raises(ValueError, match="No skill bundles or MCP scripts"):
            onboard.run_onboard(tmp_path / "empty")

    @pytest.mark.parametrize("option", ["mcp_script", "mcp_id"])
    def test_explicit_mcp_option_requires_discovered_mcp(self, tmp_path, option):
        core = tmp_path / "core"
        _write_skill_dir(core, "solo")
        kwargs = {option: "solo-mcp" if option == "mcp_script" else "solo"}

        with pytest.raises(ValueError, match="no MCP scripts"):
            onboard.run_onboard(core, **kwargs)

    def test_project_scope_writes_project_registry_and_skills(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(onboard, "sync", lambda **kwargs: {})
        project = tmp_path / "project"
        core = tmp_path / "core"
        _write_skill_dir(core, "finder")
        _write_pyproject(core, {"finder-mcp": "finder:main"})

        onboard.run_onboard(core, scope="project", project_root=project)

        assert (project / ".gearcore" / "skills" / "finder").is_symlink()
        assert yaml.safe_load((project / ".gearcore" / "config.yaml").read_text())[
            "registry"
        ]

    def test_copy_and_symlink_are_equivalent_reregistrations(
        self, tmp_path, monkeypatch
    ):
        home = tmp_path / "home"
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setattr(onboard, "sync", lambda **kwargs: {})
        core = tmp_path / "core"
        _write_skill_dir(core, "finder")

        onboard.run_onboard(core, copy_skills=True, no_sync=True)
        dest = home / ".config" / "gearcore" / "skills" / "finder"
        assert dest.is_dir() and not dest.is_symlink()
        steps = onboard.run_onboard(core, no_sync=True)

        assert "already matches" in steps[0].detail

    def test_explicit_mcp_id_overrides_inferred_id(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setattr(onboard, "sync", lambda **kwargs: {})
        core = tmp_path / "core"
        _write_pyproject(core, {"finder-mcp": "finder:main"})

        onboard.run_onboard(core, mcp_id="custom")

        entries = yaml.safe_load(
            (home / ".config" / "gearcore" / "config.yaml").read_text()
        )["registry"]["mcp_servers"]
        assert entries[0]["id"] == "custom"

    def test_happy_path_registers_mcp_and_skills(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        monkeypatch.setenv("HOME", str(home))
        called = []
        monkeypatch.setattr(
            onboard, "sync", lambda **kwargs: called.append(kwargs) or {}
        )

        core = tmp_path / "core"
        _write_skill_dir(core, "finder")
        (core / ".venv" / "bin").mkdir(parents=True)
        _make_executable(core / ".venv" / "bin" / "finder-mcp")
        _write_pyproject(core, {"finder-mcp": "python -m finder.mcp"})

        steps = onboard.run_onboard(core)
        assert any(s.action == "mcp" and "registered" in s.detail for s in steps)
        assert any(s.action == "skill" and s.target == "finder" for s in steps)

        cfg_path = home / ".config" / "gearcore" / "config.yaml"
        cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
        assert any(
            s["id"] == "finder"
            and s["command"] == str(core / ".venv" / "bin" / "finder-mcp")
            for s in cfg["registry"]["mcp_servers"]
        )
        assert (home / ".config" / "gearcore" / "skills" / "finder").is_symlink()
        assert called, "sync should be invoked"

    def test_relative_core_path_creates_working_links_and_absolute_local_mcp_command(
        self, tmp_path, monkeypatch
    ):
        home = tmp_path / "home"
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(onboard, "sync", lambda **kwargs: {})
        core = tmp_path / "core"
        _write_skill_dir(core, "finder")
        (core / ".venv" / "bin").mkdir(parents=True)
        _make_executable(core / ".venv" / "bin" / "finder-mcp")
        _write_pyproject(core, {"finder-mcp": "python -m finder.mcp"})

        onboard.run_onboard(Path("core"))

        dest = home / ".config" / "gearcore" / "skills" / "finder"
        assert dest.is_symlink()
        assert dest.exists()
        assert dest.resolve() == core / "skills" / "finder"
        cfg = yaml.safe_load(
            (home / ".config" / "gearcore" / "config.yaml").read_text(encoding="utf-8")
        )
        assert cfg["registry"]["mcp_servers"][0]["command"] == str(
            core / ".venv" / "bin" / "finder-mcp"
        )

    def test_idempotent_rerun(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setattr(onboard, "sync", lambda **kwargs: {})
        core = tmp_path / "core"
        _write_skill_dir(core, "finder")
        (core / ".venv" / "bin").mkdir(parents=True)
        (core / ".venv" / "bin" / "finder-mcp").write_text("#!/usr/bin/env python3\n")
        _write_pyproject(core, {"finder-mcp": "python -m finder.mcp"})

        onboard.run_onboard(core)
        cfg_path = home / ".config" / "gearcore" / "config.yaml"
        first = cfg_path.read_text(encoding="utf-8")

        onboard.run_onboard(core)
        second = cfg_path.read_text(encoding="utf-8")

        assert first == second
        # Should skip existing skill registration too
        assert (home / ".config" / "gearcore" / "skills" / "finder").is_symlink()

    def test_dry_run_makes_no_mutations(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        monkeypatch.setenv("HOME", str(home))
        called = []
        monkeypatch.setattr(
            onboard, "sync", lambda **kwargs: called.append(kwargs) or {}
        )
        core = tmp_path / "core"
        _write_skill_dir(core, "finder")
        _write_pyproject(core, {"finder-mcp": "python -m finder.mcp"})

        steps = onboard.run_onboard(core, dry_run=True)
        assert steps and steps[0].action == "plan"
        assert "Plan: onboarding core" in steps[0].detail
        assert not (home / ".config" / "gearcore" / "config.yaml").exists()
        assert not called

    def test_ambiguous_mcp_scripts_requires_override(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        monkeypatch.setenv("HOME", str(home))
        core = tmp_path / "core"
        _write_skill_dir(core, "finder")
        _write_pyproject(
            core,
            {
                "finder-mcp": "python -m finder.mcp",
                "other-mcp": "python -m other.mcp",
            },
        )
        called = []
        monkeypatch.setattr(
            onboard, "sync", lambda **kwargs: called.append(kwargs) or {}
        )

        with pytest.raises(ValueError, match="Multiple MCP script candidates"):
            onboard.run_onboard(core)

        steps = onboard.run_onboard(core, mcp_script="other-mcp")
        assert called
        assert any(s.target == "finder" and s.action == "skill" for s in steps)

    def test_atomic_conflict_preflight_prevents_mutation(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        monkeypatch.setenv("HOME", str(home))
        core = tmp_path / "core"
        _write_skill_dir(core, "finder")
        (home / ".config" / "gearcore" / "skills" / "finder").mkdir(parents=True)
        monkeypatch.setattr(onboard, "sync", lambda **kwargs: {})
        (home / ".config" / "gearcore" / "skills" / "finder" / "notes.txt").write_text(
            "wrong", encoding="utf-8"
        )
        _write_pyproject(core, {"finder-mcp": "python -m finder.mcp"})
        cfg_path = home / ".config" / "gearcore" / "config.yaml"
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        cfg_path.write_text(yaml.dump({"version": 2, "registry": {"mcp_servers": []}}))

        with pytest.raises(ValueError, match="Skill destination conflicts"):
            onboard.run_onboard(core)

        # No mutation expected: mcp entry still empty and conflict directory untouched.
        assert cfg_path.read_text(encoding="utf-8") == yaml.dump(
            {"version": 2, "registry": {"mcp_servers": []}}
        )

    def test_conflicting_mcp_preflight_prevents_skill_mutation(
        self, tmp_path, monkeypatch
    ):
        home = tmp_path / "home"
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setattr(onboard, "sync", lambda **kwargs: {})
        core = tmp_path / "core"
        _write_skill_dir(core, "finder")
        _write_pyproject(core, {"finder-mcp": "finder:main"})
        cfg_path = home / ".config" / "gearcore" / "config.yaml"
        cfg_path.parent.mkdir(parents=True)
        cfg_path.write_text(
            yaml.dump(
                {
                    "registry": {
                        "mcp_servers": [
                            {"id": "finder", "type": "stdio", "command": "other"}
                        ]
                    }
                }
            ),
            encoding="utf-8",
        )

        with pytest.raises(ValueError, match="Conflicting MCP entry"):
            onboard.run_onboard(core)

        assert not (home / ".config" / "gearcore" / "skills" / "finder").exists()

    def test_copied_skill_with_changed_same_named_file_is_conflict(
        self, tmp_path, monkeypatch
    ):
        home = tmp_path / "home"
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setattr(onboard, "sync", lambda **kwargs: {})
        core = tmp_path / "core"
        _write_skill_dir(core, "finder")
        dest = home / ".config" / "gearcore" / "skills" / "finder"
        dest.mkdir(parents=True)
        (dest / "SKILL.md").write_text(
            "---\nname: finder\ndescription: changed\n---\n# finder\n",
            encoding="utf-8",
        )

        with pytest.raises(ValueError, match="Skill destination conflicts"):
            onboard.run_onboard(core)

    def test_broken_skill_symlink_is_a_preflight_conflict(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        monkeypatch.setenv("HOME", str(home))
        core = tmp_path / "core"
        _write_skill_dir(core, "finder")
        dest = home / ".config" / "gearcore" / "skills" / "finder"
        dest.parent.mkdir(parents=True)
        dest.symlink_to(tmp_path / "missing")

        with pytest.raises(ValueError, match="Skill destination conflicts"):
            onboard.run_onboard(core)

    def test_no_sync_and_tool_filters_are_honored(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        monkeypatch.setenv("HOME", str(home))
        calls = []
        monkeypatch.setattr(
            onboard, "sync", lambda **kwargs: calls.append(kwargs) or {}
        )
        core = tmp_path / "core"
        _write_skill_dir(core, "finder")

        onboard.run_onboard(core, tool_filter=["codex"], no_sync=True)
        assert calls == []
        onboard.run_onboard(core, tool_filter=["codex"])
        assert calls == [{"tools": ["codex"], "dry_run": False, "remove": False}]


def test_onboard_parser_includes_dispatch_flags():
    parser = build_parser()
    args = parser.parse_args(
        [
            "--project",
            "/tmp/project",
            "onboard",
            "/tmp/core",
            "--mcp-id",
            "core-mcp",
            "--copy-skills",
            "--scope",
            "project",
            "--tool",
            "claude",
            "codex",
            "--dry-run",
            "--no-sync",
        ]
    )
    assert args.command == "onboard"
    assert args.mcp_id == "core-mcp"
    assert args.copy_skills is True
    assert args.scope == "project"
    assert args.tool == ["claude", "codex"]
    assert args.dry_run is True
    assert args.no_sync is True
    assert args.project == "/tmp/project"


def test_onboard_dispatches_to_command(monkeypatch, tmp_path):
    calls = {}

    def fake_cmd(args, project_path):
        calls["core_path"] = args.core_path
        calls["scope"] = args.scope
        calls["project_path"] = project_path

    monkeypatch.setattr(cli_main, "cmd_onboard", fake_cmd)
    monkeypatch.setattr(cli_main, "load_config", lambda *_, **__: object())
    monkeypatch.setattr(
        "sys.argv", ["gearcore", "--project", str(tmp_path), "onboard", "/tmp/core"]
    )

    cli_main.main()

    assert calls["core_path"] == "/tmp/core"
    assert calls["scope"] == "global"
    assert str(calls["project_path"]) == str(tmp_path)
