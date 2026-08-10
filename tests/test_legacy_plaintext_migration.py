from __future__ import annotations

import os
import stat
import sys
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from contextlib import suppress
from pathlib import Path
from threading import Event

import pytest
import yaml

from gearcore_hub import legacy_auth_migration as migration
from gearcore_hub import registry
from gearcore_hub.config import load_global_config
from gearcore_hub.main import main as cli_main

LEGACY_SECRET = "gc_legacy_plaintext_token_value_152_0123456789abcdef"


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _run_migration(
    monkeypatch,
    config_path: Path,
    credential_root: Path,
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "gearcore",
            "--config",
            str(config_path),
            "migrate-legacy-auth",
            "--credential-root",
            str(credential_root),
        ],
    )

    cli_main()


def _captured_text(capsys) -> str:
    captured = capsys.readouterr()
    return captured.out + captured.err


def test_cli_migrates_legacy_stdio_secret_before_strict_load(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    config_path = tmp_path / "config.yaml"
    credential_root = tmp_path / "credentials"
    config_path.write_text(
        yaml.safe_dump(
            {
                "version": 2,
                "registry": {
                    "mcp_servers": [
                        {
                            "id": "dispatcher",
                            "type": "stdio",
                            "command": "dispatcher-cli",
                            "args": ["serve", "--safe"],
                            "env": {
                                "DISPATCHER_TOKEN": LEGACY_SECRET,
                                "KEEP_ME": "nonsecret-runtime-mode",
                            },
                            "enabled": True,
                            "metadata": {"owner": "ops", "priority": 7},
                        }
                    ]
                },
                "skills_dirs": ["/opt/gearcore/skills"],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    _run_migration(monkeypatch, config_path, credential_root)

    captured = capsys.readouterr()
    rendered_output = captured.out + captured.err
    assert LEGACY_SECRET not in rendered_output
    assert "dispatcher" in captured.out

    migrated_text = config_path.read_text(encoding="utf-8")
    assert LEGACY_SECRET not in migrated_text
    migrated = yaml.safe_load(migrated_text)
    server = migrated["registry"]["mcp_servers"][0]
    assert server == {
        "id": "dispatcher",
        "type": "stdio",
        "command": "dispatcher-cli",
        "args": ["serve", "--safe"],
        "env": {"KEEP_ME": "nonsecret-runtime-mode"},
        "enabled": True,
        "metadata": {"owner": "ops", "priority": 7},
        "auth": {
            "credential_ref": "dispatcher",
            "stdio_environment": "DISPATCHER_TOKEN",
        },
    }
    assert migrated["skills_dirs"] == ["/opt/gearcore/skills"]

    credential_file = credential_root / "dispatcher"
    assert credential_file.read_text(encoding="utf-8") == f"{LEGACY_SECRET}\n"
    assert _mode(credential_root) == 0o700
    assert _mode(credential_file) == 0o600
    if hasattr(os, "getuid"):
        assert credential_file.stat().st_uid == os.getuid()

    backup = config_path.with_suffix(config_path.suffix + ".legacy-auth-backup")
    assert backup.is_file()
    assert _mode(backup) == 0o600
    assert LEGACY_SECRET in backup.read_text(encoding="utf-8")
    loaded = load_global_config(config_path)
    assert loaded.mcp_servers[0].auth is not None
    assert loaded.mcp_servers[0].auth.credential_id() == "dispatcher"


def test_legacy_auth_migration_is_idempotent_and_keeps_existing_backup(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    config_path = tmp_path / "config.yaml"
    credential_root = tmp_path / "credentials"
    config_path.write_text(
        yaml.safe_dump(
            {
                "version": 2,
                "registry": {
                    "mcp_servers": [
                        {
                            "id": "dispatcher",
                            "type": "stdio",
                            "command": "dispatcher-cli",
                            "env": {"DISPATCHER_TOKEN": LEGACY_SECRET},
                        }
                    ]
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    _run_migration(monkeypatch, config_path, credential_root)
    first_config = config_path.read_text(encoding="utf-8")
    first_backup = config_path.with_suffix(
        config_path.suffix + ".legacy-auth-backup"
    ).read_text(encoding="utf-8")
    capsys.readouterr()

    _run_migration(monkeypatch, config_path, credential_root)

    assert config_path.read_text(encoding="utf-8") == first_config
    assert (
        config_path.with_suffix(config_path.suffix + ".legacy-auth-backup").read_text(
            encoding="utf-8"
        )
        == first_backup
    )
    assert "already migrated" in capsys.readouterr().out


def test_legacy_auth_migration_uses_unique_backup_without_overwriting_existing(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    config_path = tmp_path / "config.yaml"
    credential_root = tmp_path / "credentials"
    existing_backup = config_path.with_suffix(config_path.suffix + ".legacy-auth-backup")
    existing_backup.write_text("preexisting backup\n", encoding="utf-8")
    config_path.write_text(
        yaml.safe_dump(
            {
                "version": 2,
                "registry": {
                    "mcp_servers": [
                        {
                            "id": "dispatcher",
                            "type": "stdio",
                            "command": "dispatcher-cli",
                            "env": {"DISPATCHER_TOKEN": LEGACY_SECRET},
                        }
                    ]
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    _run_migration(monkeypatch, config_path, credential_root)

    assert "dispatcher" in capsys.readouterr().out
    assert existing_backup.read_text(encoding="utf-8") == "preexisting backup\n"
    new_backups = sorted(tmp_path.glob("config.yaml.legacy-auth-backup.*"))
    assert len(new_backups) == 1
    assert LEGACY_SECRET in new_backups[0].read_text(encoding="utf-8")
    assert load_global_config(config_path).mcp_servers[0].auth is not None


@pytest.mark.parametrize(
    "server_update",
    [
        {"env": {"API_TOKEN": LEGACY_SECRET, "OTHER_SECRET": "second-secret"}},
        {"env": {"API_TOKEN": LEGACY_SECRET}, "auth": {"credential_ref": "existing"}},
        {"args": ["--token", LEGACY_SECRET]},
        {"url": f"https://user:{LEGACY_SECRET}@example.invalid/mcp"},
        {"headers": {"Authorization": f"Bearer {LEGACY_SECRET}"}},
    ],
)
def test_legacy_auth_migration_fails_closed_on_ambiguous_plaintext_routes(
    tmp_path: Path,
    monkeypatch,
    capsys,
    server_update: dict[str, object],
) -> None:
    config_path = tmp_path / "config.yaml"
    credential_root = tmp_path / "credentials"
    server = {
        "id": "dispatcher",
        "type": "stdio",
        "command": "dispatcher-cli",
        **server_update,
    }
    original = yaml.safe_dump(
        {"version": 2, "registry": {"mcp_servers": [server]}},
        sort_keys=False,
    )
    config_path.write_text(original, encoding="utf-8")

    with pytest.raises(SystemExit) as exc_info:
        _run_migration(monkeypatch, config_path, credential_root)

    assert exc_info.value.code == 1
    assert LEGACY_SECRET not in _captured_text(capsys)
    assert config_path.read_text(encoding="utf-8") == original
    assert not credential_root.exists()
    assert not config_path.with_suffix(config_path.suffix + ".legacy-auth-backup").exists()


def test_legacy_auth_migration_rolls_back_when_config_publish_fails(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    config_path = tmp_path / "config.yaml"
    credential_root = tmp_path / "credentials"
    original = yaml.safe_dump(
        {
            "version": 2,
            "registry": {
                "mcp_servers": [
                    {
                        "id": "dispatcher",
                        "type": "stdio",
                        "command": "dispatcher-cli",
                        "env": {"DISPATCHER_TOKEN": LEGACY_SECRET},
                    }
                ]
            },
        },
        sort_keys=False,
    )
    config_path.write_text(original, encoding="utf-8")

    real_replace = os.replace

    def failing_config_replace(
        src: str | Path,
        dst: str | Path,
        *args: object,
        **kwargs: object,
    ) -> None:
        if Path(dst) == config_path or (
            Path(dst) == Path(config_path.name) and kwargs.get("dst_dir_fd") is not None
        ):
            raise OSError("simulated replace failure")
        real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "replace", failing_config_replace)

    with pytest.raises(SystemExit) as exc_info:
        _run_migration(monkeypatch, config_path, credential_root)

    assert exc_info.value.code == 1
    assert LEGACY_SECRET not in _captured_text(capsys)
    assert config_path.read_text(encoding="utf-8") == original
    assert (credential_root / "dispatcher").read_text(encoding="utf-8") == (
        f"{LEGACY_SECRET}\n"
    )
    assert (
        config_path.with_suffix(config_path.suffix + ".legacy-auth-backup").read_bytes()
        == original.encode()
    )

    monkeypatch.setattr(os, "replace", real_replace)
    _run_migration(monkeypatch, config_path, credential_root)

    assert LEGACY_SECRET not in config_path.read_text(encoding="utf-8")
    assert load_global_config(config_path).mcp_servers[0].auth is not None


def test_legacy_auth_migration_keeps_credentials_when_config_publish_committed(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    config_path = tmp_path / "config.yaml"
    credential_root = tmp_path / "credentials"
    original = yaml.safe_dump(
        {
            "version": 2,
            "registry": {
                "mcp_servers": [
                    {
                        "id": "dispatcher",
                        "type": "stdio",
                        "command": "dispatcher-cli",
                        "env": {"DISPATCHER_TOKEN": LEGACY_SECRET},
                    }
                ]
            },
        },
        sort_keys=False,
    )
    config_path.write_text(original, encoding="utf-8")

    real_replace = os.replace

    def committed_config_replace(
        src: str | Path,
        dst: str | Path,
        *args: object,
        **kwargs: object,
    ) -> None:
        real_replace(src, dst, *args, **kwargs)
        if Path(dst) == config_path or (
            Path(dst) == Path(config_path.name) and kwargs.get("dst_dir_fd") is not None
        ):
            raise OSError("simulated post-commit durability failure")

    monkeypatch.setattr(os, "replace", committed_config_replace)

    with pytest.raises(SystemExit) as exc_info:
        _run_migration(monkeypatch, config_path, credential_root)

    assert exc_info.value.code == 1
    assert LEGACY_SECRET not in _captured_text(capsys)
    assert LEGACY_SECRET not in config_path.read_text(encoding="utf-8")
    assert (credential_root / "dispatcher").read_text(encoding="utf-8") == (
        f"{LEGACY_SECRET}\n"
    )
    backup = config_path.with_suffix(config_path.suffix + ".legacy-auth-backup")
    assert backup.is_file()
    assert LEGACY_SECRET in backup.read_text(encoding="utf-8")
    assert load_global_config(config_path).mcp_servers[0].auth is not None


def test_legacy_auth_migration_does_not_chmod_substituted_symlink_after_publish(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config_path = tmp_path / "config.yaml"
    credential_root = tmp_path / "credentials"
    victim = tmp_path / "victim"
    victim.write_text("do not chmod me\n", encoding="utf-8")
    victim.chmod(0o644)
    config_path.write_text(
        yaml.safe_dump(
            {
                "version": 2,
                "registry": {
                    "mcp_servers": [
                        {
                            "id": "dispatcher",
                            "type": "stdio",
                            "command": "dispatcher-cli",
                            "env": {"DISPATCHER_TOKEN": LEGACY_SECRET},
                        }
                    ]
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    real_chmod = os.chmod
    swapped = False

    def substituting_chmod(
        path: str | Path,
        mode: int,
        *args: object,
        **kwargs: object,
    ) -> None:
        nonlocal swapped
        if Path(path) == config_path and mode == 0o600 and not swapped:
            swapped = True
            config_path.unlink()
            config_path.symlink_to(victim)
        real_chmod(path, mode, *args, **kwargs)

    monkeypatch.setattr(os, "chmod", substituting_chmod)

    _run_migration(monkeypatch, config_path, credential_root)

    assert not config_path.is_symlink()
    assert _mode(config_path) == 0o600
    assert _mode(victim) == 0o644
    assert load_global_config(config_path).mcp_servers[0].auth is not None


def test_legacy_auth_migration_recovers_verified_crash_residue(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    config_path = tmp_path / "config.yaml"
    credential_root = tmp_path / "credentials"
    credential_path = credential_root / "dispatcher"
    backup_path = config_path.with_suffix(config_path.suffix + ".legacy-auth-backup")
    original = yaml.safe_dump(
        {
            "version": 2,
            "registry": {
                "mcp_servers": [
                    {
                        "id": "dispatcher",
                        "type": "stdio",
                        "command": "dispatcher-cli",
                        "env": {"DISPATCHER_TOKEN": LEGACY_SECRET},
                    }
                ]
            },
        },
        sort_keys=False,
    )
    config_path.write_text(original, encoding="utf-8")
    credential_root.mkdir(mode=0o700)
    backup_path.write_bytes(config_path.read_bytes())
    backup_path.chmod(0o600)
    credential_path.write_text(f"{LEGACY_SECRET}\n", encoding="utf-8")
    credential_path.chmod(0o600)

    _run_migration(monkeypatch, config_path, credential_root)

    assert LEGACY_SECRET not in _captured_text(capsys)
    assert LEGACY_SECRET not in config_path.read_text(encoding="utf-8")
    assert credential_path.read_text(encoding="utf-8") == f"{LEGACY_SECRET}\n"
    assert backup_path.read_bytes() == original.encode()
    assert load_global_config(config_path).mcp_servers[0].auth is not None


def test_legacy_auth_migration_cleanup_preserves_replaced_credential(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    config_path = tmp_path / "config.yaml"
    credential_root = tmp_path / "credentials"
    credential_path = credential_root / "dispatcher"
    original = yaml.safe_dump(
        {
            "version": 2,
            "registry": {
                "mcp_servers": [
                    {
                        "id": "dispatcher",
                        "type": "stdio",
                        "command": "dispatcher-cli",
                        "env": {"DISPATCHER_TOKEN": LEGACY_SECRET},
                    }
                ]
            },
        },
        sort_keys=False,
    )
    config_path.write_text(original, encoding="utf-8")

    def replace_credential_then_fail(*_args: object, **_kwargs: object) -> bool:
        credential_path.unlink()
        credential_path.write_text("same-uid replacement\n", encoding="utf-8")
        credential_path.chmod(0o600)
        raise RuntimeError("simulated publish failure")

    monkeypatch.setattr(
        migration.registry_mutation,
        "_atomic_replace_yaml",
        replace_credential_then_fail,
    )

    with pytest.raises(SystemExit) as exc_info:
        _run_migration(monkeypatch, config_path, credential_root)

    assert exc_info.value.code == 1
    assert LEGACY_SECRET not in _captured_text(capsys)
    assert config_path.read_text(encoding="utf-8") == original
    assert credential_path.read_text(encoding="utf-8") == "same-uid replacement\n"


def test_legacy_auth_migration_cleanup_never_path_unlinks_after_identity_check(
    tmp_path: Path,
    monkeypatch,
) -> None:
    target = tmp_path / "credential"
    target.write_text("created-by-migration\n", encoding="utf-8")
    target.chmod(0o600)
    identity = migration._path_identity(target)
    assert identity is not None
    real_unlink = Path.unlink

    def substituting_unlink(
        self: Path,
        *args: object,
        **kwargs: object,
    ) -> None:
        if self == target:
            real_unlink(self, *args, **kwargs)
            self.write_text("same-uid replacement\n", encoding="utf-8")
            self.chmod(0o600)
        real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", substituting_unlink)

    migration._unlink_if_identity(target, identity)

    assert target.exists()


def test_legacy_auth_migration_rejects_recovery_backup_swapped_between_stat_and_read(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    config_path = tmp_path / "config.yaml"
    credential_root = tmp_path / "credentials"
    credential_path = credential_root / "dispatcher"
    backup_path = config_path.with_suffix(config_path.suffix + ".legacy-auth-backup")
    original = yaml.safe_dump(
        {
            "version": 2,
            "registry": {
                "mcp_servers": [
                    {
                        "id": "dispatcher",
                        "type": "stdio",
                        "command": "dispatcher-cli",
                        "env": {"DISPATCHER_TOKEN": LEGACY_SECRET},
                    }
                ]
            },
        },
        sort_keys=False,
    )
    config_path.write_text(original, encoding="utf-8")
    credential_root.mkdir(mode=0o700)
    backup_path.write_text("wrong backup\n", encoding="utf-8")
    backup_path.chmod(0o600)
    credential_path.write_text(f"{LEGACY_SECRET}\n", encoding="utf-8")
    credential_path.chmod(0o600)
    real_read_bytes = Path.read_bytes
    swapped = False

    def swap_backup_to_expected() -> None:
        nonlocal swapped
        if not swapped:
            swapped = True
            backup_path.write_bytes(original.encode())
            backup_path.chmod(0o600)

    def swapping_read_bytes(self: Path) -> bytes:
        if self == backup_path:
            swap_backup_to_expected()
        return real_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", swapping_read_bytes)
    monkeypatch.setattr(
        migration,
        "_before_recovery_content_read",
        lambda path: swap_backup_to_expected() if path == backup_path else None,
        raising=False,
    )

    with pytest.raises(SystemExit) as exc_info:
        _run_migration(monkeypatch, config_path, credential_root)

    assert exc_info.value.code == 1
    assert LEGACY_SECRET not in _captured_text(capsys)
    assert config_path.read_text(encoding="utf-8") == original


def test_legacy_auth_migration_rejects_recovery_backup_path_replaced_after_open(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    config_path = tmp_path / "config.yaml"
    credential_root = tmp_path / "credentials"
    credential_path = credential_root / "dispatcher"
    backup_path = config_path.with_suffix(config_path.suffix + ".legacy-auth-backup")
    orphaned_backup = tmp_path / "orphaned-backup"
    original = yaml.safe_dump(
        {
            "version": 2,
            "registry": {
                "mcp_servers": [
                    {
                        "id": "dispatcher",
                        "type": "stdio",
                        "command": "dispatcher-cli",
                        "env": {"DISPATCHER_TOKEN": LEGACY_SECRET},
                    }
                ]
            },
        },
        sort_keys=False,
    )
    config_path.write_text(original, encoding="utf-8")
    credential_root.mkdir(mode=0o700)
    backup_path.write_bytes(config_path.read_bytes())
    backup_path.chmod(0o600)
    credential_path.write_text(f"{LEGACY_SECRET}\n", encoding="utf-8")
    credential_path.chmod(0o600)
    replaced = False
    monkeypatch.setattr(
        migration,
        "_metadata_identity",
        lambda _metadata: (1, 1, 1, 1, 1),
    )

    def replace_backup_path(path: Path) -> None:
        nonlocal replaced
        if path == backup_path and not replaced:
            replaced = True
            backup_path.rename(orphaned_backup)
            backup_path.write_text("replacement residue\n", encoding="utf-8")
            backup_path.chmod(0o600)

    monkeypatch.setattr(
        migration,
        "_before_recovery_content_read",
        replace_backup_path,
    )

    with pytest.raises(SystemExit) as exc_info:
        _run_migration(monkeypatch, config_path, credential_root)

    assert exc_info.value.code == 1
    assert LEGACY_SECRET not in _captured_text(capsys)
    assert config_path.read_text(encoding="utf-8") == original
    assert backup_path.read_text(encoding="utf-8") == "replacement residue\n"
    assert orphaned_backup.read_bytes() == original.encode()


def test_legacy_auth_migration_rechecks_recovery_residue_path_before_commit(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    config_path = tmp_path / "config.yaml"
    credential_root = tmp_path / "credentials"
    credential_path = credential_root / "dispatcher"
    backup_path = config_path.with_suffix(config_path.suffix + ".legacy-auth-backup")
    orphaned_backup = tmp_path / "orphaned-backup"
    original = yaml.safe_dump(
        {
            "version": 2,
            "registry": {
                "mcp_servers": [
                    {
                        "id": "dispatcher",
                        "type": "stdio",
                        "command": "dispatcher-cli",
                        "env": {"DISPATCHER_TOKEN": LEGACY_SECRET},
                    }
                ]
            },
        },
        sort_keys=False,
    )
    config_path.write_text(original, encoding="utf-8")
    credential_root.mkdir(mode=0o700)
    backup_path.write_bytes(config_path.read_bytes())
    backup_path.chmod(0o600)
    credential_path.write_text(f"{LEGACY_SECRET}\n", encoding="utf-8")
    credential_path.chmod(0o600)
    replaced = False

    def replace_backup_path() -> None:
        nonlocal replaced
        if not replaced:
            replaced = True
            backup_path.rename(orphaned_backup)
            backup_path.write_text("replacement residue\n", encoding="utf-8")
            backup_path.chmod(0o600)

    monkeypatch.setattr(
        migration,
        "_before_recovery_commit_verification",
        replace_backup_path,
        raising=False,
    )

    with pytest.raises(SystemExit) as exc_info:
        _run_migration(monkeypatch, config_path, credential_root)

    assert exc_info.value.code == 1
    assert LEGACY_SECRET not in _captured_text(capsys)
    assert config_path.read_text(encoding="utf-8") == original
    assert backup_path.read_text(encoding="utf-8") == "replacement residue\n"
    assert orphaned_backup.read_bytes() == original.encode()


def test_legacy_auth_migration_recovery_open_uses_nonblocking_no_follow(
    tmp_path: Path,
    monkeypatch,
) -> None:
    backup_path = tmp_path / "config.yaml.legacy-auth-backup"
    backup_path.write_text("safe backup\n", encoding="utf-8")
    backup_path.chmod(0o600)
    real_open = os.open
    opened_flags: list[int] = []

    def recording_open(
        path: str | bytes | Path,
        flags: int,
        *args: object,
        **kwargs: object,
    ) -> int:
        if Path(path) == backup_path:
            opened_flags.append(flags)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", recording_open)

    assert migration._owner_only_file_has_bytes(backup_path, b"safe backup\n")
    assert opened_flags
    assert opened_flags[0] & os.O_NOFOLLOW
    assert opened_flags[0] & os.O_NONBLOCK


def test_legacy_auth_migration_refuses_concurrent_credential_publication(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    config_path = tmp_path / "config.yaml"
    credential_root = tmp_path / "credentials"
    credential_path = credential_root / "dispatcher"
    original = yaml.safe_dump(
        {
            "version": 2,
            "registry": {
                "mcp_servers": [
                    {
                        "id": "dispatcher",
                        "type": "stdio",
                        "command": "dispatcher-cli",
                        "env": {"DISPATCHER_TOKEN": LEGACY_SECRET},
                    }
                ]
            },
        },
        sort_keys=False,
    )
    config_path.write_text(original, encoding="utf-8")

    def publish_competing_file(path: Path) -> None:
        if path == credential_path and not credential_path.exists():
            credential_path.write_text("concurrent-owner\n", encoding="utf-8")
            credential_path.chmod(0o600)

    monkeypatch.setattr(
        migration,
        "_before_no_replace_publication",
        publish_competing_file,
        raising=False,
    )

    with pytest.raises(SystemExit) as exc_info:
        _run_migration(monkeypatch, config_path, credential_root)

    assert exc_info.value.code == 1
    assert LEGACY_SECRET not in _captured_text(capsys)
    assert config_path.read_text(encoding="utf-8") == original
    assert credential_path.read_text(encoding="utf-8") == "concurrent-owner\n"
    assert (
        config_path.with_suffix(config_path.suffix + ".legacy-auth-backup").read_bytes()
        == original.encode()
    )


def test_legacy_auth_migration_refuses_concurrent_backup_publication(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    config_path = tmp_path / "config.yaml"
    credential_root = tmp_path / "credentials"
    backup_path = config_path.with_suffix(config_path.suffix + ".legacy-auth-backup")
    original = yaml.safe_dump(
        {
            "version": 2,
            "registry": {
                "mcp_servers": [
                    {
                        "id": "dispatcher",
                        "type": "stdio",
                        "command": "dispatcher-cli",
                        "env": {"DISPATCHER_TOKEN": LEGACY_SECRET},
                    }
                ]
            },
        },
        sort_keys=False,
    )
    config_path.write_text(original, encoding="utf-8")

    def publish_competing_file(path: Path) -> None:
        if path == backup_path and not backup_path.exists():
            backup_path.write_text("concurrent-backup\n", encoding="utf-8")
            backup_path.chmod(0o600)

    monkeypatch.setattr(
        migration,
        "_before_no_replace_publication",
        publish_competing_file,
        raising=False,
    )

    with pytest.raises(SystemExit) as exc_info:
        _run_migration(monkeypatch, config_path, credential_root)

    assert exc_info.value.code == 1
    assert LEGACY_SECRET not in _captured_text(capsys)
    assert config_path.read_text(encoding="utf-8") == original
    assert backup_path.read_text(encoding="utf-8") == "concurrent-backup\n"
    assert not (credential_root / "dispatcher").exists()


def test_legacy_auth_migration_serializes_concurrent_profile_mutation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config_path = tmp_path / "config.yaml"
    credential_root = tmp_path / "credentials"
    backup_path = config_path.with_suffix(config_path.suffix + ".legacy-auth-backup")
    original = yaml.safe_dump(
        {
            "version": 2,
            "registry": {
                "mcp_servers": [
                    {
                        "id": "dispatcher",
                        "type": "stdio",
                        "command": "dispatcher-cli",
                        "env": {"DISPATCHER_TOKEN": LEGACY_SECRET},
                    }
                ]
            },
        },
        sort_keys=False,
    )
    config_path.write_text(original, encoding="utf-8")
    entered_publication = Event()
    release_publication = Event()
    paused = False

    def pause_before_backup(path: Path) -> None:
        nonlocal paused
        if path == backup_path and not paused:
            paused = True
            entered_publication.set()
            assert release_publication.wait(timeout=5)

    monkeypatch.setattr(
        migration,
        "_before_no_replace_publication",
        pause_before_backup,
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        migration_future = executor.submit(
            migration.migrate_legacy_auth,
            config_path,
            credential_root,
        )
        assert entered_publication.wait(timeout=5)
        def concurrent_profile_mutation(data: dict[str, object]) -> bool:
            data["profiles"] = {
                "default": "operator",
                "entries": {"operator": {"constrained": False}},
            }
            return True

        profile_future = executor.submit(
            registry._mutate_yaml,
            config_path,
            concurrent_profile_mutation,
            lock_required=True,
        )
        with suppress(TimeoutError):
            profile_future.result(timeout=0.5)
        release_publication.set()
        migration_future.result(timeout=5)
        profile_future.result(timeout=5)

    data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert data["registry"]["mcp_servers"][0]["auth"] == {
        "credential_ref": "dispatcher",
        "stdio_environment": "DISPATCHER_TOKEN",
    }
    assert "operator" in data["profiles"]["entries"]
    assert LEGACY_SECRET in backup_path.read_text(encoding="utf-8")
    assert "profiles" not in yaml.safe_load(backup_path.read_text(encoding="utf-8"))


def test_legacy_auth_migration_refuses_existing_credential_collision(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    config_path = tmp_path / "config.yaml"
    credential_root = tmp_path / "credentials"
    credential_root.mkdir(mode=0o700)
    existing = credential_root / "dispatcher"
    existing.write_text("already-owned\n", encoding="utf-8")
    existing.chmod(0o600)
    original = yaml.safe_dump(
        {
            "version": 2,
            "registry": {
                "mcp_servers": [
                    {
                        "id": "dispatcher",
                        "type": "stdio",
                        "command": "dispatcher-cli",
                        "env": {"DISPATCHER_TOKEN": LEGACY_SECRET},
                    }
                ]
            },
        },
        sort_keys=False,
    )
    config_path.write_text(original, encoding="utf-8")

    with pytest.raises(SystemExit) as exc_info:
        _run_migration(monkeypatch, config_path, credential_root)

    assert exc_info.value.code == 1
    assert LEGACY_SECRET not in _captured_text(capsys)
    assert config_path.read_text(encoding="utf-8") == original
    assert existing.read_text(encoding="utf-8") == "already-owned\n"


def test_legacy_auth_migration_refuses_symlink_config_without_writes(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    target_path = tmp_path / "target.yaml"
    config_path = tmp_path / "config.yaml"
    credential_root = tmp_path / "credentials"
    original = yaml.safe_dump(
        {
            "version": 2,
            "registry": {
                "mcp_servers": [
                    {
                        "id": "dispatcher",
                        "type": "stdio",
                        "command": "dispatcher-cli",
                        "env": {"DISPATCHER_TOKEN": LEGACY_SECRET},
                    }
                ]
            },
        },
        sort_keys=False,
    )
    target_path.write_text(original, encoding="utf-8")
    config_path.symlink_to(target_path)

    with pytest.raises(SystemExit) as exc_info:
        _run_migration(monkeypatch, config_path, credential_root)

    assert exc_info.value.code == 1
    assert LEGACY_SECRET not in _captured_text(capsys)
    assert target_path.read_text(encoding="utf-8") == original
    assert not credential_root.exists()
    assert not target_path.with_suffix(target_path.suffix + ".legacy-auth-backup").exists()


def test_legacy_auth_migration_refuses_unsafe_credential_root_permissions(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    config_path = tmp_path / "config.yaml"
    credential_root = tmp_path / "credentials"
    credential_root.mkdir(mode=0o700)
    credential_root.chmod(0o755)
    original = yaml.safe_dump(
        {
            "version": 2,
            "registry": {
                "mcp_servers": [
                    {
                        "id": "dispatcher",
                        "type": "stdio",
                        "command": "dispatcher-cli",
                        "env": {"DISPATCHER_TOKEN": LEGACY_SECRET},
                    }
                ]
            },
        },
        sort_keys=False,
    )
    config_path.write_text(original, encoding="utf-8")

    with pytest.raises(SystemExit) as exc_info:
        _run_migration(monkeypatch, config_path, credential_root)

    assert exc_info.value.code == 1
    assert LEGACY_SECRET not in _captured_text(capsys)
    assert config_path.read_text(encoding="utf-8") == original
    assert list(credential_root.iterdir()) == []
