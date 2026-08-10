"""Pre-strict-load migration for legacy plaintext stdio credentials."""

from __future__ import annotations

import os
import stat
import tempfile
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from gearcore_hub import registry as registry_mutation
from gearcore_hub.config import (
    GlobalConfig,
    _arguments_contain_plaintext_auth,
    _contains_plaintext_auth_route,
    _url_contains_plaintext_auth,
)
from gearcore_hub.credentials import validate_credential_id


class LegacyAuthMigrationError(RuntimeError):
    """Raised when legacy auth migration cannot complete safely."""


@dataclass(frozen=True)
class LegacyAuthMigrationResult:
    migrated: int
    credential_refs: tuple[str, ...] = ()


FileIdentity = tuple[int, int, int, int, int]


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _absolute_without_resolving_symlinks(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(Path(path).expanduser())))


def _ensure_credential_root(root: Path) -> None:
    if root.is_symlink():
        raise LegacyAuthMigrationError("unsafe legacy auth migration input")
    try:
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError as exc:
        raise LegacyAuthMigrationError("unsafe legacy auth migration input") from exc
    try:
        metadata = root.stat()
    except OSError as exc:
        raise LegacyAuthMigrationError("unsafe legacy auth migration input") from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o700
        or (hasattr(os, "getuid") and metadata.st_uid != os.getuid())
    ):
        raise LegacyAuthMigrationError("unsafe legacy auth migration input")


def _load_yaml(path: Path) -> dict[str, Any]:
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise LegacyAuthMigrationError("legacy auth migration input is invalid") from exc
    if payload is None:
        return {}
    if not isinstance(payload, dict):
        raise LegacyAuthMigrationError("legacy auth migration input is invalid")
    return payload


def _dump_yaml(payload: dict[str, Any]) -> bytes:
    return yaml.safe_dump(
        payload,
        default_flow_style=False,
        allow_unicode=True,
        sort_keys=False,
    ).encode("utf-8")


def _write_owner_only(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fchmod(handle.fileno(), 0o600)
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except BaseException:
        with suppress(OSError):
            temporary.unlink()
        raise


def _before_no_replace_publication(path: Path) -> None:
    """Test seam for deterministic concurrent publication checks."""


def _path_identity(path: Path) -> FileIdentity | None:
    try:
        metadata = path.lstat()
    except OSError:
        return None
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        return None
    return _metadata_identity(metadata)


def _metadata_identity(metadata: os.stat_result) -> FileIdentity:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _unlink_if_identity(path: Path, identity: FileIdentity) -> None:
    # There is no portable, race-free "unlink this path only if it still names
    # this exact file" primitive. A same-UID process could swap the pathname
    # between a checked lstat and path unlink. Leave exact residue in place for
    # verified recovery rather than risk deleting someone else's replacement.
    _ = (path, identity)


def _before_recovery_content_read(path: Path) -> None:
    """Test seam for deterministic recovery read race checks."""


def _owner_only_file_identity_has_bytes(
    path: Path,
    expected: bytes,
) -> FileIdentity | None:
    if not hasattr(os, "O_NOFOLLOW"):
        return None
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(path, flags)
        before = os.fstat(descriptor)
        if not _owner_only_metadata_is_safe(before):
            return None
        before_identity = _metadata_identity(before)
        _before_recovery_content_read(path)
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 65536):
            chunks.append(chunk)
        after = os.fstat(descriptor)
        try:
            named = path.lstat()
        except OSError:
            return None
        if (
            not _owner_only_metadata_is_safe(after)
            or not _owner_only_metadata_is_safe(named)
            or _metadata_identity(after) != before_identity
            or (named.st_dev, named.st_ino) != (after.st_dev, after.st_ino)
        ):
            return None
        if b"".join(chunks) != expected:
            return None
        return before_identity
    except OSError:
        return None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _owner_only_file_has_bytes(path: Path, expected: bytes) -> bool:
    return _owner_only_file_identity_has_bytes(path, expected) is not None


def _owner_only_metadata_is_safe(metadata: os.stat_result) -> bool:
    return (
        stat.S_ISREG(metadata.st_mode)
        and metadata.st_nlink == 1
        and stat.S_IMODE(metadata.st_mode) == 0o600
        and (not hasattr(os, "getuid") or metadata.st_uid == os.getuid())
    )


