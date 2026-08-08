"""Tests for registry mutation commands."""

import json
import multiprocessing
import os
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from gearcore_hub import registry
from gearcore_hub.config import load_config


def _process_set_profile(config_path: str, profile: str) -> None:
    registry.set_profile(profile, config_path=Path(config_path))


def _process_add_mcp(config_path: str) -> None:
    registry.GLOBAL_CONFIG_PATH = Path(config_path)
    registry.add_mcp(id="process-added", type="stdio", command="safe")


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


class TestSkillManifestIdentityMutation:
    def test_renamed_directory_cannot_remove_protected_manifest_id(
        self, tmp_path, monkeypatch
    ):
        skills = tmp_path / "skills"
        _write_manifest_skill(
            skills, "renamed-directory", manifest_name="protected-id"
        )
        config = tmp_path / "config.yaml"
        config.write_text(
            yaml.safe_dump(
                {
                    "version": 3,
                    "registry": {"skills_dirs": [str(skills)]},
                    "profiles": {
                        "default": "operator",
                        "entries": {
                            "operator": {
                                "scope": {
                                    "skills": {"protected": ["protected-id"]}
                                }
                            }
                        },
                    },
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr(registry, "GLOBAL_CONFIG_PATH", config)
        monkeypatch.setattr(
            registry,
            "_skills_dir",
            lambda _scope, _project_root: skills,
        )

        with pytest.raises(ValueError, match="protected or core") as exc:
            registry.remove_skill("renamed-directory")

        assert str(tmp_path) not in str(exc.value)
        assert (skills / "renamed-directory").is_dir()

    def test_add_rejects_duplicate_manifest_identity_across_directory_names(
        self, tmp_path, monkeypatch
    ):
        skills = tmp_path / "skills"
        _write_manifest_skill(skills, "current-dir", manifest_name="same-id")
        source_root = tmp_path / "source"
        _write_manifest_skill(source_root, "new-dir", manifest_name="same-id")
        config = tmp_path / "config.yaml"
        config.write_text("version: 2\n", encoding="utf-8")
        monkeypatch.setattr(registry, "GLOBAL_CONFIG_PATH", config)
        monkeypatch.setattr(
            registry,
            "_skills_dir",
            lambda _scope, _project_root: skills,
        )

        with pytest.raises(ValueError, match="skill identity conflict") as exc:
            registry.add_skill(source_root / "new-dir")

        assert str(tmp_path) not in str(exc.value)
        assert not (skills / "new-dir").exists()

    def test_add_rejects_malformed_manifest_atomically(
        self, tmp_path, monkeypatch
    ):
        skills = tmp_path / "skills"
        source_root = tmp_path / "source"
        _write_manifest_skill(
            source_root, "broken-bundle", manifest_name=None, malformed=True
        )
        config = tmp_path / "config.yaml"
        config.write_text("version: 2\n", encoding="utf-8")
        monkeypatch.setattr(registry, "GLOBAL_CONFIG_PATH", config)
        monkeypatch.setattr(
            registry,
            "_skills_dir",
            lambda _scope, _project_root: skills,
        )

        with pytest.raises(ValueError, match="invalid skill manifest") as exc:
            registry.add_skill(source_root / "broken-bundle")

        assert str(tmp_path) not in str(exc.value)
        assert not skills.exists()

    def test_add_scans_all_configured_global_skill_roots(
        self, tmp_path, monkeypatch
    ):
        first_root = tmp_path / "first-root"
        second_root = tmp_path / "second-root"
        destination = tmp_path / "managed-destination"
        source_root = tmp_path / "source"
        first_root.mkdir()
        _write_manifest_skill(
            second_root, "protected-binding", manifest_name="protected-id"
        )
        _write_manifest_skill(
            source_root, "new-directory", manifest_name="protected-id"
        )
        config = tmp_path / "config.yaml"
        config.write_text(
            yaml.safe_dump(
                {
                    "version": 3,
                    "registry": {
                        "skills_dirs": [str(first_root), str(second_root)]
                    },
                    "profiles": {
                        "default": "operator",
                        "entries": {
                            "operator": {
                                "scope": {
                                    "skills": {"protected": ["protected-id"]}
                                }
                            }
                        },
                    },
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr(registry, "GLOBAL_CONFIG_PATH", config)
        monkeypatch.setattr(
            registry,
            "_skills_dir",
            lambda _scope, _project_root: destination,
        )

        with pytest.raises(ValueError, match="skill identity conflict") as exc:
            registry.add_skill(source_root / "new-directory")

        assert str(tmp_path) not in str(exc.value)
        assert not destination.exists()

    def test_add_cli_scans_all_configured_global_skill_roots(
        self, tmp_path, monkeypatch
    ):
        first_root = tmp_path / "first-root"
        second_root = tmp_path / "second-root"
        destination = tmp_path / "managed-cli-destination"
        first_root.mkdir()
        _write_manifest_skill(
            second_root, "protected-binding", manifest_name="tool"
        )
        config = tmp_path / "config.yaml"
        config.write_text(
            yaml.safe_dump(
                {
                    "version": 3,
                    "registry": {
                        "skills_dirs": [str(first_root), str(second_root)]
                    },
                    "profiles": {
                        "default": "operator",
                        "entries": {
                            "operator": {
                                "scope": {"skills": {"protected": ["tool"]}}
                            }
                        },
                    },
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr(registry, "GLOBAL_CONFIG_PATH", config)
        monkeypatch.setattr(
            registry,
            "_skills_dir",
            lambda _scope, _project_root: destination,
        )
        monkeypatch.setattr(registry.shutil, "which", lambda _program: "/safe")
        monkeypatch.setattr(
            registry.subprocess,
            "run",
            lambda *_args, **_kwargs: subprocess.CompletedProcess(
                args=["cli-anything"], returncode=0, stdout="{}", stderr=""
            ),
        )

        with pytest.raises(ValueError, match="skill identity conflict") as exc:
            registry.add_cli("tool")

        assert str(tmp_path) not in str(exc.value)
        assert not destination.exists()


def _write_skill(root: Path, name: str) -> None:
    bundle = root / name
    bundle.mkdir(parents=True)
    (bundle / "SKILL.md").write_text(f"# {name}\n", encoding="utf-8")


def _write_manifest_skill(
    root: Path,
    directory_name: str,
    *,
    manifest_name: str | None,
    malformed: bool = False,
) -> None:
    bundle = root / directory_name
    bundle.mkdir(parents=True)
    (bundle / "SKILL.md").write_text("# safe\n", encoding="utf-8")
    if malformed:
        (bundle / "manifest.json").write_text("{invalid", encoding="utf-8")
    elif manifest_name is not None:
        (bundle / "manifest.json").write_text(
            json.dumps({"name": manifest_name}), encoding="utf-8"
        )


def _profile_global(path: Path, skills: Path) -> None:
    _write_skill(skills, "hive-dispatcher")
    _write_skill(skills, "existing-core")
    path.write_text(
        yaml.safe_dump(
            {
                "version": 2,
                "registry": {
                    "skills_dirs": [str(skills)],
                    "mcp_servers": [
                        {
                            "id": "hive-dispatcher",
                            "type": "stdio",
                            "command": "secret-command-sentinel",
                            "auth": {
                                "credential_ref": "hive-operator",
                                "stdio_environment": "HIVE_AUTH",
                            },
                        },
                        {"id": "safe", "type": "stdio", "command": "safe-bin"},
                    ],
                },
                "disclosure": {"core_skills": ["existing-core"]},
                "unrelated": {"keep": True},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


class TestSetProfile:
    def test_creates_operator_and_constrained_worker_profiles_idempotently(
        self, tmp_path
    ):
        config = tmp_path / "config.yaml"
        skills = tmp_path / "skills"
        _profile_global(config, skills)

        first = registry.set_profile(
            "operator",
            config_path=config,
            mcp_include=("hive-dispatcher", "safe"),
            mcp_protect=("hive-dispatcher",),
            skill_include=("hive-dispatcher", "existing-core"),
            skill_protect=("hive-dispatcher",),
            core_skills=("hive-dispatcher",),
            make_default=True,
        )
        before = config.read_bytes()
        before_mtime = config.stat().st_mtime_ns
        second = registry.set_profile(
            "operator",
            config_path=config,
            mcp_include=("hive-dispatcher", "safe"),
            mcp_protect=("hive-dispatcher",),
            skill_include=("hive-dispatcher", "existing-core"),
            skill_protect=("hive-dispatcher",),
            core_skills=("hive-dispatcher",),
            make_default=True,
        )

        assert first.changed is True
        assert second.changed is False
        assert second.profile == "operator"
        assert config.read_bytes() == before
        assert config.stat().st_mtime_ns == before_mtime

        worker = registry.set_profile(
            "hive-worker",
            config_path=config,
            mcp_include=("safe",),
            mcp_deny=("hive-dispatcher",),
            skill_deny=("hive-dispatcher",),
            constrained=True,
        )
        data = yaml.safe_load(config.read_text(encoding="utf-8"))
        assert worker.changed is True
        assert data["version"] == 3
        assert data["profiles"]["default"] == "operator"
        assert data["profiles"]["entries"]["hive-worker"]["constrained"] is True
        assert data["registry"]["mcp_servers"][0]["command"] == "secret-command-sentinel"
        assert data["disclosure"]["core_skills"] == ["existing-core"]
        assert data["profiles"]["entries"]["operator"]["disclosure"][
            "core_skills"
        ] == ["existing-core", "hive-dispatcher"]
        assert data["unrelated"] == {"keep": True}

    def test_rejects_unpaired_or_unknown_protection_without_writing(self, tmp_path):
        config = tmp_path / "config.yaml"
        skills = tmp_path / "skills"
        _profile_global(config, skills)
        original = config.read_bytes()

        with pytest.raises(ValueError, match="paired"):
            registry.set_profile(
                "operator",
                config_path=config,
                mcp_protect=("hive-dispatcher",),
            )
        with pytest.raises(ValueError, match="enabled global MCP"):
            registry.set_profile(
                "operator", config_path=config, mcp_protect=("ghost",)
            )
        with pytest.raises(ValueError, match="trusted global skill"):
            registry.set_profile(
                "operator", config_path=config, skill_protect=("ghost",)
            )
        assert config.read_bytes() == original

    def test_rejects_disabled_mcp_and_conflicting_skill_bindings(self, tmp_path):
        config = tmp_path / "config.yaml"
        first_skills = tmp_path / "skills-a"
        second_skills = tmp_path / "skills-b"
        _write_skill(first_skills, "paired")
        _write_skill(second_skills, "paired")
        config.write_text(
            yaml.safe_dump(
                {
                    "version": 2,
                    "registry": {
                        "skills_dirs": [str(first_skills), str(second_skills)],
                        "mcp_servers": [
                            {
                                "id": "disabled",
                                "type": "stdio",
                                "command": "safe",
                                "enabled": False,
                            },
                            {"id": "paired", "type": "stdio", "command": "safe"},
                        ],
                    },
                }
            ),
            encoding="utf-8",
        )
        original = config.read_bytes()

        with pytest.raises(ValueError, match="enabled global MCP"):
            registry.set_profile(
                "operator", config_path=config, mcp_protect=("disabled",)
            )
        with pytest.raises(ValueError, match="conflicting trusted global skill"):
            registry.set_profile(
                "operator",
                config_path=config,
                mcp_protect=("paired",),
                skill_protect=("paired",),
            )
        assert config.read_bytes() == original

    def test_string_false_mcp_is_not_eligible_for_protection(self, tmp_path):
        config = tmp_path / "config.yaml"
        config.write_text(
            yaml.safe_dump(
                {
                    "version": 2,
                    "registry": {
                        "mcp_servers": [
                            {
                                "id": "paired",
                                "type": "stdio",
                                "command": "safe",
                                "enabled": "false",
                            }
                        ]
                    },
                }
            ),
            encoding="utf-8",
        )
        original = config.read_bytes()

        with pytest.raises(ValueError, match="enabled global MCP"):
            registry.set_profile(
                "operator", config_path=config, mcp_protect=("paired",)
            )
        assert config.read_bytes() == original

    @pytest.mark.parametrize(
        ("directory_name", "manifest_name", "malformed", "accepted"),
        [
            ("bundle-dir", "paired", False, True),
            ("paired", "different-name", False, False),
            ("paired", None, True, False),
            ("paired", None, False, True),
        ],
    )
    def test_protected_skill_uses_runtime_manifest_identity(
        self,
        tmp_path,
        directory_name,
        manifest_name,
        malformed,
        accepted,
    ):
        config = tmp_path / "config.yaml"
        skills = tmp_path / "skills"
        _write_manifest_skill(
            skills,
            directory_name,
            manifest_name=manifest_name,
            malformed=malformed,
        )
        config.write_text(
            yaml.safe_dump(
                {
                    "version": 2,
                    "registry": {"skills_dirs": [str(skills)]},
                }
            ),
            encoding="utf-8",
        )
        original = config.read_bytes()

        if accepted:
            registry.set_profile(
                "operator", config_path=config, skill_protect=("paired",)
            )
            assert config.read_bytes() != original
        else:
            with pytest.raises(ValueError, match="trusted global skill") as exc:
                registry.set_profile(
                    "operator", config_path=config, skill_protect=("paired",)
                )
            assert str(tmp_path) not in str(exc.value)
            assert config.read_bytes() == original

    @pytest.mark.parametrize(
        ("kwargs", "message"),
        [
            ({"mcp_include": ("safe",), "mcp_deny": ("safe",)}, "contradict"),
            ({"skill_deny": ("a", "a")}, "duplicate"),
            ({"mcp_protect": ("safe",), "mcp_deny": ("safe",)}, "contradict"),
        ],
    )
    def test_rejects_ambiguous_policy(self, tmp_path, kwargs, message):
        config = tmp_path / "config.yaml"
        skills = tmp_path / "skills"
        _profile_global(config, skills)
        original = config.read_bytes()

        with pytest.raises(ValueError, match=message):
            registry.set_profile("operator", config_path=config, **kwargs)
        assert config.read_bytes() == original

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"core_skills": ("paired",), "skill_deny": ("paired",)},
            {"core_skills": ("paired",), "skill_include": ("other",)},
            {"skill_protect": ("paired",), "skill_include": ("other",)},
            {"mcp_protect": ("paired",), "mcp_include": ("other",)},
        ],
    )
    def test_rejects_policy_that_would_make_core_or_protected_inactive(
        self, tmp_path, kwargs
    ):
        config = tmp_path / "config.yaml"
        skills = tmp_path / "skills"
        _write_skill(skills, "paired")
        config.write_text(
            yaml.safe_dump(
                {
                    "version": 2,
                    "registry": {
                        "skills_dirs": [str(skills)],
                        "mcp_servers": [
                            {"id": "paired", "type": "stdio", "command": "safe"},
                            {"id": "other", "type": "stdio", "command": "safe"},
                        ],
                    },
                }
            ),
            encoding="utf-8",
        )
        original = config.read_bytes()

        with pytest.raises(ValueError, match="contradictory"):
            registry.set_profile("operator", config_path=config, **kwargs)
        assert config.read_bytes() == original

    def test_malformed_duplicate_and_unsafe_targets_are_sanitized_and_untouched(
        self, tmp_path
    ):
        secret = "registry-secret-sentinel"
        malformed = tmp_path / "malformed.yaml"
        malformed.write_text(f"version: 2\nversion: 3\nsecret: {secret}\n")
        original = malformed.read_bytes()
        with pytest.raises(ValueError) as exc:
            registry.set_profile("operator", config_path=malformed)
        assert secret not in str(exc.value)
        assert malformed.read_bytes() == original

        real = tmp_path / "real.yaml"
        real.write_text("version: 2\n")
        link = tmp_path / "link.yaml"
        link.symlink_to(real)
        with pytest.raises(ValueError, match="regular file"):
            registry.set_profile("operator", config_path=link)
        assert real.read_text() == "version: 2\n"

    def test_target_symlink_swap_before_replace_fails_closed(
        self, tmp_path, monkeypatch
    ):
        config = tmp_path / "config.yaml"
        config.write_text("version: 2\n", encoding="utf-8")
        victim = tmp_path / "victim.yaml"
        victim.write_text("victim: untouched\n", encoding="utf-8")
        displaced = tmp_path / "displaced.yaml"
        original_atomic = registry._atomic_replace_yaml

        def swapped_atomic(path, data, original, **kwargs):
            path.rename(displaced)
            path.symlink_to(victim)
            return original_atomic(path, data, original, **kwargs)

        monkeypatch.setattr(registry, "_atomic_replace_yaml", swapped_atomic)

        with pytest.raises(RuntimeError, match="changed during mutation") as exc:
            registry.set_profile("operator", config_path=config)
        assert str(tmp_path) not in str(exc.value)
        assert victim.read_text(encoding="utf-8") == "victim: untouched\n"

    @pytest.mark.skipif(os.name != "posix", reason="dirfd atomic-write coverage")
    def test_parent_path_swap_cannot_redirect_temp_or_replace(
        self, tmp_path, monkeypatch
    ):
        parent = tmp_path / "validated-parent"
        parent.mkdir()
        config = parent / "config.yaml"
        config.write_text("version: 2\n", encoding="utf-8")
        relocated_parent = tmp_path / "relocated-parent"
        substitute_temp_bytes = b"substitute-temp-untouched"
        original_atomic = registry._atomic_replace_yaml

        monkeypatch.setattr(registry.secrets, "token_hex", lambda _size: "fixed")

        def swapped_parent_atomic(path, data, original, **kwargs):
            parent.rename(relocated_parent)
            parent.mkdir()
            (parent / "config.yaml").write_text(
                "substitute: untouched\n", encoding="utf-8"
            )
            (parent / ".config.yaml.fixed.tmp").write_bytes(
                substitute_temp_bytes
            )
            return original_atomic(path, data, original, **kwargs)

        monkeypatch.setattr(registry, "_atomic_replace_yaml", swapped_parent_atomic)

        result = registry.set_profile("operator", config_path=config)

        relocated = relocated_parent / "config.yaml"
        assert result.changed is True
        assert "operator" in yaml.safe_load(relocated.read_text())["profiles"][
            "entries"
        ]
        assert (parent / "config.yaml").read_text() == "substitute: untouched\n"
        assert (parent / ".config.yaml.fixed.tmp").read_bytes() == (
            substitute_temp_bytes
        )
        assert not list(relocated_parent.glob(".config.yaml.*.tmp"))

    def test_identical_noop_still_detects_target_replacement(
        self, tmp_path, monkeypatch
    ):
        config = tmp_path / "config.yaml"
        registry.set_profile("operator", config_path=config)
        displaced = tmp_path / "displaced.yaml"
        original_atomic = registry._atomic_replace_yaml

        def swapped_noop_atomic(path, data, original, **kwargs):
            path.rename(displaced)
            path.write_text("version: 2\nreplacement: untouched\n")
            return original_atomic(path, data, original, **kwargs)

        monkeypatch.setattr(registry, "_atomic_replace_yaml", swapped_noop_atomic)

        with pytest.raises(RuntimeError, match="changed during mutation"):
            registry.set_profile("operator", config_path=config)

        assert config.read_text() == "version: 2\nreplacement: untouched\n"
        assert not list(tmp_path.glob(".config.yaml.*.tmp"))

    @pytest.mark.parametrize("original_exists", [True, False])
    def test_verified_posix_temp_swap_is_detected_and_rolled_back(
        self, tmp_path, monkeypatch, original_exists
    ):
        config = tmp_path / "config.yaml"
        original = b"version: 2\nkeep: original\n"
        if original_exists:
            config.write_bytes(original)

        def swap_verified_temp(_path, name, temporary, directory_fd):
            assert temporary is None
            assert directory_fd is not None
            os.unlink(name, dir_fd=directory_fd)
            descriptor = os.open(
                name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=directory_fd,
            )
            try:
                os.write(descriptor, b"version: 3\nsubstituted: true\n")
                os.fsync(descriptor)
            finally:
                os.close(descriptor)

        monkeypatch.setattr(
            registry, "_atomic_replace_probe", swap_verified_temp
        )

        with pytest.raises(RuntimeError, match="write failed"):
            registry.set_profile("operator", config_path=config)

        if original_exists:
            assert config.read_bytes() == original
        else:
            assert not config.exists()
        assert not list(tmp_path.glob(".config.yaml.*.tmp"))
        assert not list(tmp_path.glob(".config.yaml.*.bak"))

    def test_verified_windows_fallback_temp_swap_is_rolled_back(
        self, tmp_path, monkeypatch
    ):
        class FakeMsvcrt:
            LK_LOCK = 1
            LK_UNLCK = 0

            @staticmethod
            def locking(*_args):
                return None

        config = tmp_path / "config.yaml"
        original = b"version: 2\nkeep: original\n"
        config.write_bytes(original)

        def swap_verified_temp(_path, _name, temporary, directory_fd):
            assert directory_fd is None
            assert temporary is not None
            attacker = tmp_path / "attacker-stage"
            attacker.write_bytes(b"version: 3\nsubstituted: true\n")
            temporary.unlink()
            os.replace(attacker, temporary)

        monkeypatch.setattr(registry, "_fcntl", None)
        monkeypatch.setattr(registry, "_msvcrt", FakeMsvcrt)
        monkeypatch.setattr(
            registry, "_atomic_replace_probe", swap_verified_temp
        )

        with pytest.raises(RuntimeError, match="write failed"):
            registry.set_profile("operator", config_path=config)

        assert config.read_bytes() == original
        assert not list(tmp_path.glob(".config.yaml.*.tmp"))
        assert not list(tmp_path.glob(".config.yaml.*.bak"))

    def test_transient_rollback_replace_failure_recovers_original_and_fails_closed(
        self, tmp_path, monkeypatch
    ):
        config = tmp_path / "config.yaml"
        original = b"version: 2\nkeep: original\n"
        config.write_bytes(original)

        def swap_verified_temp(_path, name, _temporary, directory_fd):
            assert directory_fd is not None
            os.unlink(name, dir_fd=directory_fd)
            descriptor = os.open(
                name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=directory_fd,
            )
            os.write(descriptor, b"substituted: true\n")
            os.close(descriptor)

        real_replace = os.replace
        calls = 0

        def fail_first_rollback(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("rollback-race-sentinel")
            return real_replace(*args, **kwargs)

        monkeypatch.setattr(
            registry, "_atomic_replace_probe", swap_verified_temp
        )
        monkeypatch.setattr(registry.os, "replace", fail_first_rollback)

        with pytest.raises(RuntimeError, match="write failed") as exc:
            registry.set_profile("operator", config_path=config)

        assert "rollback-race-sentinel" not in str(exc.value)
        assert calls >= 3
        assert config.read_bytes() == original
        assert not list(tmp_path.glob(".config.yaml.*.tmp"))
        assert not list(tmp_path.glob(".config.yaml.*.bak"))

    def test_windows_lockfile_symlink_is_rejected_before_locking(
        self, tmp_path, monkeypatch
    ):
        class FakeMsvcrt:
            LK_LOCK = 1
            LK_UNLCK = 0

            @staticmethod
            def locking(*_args):
                raise AssertionError("symlink reached locking call")

        target = tmp_path / "config.yaml"
        victim = tmp_path / "victim"
        victim.write_text("safe", encoding="utf-8")
        (tmp_path / ".config.yaml.lock").symlink_to(victim)
        monkeypatch.setattr(registry, "_fcntl", None)
        monkeypatch.setattr(registry, "_msvcrt", FakeMsvcrt)

        with pytest.raises(RuntimeError, match="locking is unavailable"):
            registry.set_profile("operator", config_path=target)
        assert victim.read_text(encoding="utf-8") == "safe"

    def test_windows_lockfile_hard_link_is_rejected_before_mutation(
        self, tmp_path, monkeypatch
    ):
        class FakeMsvcrt:
            LK_LOCK = 1
            LK_UNLCK = 0

            @staticmethod
            def locking(*_args):
                raise AssertionError("hard link reached locking call")

        target = tmp_path / "config.yaml"
        victim = tmp_path / "victim"
        victim.write_bytes(b"victim-unchanged")
        os.link(victim, tmp_path / ".config.yaml.lock")
        monkeypatch.setattr(registry, "_fcntl", None)
        monkeypatch.setattr(registry, "_msvcrt", FakeMsvcrt)

        with pytest.raises(RuntimeError, match="locking is unavailable"):
            registry.set_profile("operator", config_path=target)
        assert victim.read_bytes() == b"victim-unchanged"

    def test_failed_replace_preserves_original_and_cleans_temp(self, tmp_path):
        config = tmp_path / "config.yaml"
        skills = tmp_path / "skills"
        _profile_global(config, skills)
        original = config.read_bytes()

        with (
            patch("gearcore_hub.registry.os.replace", side_effect=OSError("secret")),
            pytest.raises(RuntimeError, match="write failed") as exc,
        ):
            registry.set_profile("operator", config_path=config)

        assert "secret" not in str(exc.value)
        assert config.read_bytes() == original
        assert not list(tmp_path.glob(".config.yaml.*.tmp"))

    def test_preserves_existing_permissions(self, tmp_path):
        config = tmp_path / "config.yaml"
        skills = tmp_path / "skills"
        _profile_global(config, skills)
        config.chmod(0o640)

        registry.set_profile("operator", config_path=config)

        assert os.stat(config).st_mode & 0o777 == 0o640

    def test_simultaneous_writers_do_not_lose_profiles(self, tmp_path):
        config = tmp_path / "config.yaml"
        config.write_text("version: 2\n", encoding="utf-8")

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(
                executor.map(
                    lambda name: registry.set_profile(name, config_path=config),
                    ("operator", "hive-worker"),
                )
            )

        data = yaml.safe_load(config.read_text(encoding="utf-8"))
        assert {result.profile for result in results} == {
            "operator",
            "hive-worker",
        }
        assert set(data["profiles"]["entries"]) == {"operator", "hive-worker"}
        assert not list(tmp_path.glob(".config.yaml.*.tmp"))

    def test_interleaved_profile_and_mcp_mutations_do_not_lose_updates(
        self, tmp_path, monkeypatch
    ):
        config = tmp_path / "config.yaml"
        config.write_text("version: 2\n", encoding="utf-8")
        monkeypatch.setattr(registry, "GLOBAL_CONFIG_PATH", config)
        entered = threading.Event()
        release = threading.Event()
        original_atomic = registry._atomic_replace_yaml
        calls = 0
        calls_lock = threading.Lock()

        def paused_atomic(*args, **kwargs):
            nonlocal calls
            with calls_lock:
                calls += 1
                should_pause = calls == 1
            if should_pause:
                entered.set()
                assert release.wait(timeout=5)
            return original_atomic(*args, **kwargs)

        monkeypatch.setattr(registry, "_atomic_replace_yaml", paused_atomic)
        with ThreadPoolExecutor(max_workers=2) as executor:
            profile_future = executor.submit(
                registry.set_profile, "operator", config_path=config
            )
            assert entered.wait(timeout=5)
            add_future = executor.submit(
                registry.add_mcp, id="added", type="stdio", command="safe"
            )
            assert not add_future.done()
            release.set()
            profile_future.result(timeout=5)
            add_future.result(timeout=5)

        data = yaml.safe_load(config.read_text(encoding="utf-8"))
        assert "operator" in data["profiles"]["entries"]
        assert [server["id"] for server in data["registry"]["mcp_servers"]] == [
            "added"
        ]

    def test_interleaved_profile_and_remove_do_not_restore_removed_server(
        self, tmp_path, monkeypatch
    ):
        config = tmp_path / "config.yaml"
        _write_global(
            config,
            [
                {"id": "remove-me", "type": "stdio", "command": "safe"},
                {"id": "keep", "type": "stdio", "command": "safe"},
            ],
        )
        monkeypatch.setattr(registry, "GLOBAL_CONFIG_PATH", config)
        entered = threading.Event()
        release = threading.Event()
        original_atomic = registry._atomic_replace_yaml
        first = True

        def paused_atomic(*args, **kwargs):
            nonlocal first
            if first:
                first = False
                entered.set()
                assert release.wait(timeout=5)
            return original_atomic(*args, **kwargs)

        monkeypatch.setattr(registry, "_atomic_replace_yaml", paused_atomic)
        with ThreadPoolExecutor(max_workers=2) as executor:
            profile_future = executor.submit(
                registry.set_profile, "operator", config_path=config
            )
            assert entered.wait(timeout=5)
            remove_future = executor.submit(registry.remove_mcp, "remove-me")
            assert not remove_future.done()
            release.set()
            profile_future.result(timeout=5)
            remove_future.result(timeout=5)

        data = yaml.safe_load(config.read_text(encoding="utf-8"))
        assert "operator" in data["profiles"]["entries"]
        assert [server["id"] for server in data["registry"]["mcp_servers"]] == [
            "keep"
        ]

    @pytest.mark.skipif(os.name != "posix", reason="POSIX process-lock coverage")
    def test_process_writers_share_the_same_lock(self, tmp_path):
        config = tmp_path / "config.yaml"
        config.write_text("version: 2\n", encoding="utf-8")
        context = multiprocessing.get_context("fork")
        processes = [
            context.Process(
                target=_process_set_profile,
                args=(str(config), profile),
            )
            for profile in ("operator", "hive-worker")
        ]

        for process in processes:
            process.start()
        for process in processes:
            process.join(timeout=10)
            assert process.exitcode == 0

        data = yaml.safe_load(config.read_text(encoding="utf-8"))
        assert set(data["profiles"]["entries"]) == {"operator", "hive-worker"}

    @pytest.mark.skipif(os.name != "posix", reason="POSIX process-lock coverage")
    def test_process_profile_and_add_mcp_do_not_lose_updates(self, tmp_path):
        config = tmp_path / "config.yaml"
        config.write_text("version: 2\n", encoding="utf-8")
        context = multiprocessing.get_context("fork")
        processes = [
            context.Process(
                target=_process_set_profile,
                args=(str(config), "operator"),
            ),
            context.Process(target=_process_add_mcp, args=(str(config),)),
        ]

        for process in processes:
            process.start()
        for process in processes:
            process.join(timeout=10)
            assert process.exitcode == 0

        data = yaml.safe_load(config.read_text(encoding="utf-8"))
        assert "operator" in data["profiles"]["entries"]
        assert [server["id"] for server in data["registry"]["mcp_servers"]] == [
            "process-added"
        ]

    @pytest.mark.parametrize(
        "unsafe_id",
        [
            "bad\nsource: forged",
            "bad\rforged",
            "bad\tforged",
            "bad\x1b[31m",
            "bad\x00forged",
            "bad\u200eforged",
        ],
    )
    def test_mutation_rejects_non_line_safe_capability_ids(
        self, tmp_path, unsafe_id
    ):
        config = tmp_path / "config.yaml"
        config.write_text("version: 2\n", encoding="utf-8")
        original = config.read_bytes()

        with pytest.raises(ValueError, match="invalid capability ID"):
            registry.set_profile(
                "operator", config_path=config, mcp_include=(unsafe_id,)
            )
        assert config.read_bytes() == original

    def test_v2_compatible_unicode_space_colon_and_delimiter_ids_round_trip(
        self, tmp_path, monkeypatch
    ):
        config = tmp_path / "config.yaml"
        config.write_text("version: 2\n", encoding="utf-8")
        monkeypatch.setattr(registry, "GLOBAL_CONFIG_PATH", config)
        legacy_id = "legacy: 工具, id"

        registry.add_mcp(legacy_id, "stdio", command="safe")
        registry.set_profile(
            "Operator: 本地 profile",
            config_path=config,
            mcp_include=(legacy_id,),
            make_default=True,
        )

        data = yaml.safe_load(config.read_text(encoding="utf-8"))
        assert data["registry"]["mcp_servers"][0]["id"] == legacy_id
        assert data["profiles"]["default"] == "Operator: 本地 profile"
        registry.remove_mcp(legacy_id)
        assert yaml.safe_load(config.read_text())["registry"]["mcp_servers"] == []

    def test_skill_filesystem_names_allow_v2_text_but_reject_traversal(
        self, tmp_path
    ):
        project = tmp_path / "project"
        source = tmp_path / "legacy skill: 工具"
        _write_skill(tmp_path, source.name)

        installed = registry.add_skill(
            source, scope="project", project_root=project
        )
        assert installed.name == source.name
        registry.remove_skill(source.name, scope="project", project_root=project)
        assert not installed.exists()

        with pytest.raises(ValueError, match="invalid skill ID"):
            registry.remove_skill(
                "../outside", scope="project", project_root=project
            )

    def test_missing_platform_lock_backend_fails_only_mutation(
        self, tmp_path, monkeypatch
    ):
        config = tmp_path / "config.yaml"
        config.write_text("version: 2\n", encoding="utf-8")
        source = tmp_path / "safe-skill"
        _write_skill(tmp_path, "safe-skill")
        monkeypatch.setattr(registry, "_fcntl", None)
        monkeypatch.setattr(registry, "_msvcrt", None)

        with pytest.raises(RuntimeError, match="locking is unavailable"):
            registry.set_profile("operator", config_path=config)
        installed = registry.add_skill(
            source, scope="project", project_root=tmp_path / "project"
        )
        assert (installed / "SKILL.md").is_file()

    def test_registry_module_imports_without_platform_lock_modules(self):
        script = """
import builtins
import subprocess
original_import = builtins.__import__
def guarded_import(name, *args, **kwargs):
    if name in {'fcntl', 'msvcrt'}:
        raise ImportError(name)
    return original_import(name, *args, **kwargs)
builtins.__import__ = guarded_import
import gearcore_hub.registry as registry
assert registry._fcntl is None
assert registry._msvcrt is None
"""

        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            check=False,
        )

        assert result.returncode == 0, result.stderr