def _write_new_owner_only(path: Path, data: bytes) -> FileIdentity:
    if path.exists() or path.is_symlink():
        raise LegacyAuthMigrationError("legacy auth migration collision")
    path.parent.mkdir(parents=True, exist_ok=True)
    _before_no_replace_publication(path)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    descriptor: int | None = None
    identity: FileIdentity | None = None
    try:
        try:
            descriptor = os.open(path, flags, 0o600)
        except FileExistsError as exc:
            raise LegacyAuthMigrationError("legacy auth migration collision") from exc
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise OSError("unsafe credential target")
        identity = _metadata_identity(metadata)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = None
            handle.write(data)
            handle.flush()
            os.fchmod(handle.fileno(), 0o600)
            os.fsync(handle.fileno())
            identity = _metadata_identity(os.fstat(handle.fileno()))
        _fsync_directory(path.parent)
        if _path_identity(path) != identity:
            raise LegacyAuthMigrationError("legacy auth migration collision")
        return identity
    except BaseException:
        if descriptor is not None:
            os.close(descriptor)
        if identity is not None:
            _unlink_if_identity(path, identity)
        raise


def _plaintext_detected(server: dict[str, Any]) -> bool:
    return (
        _contains_plaintext_auth_route(server, server_root=True)
        or _arguments_contain_plaintext_auth(server.get("args"))
        or _url_contains_plaintext_auth(server.get("url"))
    )


def _sensitive_env_entries(env: Any) -> list[tuple[str, str]]:
    if not isinstance(env, dict):
        return []
    candidates: list[tuple[str, str]] = []
    for key, value in env.items():
        if not isinstance(key, str) or not isinstance(value, str) or not value:
            continue
        if _contains_plaintext_auth_route({key: value}):
            candidates.append((key, value))
    return candidates


def _without_env_secret(server: dict[str, Any], env_name: str) -> dict[str, Any]:
    candidate = dict(server)
    env = dict(candidate.get("env") or {})
    env.pop(env_name, None)
    if env:
        candidate["env"] = env
    else:
        candidate.pop("env", None)
    return candidate


def _plan_server_migration(
    server: Any,
) -> tuple[dict[str, Any], tuple[str, str, str] | None]:
    if not isinstance(server, dict):
        raise LegacyAuthMigrationError("legacy auth migration input is invalid")
    if not _plaintext_detected(server):
        return server, None
    if server.get("type", "stdio") != "stdio":
        raise LegacyAuthMigrationError("legacy auth migration is ambiguous")
    if server.get("auth") is not None:
        raise LegacyAuthMigrationError("legacy auth migration is ambiguous")
    if _arguments_contain_plaintext_auth(server.get("args")) or _url_contains_plaintext_auth(
        server.get("url")
    ):
        raise LegacyAuthMigrationError("legacy auth migration is ambiguous")
    env_candidates = _sensitive_env_entries(server.get("env"))
    if len(env_candidates) != 1:
        raise LegacyAuthMigrationError("legacy auth migration is ambiguous")
    env_name, secret = env_candidates[0]
    credential_ref = validate_credential_id(str(server.get("id", "")))
    migrated = _without_env_secret(server, env_name)
    if _plaintext_detected(migrated):
        raise LegacyAuthMigrationError("legacy auth migration is ambiguous")
    migrated["auth"] = {
        "credential_ref": credential_ref,
        "stdio_environment": env_name,
    }
    return migrated, (credential_ref, env_name, secret)


def _planned_payload(
    payload: dict[str, Any],
) -> tuple[dict[str, Any], list[tuple[str, str, str]]]:
    registry = payload.get("registry")
    if not isinstance(registry, dict):
        return payload, []
    servers = registry.get("mcp_servers")
    if not isinstance(servers, list):
        return payload, []

    planned_payload = dict(payload)
    planned_registry = dict(registry)
    planned_servers: list[Any] = []
    credentials: list[tuple[str, str, str]] = []
    for server in servers:
        migrated_server, credential = _plan_server_migration(server)
        planned_servers.append(migrated_server)
        if credential is not None:
            credentials.append(credential)
    planned_registry["mcp_servers"] = planned_servers
    planned_payload["registry"] = planned_registry
    return planned_payload, credentials


def _allocate_backup_path(config_path: Path) -> Path:
    base = config_path.with_suffix(config_path.suffix + ".legacy-auth-backup")
    if not base.exists() and not base.is_symlink():
        return base
    for index in range(1, 1000):
        candidate = config_path.with_suffix(
            config_path.suffix + f".legacy-auth-backup.{index}"
        )
        if not candidate.exists() and not candidate.is_symlink():
            return candidate
    raise LegacyAuthMigrationError("legacy auth migration collision")


def _matching_backup_path(
    config_path: Path,
    original: bytes,
) -> tuple[Path, FileIdentity] | None:
    base = config_path.with_suffix(config_path.suffix + ".legacy-auth-backup")
    candidates = [base, *sorted(config_path.parent.glob(f"{base.name}.*"))]
    for candidate in candidates:
        identity = _owner_only_file_identity_has_bytes(candidate, original)
        if identity is not None:
            return candidate, identity
    return None


def _before_recovery_commit_verification() -> None:
    """Test seam for deterministic final recovery binding checks."""


def _file_has_exact_bytes(path: Path, expected: bytes) -> bool:
    try:
        return path.read_bytes() == expected
    except OSError:
        return False


def migrate_legacy_auth(
    config_path: Path,
    credential_root: Path,
) -> LegacyAuthMigrationResult:
    config_path = _absolute_without_resolving_symlinks(config_path)
    credential_root = _absolute_without_resolving_symlinks(credential_root)
    with registry_mutation._MutationLock(config_path, required=True) as locked:
        try:
            payload, snapshot = registry_mutation._read_profile_document(
                config_path,
                locked.directory_fd,
            )
        except ValueError as exc:
            raise LegacyAuthMigrationError(
                "legacy auth migration input is invalid"
            ) from exc

        migrated_payload, credentials = _planned_payload(payload)
        if not credentials:
            GlobalConfig(**payload)
            return LegacyAuthMigrationResult(migrated=0)

        credential_refs = tuple(
            credential_ref for credential_ref, _env, _secret in credentials
        )
        if len(set(credential_refs)) != len(credential_refs):
            raise LegacyAuthMigrationError("legacy auth migration collision")
        _ensure_credential_root(credential_root)

        # Prove strict loading accepts the migrated document before any
        # filesystem mutation is committed.
        GlobalConfig(**migrated_payload)

        original_bytes = b"" if snapshot is None else snapshot.raw
        migrated_bytes = _dump_yaml(migrated_payload)
        recovery_backup = _matching_backup_path(config_path, original_bytes)
        recovery_backup_path: Path | None = None
        recovery_backup_identity: FileIdentity | None = None
        if recovery_backup is not None:
            recovery_backup_path, recovery_backup_identity = recovery_backup
        backup_path = recovery_backup_path or _allocate_backup_path(config_path)

        recovered_credentials: dict[Path, tuple[bytes, FileIdentity]] = {}
        for credential_ref, _env_name, secret in credentials:
            credential_path = credential_root / credential_ref
            expected = f"{secret}\n".encode()
            if credential_path.exists() or credential_path.is_symlink():
                credential_identity = _owner_only_file_identity_has_bytes(
                    credential_path,
                    expected,
                )
                if recovery_backup is not None and credential_identity is not None:
                    recovered_credentials[credential_path] = (
                        expected,
                        credential_identity,
                    )
                    continue
                raise LegacyAuthMigrationError("legacy auth migration collision")

        created_paths: list[tuple[Path, FileIdentity]] = []
        config_committed = False
        try:
            if recovery_backup is None:
                backup_identity = _write_new_owner_only(backup_path, original_bytes)
                created_paths.append((backup_path, backup_identity))
            for credential_ref, _env_name, secret in credentials:
                credential_path = credential_root / credential_ref
                if credential_path in recovered_credentials:
                    continue
                credential_identity = _write_new_owner_only(
                    credential_path,
                    f"{secret}\n".encode(),
                )
                created_paths.append((credential_path, credential_identity))
            try:
                if recovery_backup_path is not None:
                    _before_recovery_commit_verification()
                    if (
                        recovery_backup_identity is None
                        or _owner_only_file_identity_has_bytes(
                            recovery_backup_path,
                            original_bytes,
                        )
                        != recovery_backup_identity
                    ):
                        raise LegacyAuthMigrationError(
                            "legacy auth migration collision"
                        )
                    for (
                        credential_path,
                        (expected, expected_identity),
                    ) in recovered_credentials.items():
                        if (
                            _owner_only_file_identity_has_bytes(
                                credential_path,
                                expected,
                            )
                            != expected_identity
                        ):
                            raise LegacyAuthMigrationError(
                                "legacy auth migration collision"
                            )
                registry_mutation._atomic_replace_yaml(
                    config_path,
                    migrated_payload,
                    original_bytes,
                    directory_fd=locked.directory_fd,
                    snapshot=snapshot,
                    replacement_mode=0o600,
                )
                _fsync_directory(config_path.parent)
                config_committed = True
            except BaseException:
                config_committed = _file_has_exact_bytes(config_path, migrated_bytes)
                raise
        except BaseException:
            if not config_committed:
                for created_path, identity in reversed(created_paths):
                    _unlink_if_identity(created_path, identity)
            raise

        return LegacyAuthMigrationResult(
            migrated=len(credentials),
            credential_refs=credential_refs,
        )
