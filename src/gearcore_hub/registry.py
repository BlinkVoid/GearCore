"""
Registry management commands: add-mcp, add-skill, add-cli.

All mutations target the global config (~/.config/gearcore/config.yaml) by default.
Pass scope="project" + project_root to target the project's .gearcore/config.yaml instead.

Note: add-cli requires CLI-Anything (https://github.com/HKUDS/CLI-Anything) to be
installed and available on PATH as `cli-anything`.
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import json
import logging
import os
import secrets
import shutil
import stat
import subprocess
import tempfile
import threading
import unicodedata
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from gearcore_hub.config import (
    GLOBAL_CONFIG_PATH,
    EffectiveConfig,
    GlobalConfig,
    ProjectConfig,
)
from gearcore_hub.logging_utils import silence_logger

try:
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - exercised by platform simulation
    _fcntl = None  # type: ignore[assignment]

try:
    import msvcrt as _msvcrt
except ImportError:  # pragma: no cover - normal on POSIX
    _msvcrt = None  # type: ignore[assignment]

logger = logging.getLogger("gearcore.registry")


class _UniqueKeyLoader(yaml.SafeLoader):
    """YAML loader that rejects ambiguous duplicate mapping keys."""


def _construct_unique_mapping(
    loader: _UniqueKeyLoader, node: yaml.MappingNode, deep: bool = False
) -> dict[Any, Any]:
    loader.flatten_mapping(node)
    result: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in result
        except TypeError:
            raise yaml.constructor.ConstructorError(
                None, None, "invalid mapping key", key_node.start_mark
            ) from None
        if duplicate:
            raise yaml.constructor.ConstructorError(
                None, None, "duplicate mapping key", key_node.start_mark
            )
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


@dataclass(frozen=True, slots=True)
class ProfileSetResult:
    path: Path
    profile: str
    changed: bool
    selected_default: bool


@dataclass(frozen=True, slots=True)
class _DocumentSnapshot:
    raw: bytes
    fingerprint: tuple[int, int, int, int]


_PROCESS_FALLBACK_LOCK = threading.RLock()
_MAX_CAPABILITY_ID_LENGTH = 512

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _MutationLock:
    """Cross-command lock anchored beside the configuration target."""

    def __init__(self, target: Path, *, required: bool):
        self.target = target
        self.required = required
        self.directory_fd: int | None = None
        self.lock_fd: int | None = None
        self._fallback = False

    def __enter__(self) -> _MutationLock:
        self.target.parent.mkdir(parents=True, exist_ok=True)
        if _fcntl is not None:
            flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            self.directory_fd = os.open(self.target.parent, flags)
            _fcntl.flock(self.directory_fd, _fcntl.LOCK_EX)
            return self
        if _msvcrt is not None:
            lock_path = self.target.parent / f".{self.target.name}.lock"
            base_flags = os.O_RDWR | getattr(os, "O_BINARY", 0)
            base_flags |= getattr(os, "O_NOFOLLOW", 0)
            try:
                before: os.stat_result | None = None
                created = False
                try:
                    before = lock_path.lstat()
                    if (
                        lock_path.is_symlink()
                        or not stat.S_ISREG(before.st_mode)
                        or before.st_nlink != 1
                    ):
                        raise OSError("invalid mutation lock")
                    self.lock_fd = os.open(lock_path, base_flags)
                except FileNotFoundError:
                    self.lock_fd = os.open(
                        lock_path,
                        base_flags | os.O_CREAT | os.O_EXCL,
                        0o600,
                    )
                    created = True
                metadata = os.fstat(self.lock_fd)
                if (
                    not stat.S_ISREG(metadata.st_mode)
                    or metadata.st_nlink != 1
                    or (
                        hasattr(os, "geteuid")
                        and metadata.st_uid != os.geteuid()
                    )
                ):
                    raise OSError("invalid mutation lock")
                after_path = lock_path.lstat()
                if (
                    lock_path.is_symlink()
                    or not stat.S_ISREG(after_path.st_mode)
                    or after_path.st_nlink != 1
                    or (after_path.st_dev, after_path.st_ino)
                    != (metadata.st_dev, metadata.st_ino)
                ):
                    raise OSError("mutation lock changed")
                if before is not None and (
                    before.st_dev,
                    before.st_ino,
                ) != (metadata.st_dev, metadata.st_ino):
                    raise OSError("mutation lock changed")
                if created:
                    os.write(self.lock_fd, b"\0")
                    os.fsync(self.lock_fd)
                elif metadata.st_size < 1:
                    raise OSError("invalid mutation lock")
                os.lseek(self.lock_fd, 0, os.SEEK_SET)
                _msvcrt.locking(self.lock_fd, _msvcrt.LK_LOCK, 1)
                locked_metadata = os.fstat(self.lock_fd)
                locked_path = lock_path.lstat()
                if (
                    lock_path.is_symlink()
                    or not stat.S_ISREG(locked_metadata.st_mode)
                    or locked_metadata.st_nlink != 1
                    or locked_path.st_nlink != 1
                    or (locked_path.st_dev, locked_path.st_ino)
                    != (locked_metadata.st_dev, locked_metadata.st_ino)
                    or (locked_metadata.st_dev, locked_metadata.st_ino)
                    != (metadata.st_dev, metadata.st_ino)
                ):
                    raise OSError("mutation lock changed")
            except Exception:
                if self.lock_fd is not None:
                    os.close(self.lock_fd)
                    self.lock_fd = None
                raise RuntimeError("configuration locking is unavailable") from None
            return self
        if self.required:
            raise RuntimeError("configuration locking is unavailable")
        _PROCESS_FALLBACK_LOCK.acquire()
        self._fallback = True
        return self

    def __exit__(self, *exc_info: object) -> None:
        if self.directory_fd is not None:
            assert _fcntl is not None
            _fcntl.flock(self.directory_fd, _fcntl.LOCK_UN)
            os.close(self.directory_fd)
            self.directory_fd = None
        if self.lock_fd is not None:
            assert _msvcrt is not None
            os.lseek(self.lock_fd, 0, os.SEEK_SET)
            _msvcrt.locking(  # type: ignore[attr-defined]
                self.lock_fd,
                _msvcrt.LK_UNLCK,  # type: ignore[attr-defined]
                1,
            )
            os.close(self.lock_fd)
            self.lock_fd = None
        if self._fallback:
            _PROCESS_FALLBACK_LOCK.release()
            self._fallback = False


def _open_target(path: Path, directory_fd: int | None) -> int:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    if directory_fd is not None and os.open in os.supports_dir_fd:
        return os.open(path.name, flags, dir_fd=directory_fd)
    before = path.lstat()
    if path.is_symlink() or not stat.S_ISREG(before.st_mode):
        raise OSError(errno.ELOOP, "unsafe configuration target")
    descriptor = os.open(path, flags)
    after = os.fstat(descriptor)
    if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
        os.close(descriptor)
        raise OSError(errno.ESTALE, "configuration target changed")
    return descriptor


def _read_profile_document(
    path: Path, directory_fd: int | None = None
) -> tuple[dict[str, Any], _DocumentSnapshot | None]:
    """Read a regular target without following symlinks or retaining parse text."""

    descriptor: int | None = None
    try:
        try:
            descriptor = _open_target(path, directory_fd)
        except FileNotFoundError:
            return {}, None
        except OSError as exc:
            if exc.errno in {errno.ELOOP, errno.ENOTDIR, errno.EINVAL}:
                raise ValueError("configuration target must be a regular file") from None
            raise
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("configuration target must be a regular file")
        with os.fdopen(descriptor, "rb", closefd=True) as stream:
            descriptor = None
            raw = stream.read()
    except ValueError:
        raise
    except Exception:
        raise ValueError("invalid configuration document") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
    try:
        data = yaml.load(raw.decode("utf-8"), Loader=_UniqueKeyLoader) or {}
    except Exception:
        raise ValueError("invalid configuration document") from None
    if not isinstance(data, dict):
        raise ValueError("invalid configuration document")
    return data, _DocumentSnapshot(
        raw=raw,
        fingerprint=(
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_size,
            metadata.st_mtime_ns,
        ),
    )


def _assert_target_unchanged(
    path: Path,
    snapshot: _DocumentSnapshot | None,
    directory_fd: int | None,
) -> None:
    descriptor: int | None = None
    try:
        descriptor = _open_target(path, directory_fd)
    except FileNotFoundError:
        if snapshot is None:
            return
        raise RuntimeError("configuration changed during mutation") from None
    except OSError:
        raise RuntimeError("configuration changed during mutation") from None
    try:
        metadata = os.fstat(descriptor)
        current = (
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_size,
            metadata.st_mtime_ns,
        )
        if snapshot is None or current != snapshot.fingerprint:
            raise RuntimeError("configuration changed during mutation")
    finally:
        os.close(descriptor)


def _read_yaml(path: Path) -> dict[str, Any]:
    data, _ = _read_profile_document(path)
    return data


def _validate_unique(values: Iterable[str], label: str) -> tuple[str, ...]:
    materialized = tuple(values)
    try:
        for value in materialized:
            _validate_capability_id(value)
    except ValueError:
        raise ValueError(f"invalid capability ID in {label}") from None
    if len(materialized) != len(set(materialized)):
        raise ValueError(f"duplicate capability ID in {label}")
    return materialized


def _validate_capability_id(value: str, label: str = "capability") -> str:
    """Accept v2-compatible text while rejecting control/format injection."""

    if (
        not isinstance(value, str)
        or not value
        or len(value) > _MAX_CAPABILITY_ID_LENGTH
        or any(unicodedata.category(character) in {"Cc", "Cf"} for character in value)
    ):
        raise ValueError(f"invalid {label} ID")
    return value


def _validate_filesystem_skill_name(value: str) -> str:
    """Validate an ID that will also be used as one directory entry name."""

    _validate_capability_id(value, "skill")
    separators = {"/", "\\"}
    if value in {".", ".."} or any(separator in value for separator in separators):
        raise ValueError("invalid skill ID")
    return value


def _capability_policy(
    *,
    include: Iterable[str],
    deny: Iterable[str],
    protected: Iterable[str],
    label: str,
) -> dict[str, Any]:
    included = _validate_unique(include, f"{label} include")
    denied = _validate_unique(deny, f"{label} deny")
    protected_ids = _validate_unique(protected, f"{label} protect")
    contradictory = (set(included) & set(denied)) | (
        set(denied) & set(protected_ids)
    )
    if contradictory:
        raise ValueError(f"contradictory {label} capability policy")
    policy: dict[str, Any] = {}
    if included:
        policy["include"] = list(included)
    if denied:
        policy["deny"] = list(denied)
    if protected_ids:
        policy["protected"] = list(protected_ids)
    return policy


def _enabled_global_mcp_ids(config: GlobalConfig) -> set[str]:
    """Return enabled IDs from the normalized, revalidated global model."""

    servers = config.mcp_servers
    definition_ids = [server.id for server in servers]
    if len(definition_ids) != len(set(definition_ids)):
        raise ValueError("duplicate global MCP definition")
    return {server.id for server in servers if server.enabled}


def _globally_protected_ids(
    config: GlobalConfig, capability_kind: str
) -> set[str]:
    if config.profiles is None:
        return set()
    return {
        capability_id
        for profile in config.profiles.entries.values()
        for capability_id in getattr(profile.scope, capability_kind).protected
    }


def _trusted_global_skill_ids(config: GlobalConfig) -> tuple[set[str], set[str]]:
    """Resolve unique global bundles with the runtime manifest semantics."""

    from gearcore_hub.skill_manager import SkillManager

    try:
        with silence_logger("gearcore.skill_manager"):
            manager = SkillManager(EffectiveConfig(config, None, None))
    except Exception:
        raise ValueError("invalid trusted global skill registry") from None

    candidates: defaultdict[str, list[Path]] = defaultdict(list)
    for root in config.skills_dirs:
        try:
            for bundle in root.iterdir():
                if not bundle.is_dir() or not (bundle / "SKILL.md").is_file():
                    continue
                try:
                    identity = _skill_manifest_identity(bundle)
                except Exception:
                    continue
                # Each directory binding is significant even when two symlink
                # aliases happen to resolve to the same target. Runtime scan
                # order would otherwise silently choose a winner.
                candidates[identity].append(bundle.absolute())
        except OSError:
            continue

    conflicting = {
        name for name, bindings in candidates.items() if len(bindings) != 1
    }
    trusted = {
        name
        for name, bindings in candidates.items()
        if len(bindings) == 1
        and name in manager.skills
        and manager.skills[name].manifest.name == name
        and manager.skills[name].path.absolute() == bindings[0]
        and not manager.skills[name].is_project_local
    }
    return trusted, conflicting


def _skill_manifest_identity(bundle: Path) -> str:
    """Load the identity a runtime skill bundle would advertise."""

    from gearcore_hub.skill_manager import SkillManifest

    manifest_path = bundle / "manifest.json"
    try:
        if manifest_path.exists():
            manifest = SkillManifest.model_validate_json(
                manifest_path.read_text(encoding="utf-8")
            )
        else:
            manifest = SkillManifest(name=bundle.name)
    except Exception:
        raise ValueError("invalid skill manifest") from None
    return _validate_capability_id(manifest.name, "skill")


def _protected_and_core_skill_ids(config: GlobalConfig) -> set[str]:
    protected = _globally_protected_ids(config, "skills")
    if config.profiles is None:
        return protected.union(config.disclosure.core_skills)
    return protected.union(
        skill
        for profile in config.profiles.entries.values()
        for skill in profile.disclosure.core_skills
    )


def _atomic_replace_yaml(
    path: Path,
    data: dict[str, Any],
    original: bytes,
    *,
    directory_fd: int | None = None,
    snapshot: _DocumentSnapshot | None = None,
) -> bool:
    rendered = yaml.safe_dump(
        data,
        default_flow_style=False,
        allow_unicode=True,
        sort_keys=False,
    ).encode("utf-8")
    _assert_target_unchanged(path, snapshot, directory_fd)
    if rendered == original:
        return False

    mode = 0o600
    original_descriptor = -1
    if snapshot is not None:
        try:
            original_descriptor = _open_target(path, directory_fd)
            original_metadata = os.fstat(original_descriptor)
            if (
                original_metadata.st_dev,
                original_metadata.st_ino,
                original_metadata.st_size,
                original_metadata.st_mtime_ns,
            ) != snapshot.fingerprint:
                raise OSError("configuration target changed")
            mode = stat.S_IMODE(original_metadata.st_mode)
        except OSError:
            raise RuntimeError("configuration changed during mutation") from None

    temporary: _StagedFile | None = None
    backup: _StagedFile | None = None
    try:
        if snapshot is not None:
            backup = _create_staged_file(
                path, original, mode, ".bak", directory_fd
            )
        temporary = _create_staged_file(
            path, rendered, mode, ".tmp", directory_fd
        )
        _assert_target_unchanged(path, snapshot, directory_fd)
        if directory_fd is None and original_descriptor >= 0:
            # Windows replacement requires the destination not be held open.
            os.close(original_descriptor)
            original_descriptor = -1
        _atomic_replace_probe(
            path,
            temporary.name,
            temporary.path,
            directory_fd,
        )
        _replace_staged_file(temporary, path, directory_fd)
        if not _target_matches(path, directory_fd, temporary):
            _restore_original(
                path,
                snapshot,
                original,
                mode,
                backup,
                directory_fd,
            )
            raise OSError("configuration temporary changed")
        _sync_directory(path, directory_fd)
    except Exception:
        raise RuntimeError("configuration write failed") from None
    finally:
        _cleanup_staged_file(temporary, directory_fd)
        _cleanup_staged_file(backup, directory_fd)
        if original_descriptor >= 0:
            os.close(original_descriptor)
        with contextlib.suppress(OSError):
            _sync_directory(path, directory_fd)
    return True


@dataclass(slots=True)
class _StagedFile:
    name: str
    path: Path | None
    descriptor: int
    fingerprint: tuple[int, int]
    digest: bytes
    consumed: bool = False


def _atomic_replace_probe(
    path: Path,
    name: str,
    temporary: Path | None,
    directory_fd: int | None,
) -> None:
    """Deterministic no-op probe used to exercise the final replace race."""


def _create_staged_file(
    target: Path,
    content: bytes,
    mode: int,
    suffix: str,
    directory_fd: int | None,
) -> _StagedFile:
    descriptor = -1
    staged_path: Path | None = None
    name = ""
    fingerprint: tuple[int, int] | None = None
    try:
        if directory_fd is not None:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            for _attempt in range(32):
                name = f".{target.name}.{secrets.token_hex(16)}{suffix}"
                try:
                    descriptor = os.open(
                        name, flags, 0o600, dir_fd=directory_fd
                    )
                    break
                except FileExistsError:
                    continue
            else:
                raise OSError("unable to allocate configuration temporary")
        else:
            descriptor, raw_path = tempfile.mkstemp(
                dir=target.parent,
                prefix=f".{target.name}.",
                suffix=suffix,
            )
            staged_path = Path(raw_path)
            name = staged_path.name
        os.fchmod(descriptor, mode)
        _write_all(descriptor, content)
        os.fsync(descriptor)
        if directory_fd is not None:
            fingerprint = _verify_temporary_binding(
                name, descriptor, directory_fd, expected_mode=mode
            )
        else:
            metadata = os.fstat(descriptor)
            fingerprint = (metadata.st_dev, metadata.st_ino)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or stat.S_IMODE(metadata.st_mode) != mode
                or not _path_name_matches(staged_path, fingerprint)
            ):
                raise OSError("unsafe configuration temporary")
            # The Windows path fallback cannot replace an open source file.
            os.close(descriptor)
            descriptor = -1
        assert fingerprint is not None
        return _StagedFile(
            name,
            staged_path,
            descriptor,
            fingerprint,
            hashlib.sha256(content).digest(),
        )
    except Exception:
        binding_matches = False
        if descriptor >= 0:
            metadata = os.fstat(descriptor)
            fingerprint = (metadata.st_dev, metadata.st_ino)
            binding_matches = (
                _directory_name_matches(name, directory_fd, fingerprint)
                if directory_fd is not None and name
                else _path_name_matches(staged_path, fingerprint)
            )
        if descriptor >= 0:
            os.close(descriptor)
        if directory_fd is not None and name and binding_matches:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(name, dir_fd=directory_fd)
        elif staged_path is not None and binding_matches:
            with contextlib.suppress(FileNotFoundError):
                staged_path.unlink()
        raise


def _write_all(descriptor: int, content: bytes) -> None:
    view = memoryview(content)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("short configuration write")
        view = view[written:]


def _replace_staged_file(
    staged: _StagedFile,
    target: Path,
    directory_fd: int | None,
) -> None:
    if directory_fd is not None:
        os.replace(
            staged.name,
            target.name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
    else:
        assert staged.path is not None
        os.replace(staged.path, target)
    staged.consumed = True


def _restore_original(
    path: Path,
    snapshot: _DocumentSnapshot | None,
    original: bytes,
    mode: int,
    backup: _StagedFile | None,
    directory_fd: int | None,
) -> None:
    if snapshot is None:
        _unlink_target(path, directory_fd)
        if _target_exists(path, directory_fd):
            raise OSError("configuration rollback failed")
        _sync_directory(path, directory_fd)
        return
    assert backup is not None
    candidates = [backup]
    recoveries: list[_StagedFile] = []
    try:
        for _attempt in range(3):
            candidate = candidates[-1]
            if candidate.consumed or not _staged_file_matches(
                candidate, directory_fd
            ):
                recovery = _create_staged_file(
                    path, original, mode, ".bak", directory_fd
                )
                recoveries.append(recovery)
                candidates.append(recovery)
                candidate = recovery
            try:
                _replace_staged_file(candidate, path, directory_fd)
            except OSError:
                continue
            if _target_matches(path, directory_fd, candidate):
                _sync_directory(path, directory_fd)
                return
        raise OSError("configuration rollback failed")
    finally:
        for recovery in recoveries:
            _cleanup_staged_file(recovery, directory_fd)


def _unlink_target(path: Path, directory_fd: int | None) -> None:
    try:
        if directory_fd is not None:
            os.unlink(path.name, dir_fd=directory_fd)
        else:
            path.unlink()
    except FileNotFoundError:
        pass


def _target_exists(path: Path, directory_fd: int | None) -> bool:
    try:
        if directory_fd is not None:
            os.stat(path.name, dir_fd=directory_fd, follow_symlinks=False)
        else:
            path.lstat()
    except FileNotFoundError:
        return False
    return True


def _target_matches(
    path: Path,
    directory_fd: int | None,
    staged: _StagedFile,
) -> bool:
    if directory_fd is not None:
        binding_matches = _directory_name_matches(
            path.name, directory_fd, staged.fingerprint
        )
    else:
        binding_matches = _path_name_matches(path, staged.fingerprint)
    return binding_matches and _target_digest(path, directory_fd) == staged.digest


def _target_digest(path: Path, directory_fd: int | None) -> bytes | None:
    descriptor = -1
    try:
        descriptor = _open_target(path, directory_fd)
        digest = hashlib.sha256()
        while chunk := os.read(descriptor, 65536):
            digest.update(chunk)
        return digest.digest()
    except OSError:
        return None
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _staged_file_matches(
    staged: _StagedFile, directory_fd: int | None
) -> bool:
    if staged.consumed:
        return False
    if directory_fd is not None:
        return _directory_name_matches(
            staged.name, directory_fd, staged.fingerprint
        )
    return staged.path is not None and _path_name_matches(
        staged.path, staged.fingerprint
    )


def _cleanup_staged_file(
    staged: _StagedFile | None, directory_fd: int | None
) -> None:
    if staged is None:
        return
    if not staged.consumed and _staged_file_matches(staged, directory_fd):
        with contextlib.suppress(FileNotFoundError):
            if directory_fd is not None:
                os.unlink(staged.name, dir_fd=directory_fd)
            else:
                assert staged.path is not None
                staged.path.unlink()
    if staged.descriptor >= 0:
        os.close(staged.descriptor)
        staged.descriptor = -1


def _sync_directory(path: Path, directory_fd: int | None) -> None:
    sync_fd = directory_fd
    close_sync_fd = False
    if sync_fd is None:
        try:
            sync_fd = os.open(
                path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            )
            close_sync_fd = True
        except OSError:
            sync_fd = None
    try:
        if sync_fd is not None:
            try:
                os.fsync(sync_fd)
            except OSError as exc:
                unsupported = {
                    errno.EBADF,
                    errno.EINVAL,
                    errno.EROFS,
                    getattr(errno, "ENOTSUP", errno.EINVAL),
                }
                if exc.errno not in unsupported:
                    raise
    finally:
        if close_sync_fd and sync_fd is not None:
            os.close(sync_fd)


def _verify_temporary_binding(
    name: str,
    descriptor: int,
    directory_fd: int,
    *,
    expected_mode: int,
) -> tuple[int, int]:
    """Prove a temporary name is the open, regular, single-link file."""

    metadata = os.fstat(descriptor)
    named = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    fingerprint = (metadata.st_dev, metadata.st_ino)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or not stat.S_ISREG(named.st_mode)
        or named.st_nlink != 1
        or (named.st_dev, named.st_ino) != fingerprint
        or stat.S_IMODE(metadata.st_mode) != expected_mode
    ):
        raise OSError("unsafe configuration temporary")
    return fingerprint


def _directory_name_matches(
    name: str,
    directory_fd: int,
    fingerprint: tuple[int, int] | None,
) -> bool:
    if fingerprint is None:
        return False
    try:
        metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except OSError:
        return False
    return (
        stat.S_ISREG(metadata.st_mode)
        and metadata.st_nlink == 1
        and (metadata.st_dev, metadata.st_ino) == fingerprint
    )


def _path_name_matches(path: Path | None, fingerprint: tuple[int, int]) -> bool:
    if path is None:
        return False
    try:
        metadata = path.lstat()
    except OSError:
        return False
    return (
        stat.S_ISREG(metadata.st_mode)
        and metadata.st_nlink == 1
        and (metadata.st_dev, metadata.st_ino) == fingerprint
    )


def _mutate_yaml[T](
    path: Path,
    mutation: Callable[[dict[str, Any]], T],
    *,
    lock_required: bool,
) -> tuple[bool, T]:
    """Run one read/modify/validate/write transaction under the shared lock."""

    with _MutationLock(path, required=lock_required) as locked:
        data, snapshot = _read_profile_document(path, locked.directory_fd)
        result = mutation(data)
        changed = _atomic_replace_yaml(
            path,
            data,
            b"" if snapshot is None else snapshot.raw,
            directory_fd=locked.directory_fd,
            snapshot=snapshot,
        )
        return changed, result


def set_profile(
    profile: str,
    *,
    config_path: Path | None = None,
    mcp_include: Iterable[str] = (),
    mcp_deny: Iterable[str] = (),
    mcp_protect: Iterable[str] = (),
    skill_include: Iterable[str] = (),
    skill_deny: Iterable[str] = (),
    skill_protect: Iterable[str] = (),
    core_skills: Iterable[str] = (),
    constrained: bool = False,
    make_default: bool = False,
) -> ProfileSetResult:
    """Create or replace one global capability profile atomically.

    Profile authority is global-only by construction. Project config paths are
    never inferred and project overlays cannot be selected as defaults here.
    """

    _validate_capability_id(profile, "profile")
    target = config_path or GLOBAL_CONFIG_PATH
    mcp_include_ids = tuple(mcp_include)
    mcp_deny_ids = tuple(mcp_deny)
    mcp_protect_ids = tuple(mcp_protect)
    skill_include_ids = tuple(skill_include)
    skill_deny_ids = tuple(skill_deny)
    skill_protect_ids = tuple(skill_protect)
    explicit_core = tuple(core_skills)

    def mutation(data: dict[str, Any]) -> bool:
        return _apply_profile_mutation(
            data,
            profile=profile,
            mcp_include=mcp_include_ids,
            mcp_deny=mcp_deny_ids,
            mcp_protect=mcp_protect_ids,
            skill_include=skill_include_ids,
            skill_deny=skill_deny_ids,
            skill_protect=skill_protect_ids,
            core_skills=explicit_core,
            constrained=constrained,
            make_default=make_default,
        )

    changed, selected_default = _mutate_yaml(
        target, mutation, lock_required=True
    )
    return ProfileSetResult(
        path=target,
        profile=profile,
        changed=changed,
        selected_default=selected_default,
    )


def _apply_profile_mutation(
    data: dict[str, Any],
    *,
    profile: str,
    mcp_include: tuple[str, ...],
    mcp_deny: tuple[str, ...],
    mcp_protect: tuple[str, ...],
    skill_include: tuple[str, ...],
    skill_deny: tuple[str, ...],
    skill_protect: tuple[str, ...],
    core_skills: tuple[str, ...],
    constrained: bool,
    make_default: bool,
) -> bool:
    try:
        global_config = GlobalConfig.model_validate(data)
    except Exception:
        raise ValueError("invalid configuration document") from None

    mcp_policy = _capability_policy(
        include=mcp_include,
        deny=mcp_deny,
        protected=mcp_protect,
        label="MCP",
    )
    skill_policy = _capability_policy(
        include=skill_include,
        deny=skill_deny,
        protected=skill_protect,
        label="skill",
    )
    explicit_core = _validate_unique(core_skills, "core skills")
    raw_profiles = data.get("profiles")
    carry_legacy_core = raw_profiles is None or (
        isinstance(raw_profiles, dict)
        and raw_profiles.get("default") == profile
    )
    core = tuple(
        dict.fromkeys(
            (
                *(
                    global_config.disclosure.core_skills
                    if carry_legacy_core
                    else ()
                ),
                *explicit_core,
            )
        )
    )
    protected_mcps = tuple(mcp_policy.get("protected", ()))
    protected_skills = tuple(skill_policy.get("protected", ()))

    dispatcher_id = "hive-dispatcher"
    if (dispatcher_id in protected_mcps) != (
        dispatcher_id in protected_skills
    ):
        raise ValueError("hive-dispatcher protected MCP and skill must be paired")
    if mcp_include and set(protected_mcps).difference(mcp_include):
        raise ValueError("contradictory MCP capability policy")
    if skill_include and set(protected_skills).union(core).difference(skill_include):
        raise ValueError("contradictory skill capability policy")
    if set(core).intersection(skill_deny):
        raise ValueError("contradictory skill capability policy")

    enabled_mcps = _enabled_global_mcp_ids(global_config)
    if set(protected_mcps).difference(enabled_mcps):
        raise ValueError("protected capability has no enabled global MCP definition")
    requested_skills = set(protected_skills).union(core)
    if requested_skills:
        trusted_skills, conflicting_skills = _trusted_global_skill_ids(global_config)
        if requested_skills.intersection(conflicting_skills):
            raise ValueError("conflicting trusted global skill definition")
        if set(protected_skills).difference(trusted_skills):
            raise ValueError(
                "protected capability has no trusted global skill bundle"
            )
        if set(core).difference(trusted_skills):
            raise ValueError("core skill has no trusted global skill bundle")

    entries: dict[str, Any]
    old_profiles = data.get("profiles")
    current_default: object
    if old_profiles is None:
        entries = {}
        current_default = profile
    elif isinstance(old_profiles, dict) and isinstance(
        old_profiles.get("entries"), dict
    ):
        entries = dict(old_profiles["entries"])
        current_default = old_profiles.get("default")
    else:
        raise ValueError("invalid configuration document")

    profile_data: dict[str, Any] = {"constrained": constrained}
    if mcp_policy or skill_policy:
        profile_data["scope"] = {
            "mcp_servers": mcp_policy,
            "skills": skill_policy,
        }
    if (
        (old_profiles is None or current_default == profile)
        and isinstance(data.get("disclosure"), dict)
    ):
        legacy_disclosure = dict(data["disclosure"])
        legacy_disclosure["core_skills"] = list(core)
        profile_data["disclosure"] = legacy_disclosure
    elif core:
        profile_data["disclosure"] = {"core_skills": list(core)}
    entries[profile] = profile_data
    selected_default = profile if make_default else current_default
    if not isinstance(selected_default, str) or selected_default not in entries:
        raise ValueError("default must select a defined global profile")

    candidate = dict(data)
    candidate["version"] = 3
    candidate["profiles"] = {"default": selected_default, "entries": entries}
    try:
        GlobalConfig.model_validate(candidate)
    except Exception:
        raise ValueError("invalid capability profile configuration") from None
    data.clear()
    data.update(candidate)
    return selected_default == profile


def _config_path(scope: str, project_root: Path | None) -> Path:
    if scope == "project":
        if project_root is None:
            raise ValueError(
                "--scope project requires a project root (use --project <path>)"
            )
        return project_root / ".gearcore" / "config.yaml"
    return GLOBAL_CONFIG_PATH


def _skills_dir(scope: str, project_root: Path | None) -> Path:
    if scope == "project":
        if project_root is None:
            raise ValueError("--scope project requires a project root")
        return project_root / ".gearcore" / "skills"
    return Path.home() / ".config" / "gearcore" / "skills"


# ---------------------------------------------------------------------------
# add-mcp
# ---------------------------------------------------------------------------


def _server_entry(
    id: str,
    type: str,
    command: str = "",
    args: list[str] | None = None,
    url: str = "",
    env: dict[str, str] | None = None,
    enabled: bool = True,
) -> dict:
    entry: dict = {"id": id, "type": type, "enabled": enabled}
    if type == "stdio":
        entry["command"] = command
        if args:
            entry["args"] = args
        if env:
            entry["env"] = env
    else:
        entry["url"] = url
    return entry


def add_mcp(
    id: str,
    type: str,
    command: str = "",
    args: list[str] | None = None,
    url: str = "",
    env: dict[str, str] | None = None,
    scope: str = "global",
    project_root: Path | None = None,
    enabled: bool = True,
    allowlist: bool = False,
) -> Path:
    """
    Register a new MCP server in the config.

    With scope="project" + allowlist=True, no new definition is written:
    instead the id of an existing *global* server is appended to the
    project's scope.mcp_servers.include allowlist.

    Returns the path of the config file that was modified.
    Raises ValueError if an entry with the same id already exists.
    """
    _validate_capability_id(id, "MCP")
    cfg_path = _config_path(scope, project_root)

    if allowlist:
        if scope != "project":
            raise ValueError("--allowlist requires --scope project")
        global_data = _read_yaml(GLOBAL_CONFIG_PATH)
        global_ids = {
            s.get("id")
            for s in global_data.get("registry", {}).get("mcp_servers", [])
        }
        if id not in global_ids:
            raise ValueError(
                f"MCP server '{id}' is not registered globally; "
                "omit --allowlist to write a project-local definition instead"
            )

        def allowlist_mutation(data: dict[str, Any]) -> None:
            include = data.setdefault("scope", {}).setdefault(
                "mcp_servers", {}
            ).setdefault("include", [])
            if id in include:
                raise ValueError(
                    f"MCP server '{id}' already allowlisted in project."
                )
            include.append(id)
            try:
                ProjectConfig.model_validate(data)
            except Exception:
                raise ValueError("invalid project configuration") from None

        _mutate_yaml(cfg_path, allowlist_mutation, lock_required=False)
        logger.info("Allowlisted global MCP server '%s' in %s", id, cfg_path)
        return cfg_path

    def add_mutation(data: dict[str, Any]) -> None:
        registry_section = data.setdefault("registry", {})
        servers = registry_section.setdefault("mcp_servers", [])
        if any(s.get("id") == id for s in servers):
            where = "in project" if scope == "project" else ""
            raise ValueError(
                f"MCP server '{id}' already registered {where}. Remove it first."
            )
        servers.append(_server_entry(id, type, command, args, url, env, enabled))
        try:
            if scope == "project":
                ProjectConfig.model_validate(data)
            else:
                GlobalConfig.model_validate(data)
        except Exception:
            raise ValueError("invalid configuration document") from None

    _mutate_yaml(cfg_path, add_mutation, lock_required=False)
    logger.info("Registered MCP server '%s' (%s scope) in %s", id, scope, cfg_path)
    return cfg_path


# ---------------------------------------------------------------------------
# add-skill
# ---------------------------------------------------------------------------


def add_skill(
    source: Path,
    scope: str = "global",
    project_root: Path | None = None,
    symlink: bool = False,
) -> Path:
    """
    Register a skill bundle directory into the appropriate skills dir.

    If *symlink* is True, creates a symlink instead of copying (useful for
    skills still under active development).

    Returns the destination path.
    Raises FileNotFoundError if source doesn't have a SKILL.md.
    """
    source = source.resolve()
    if not (source / "SKILL.md").exists():
        raise FileNotFoundError(f"No SKILL.md found in {source}")

    config_path = _config_path(scope, project_root)
    with _MutationLock(config_path, required=False) as locked:
        identity_roots: tuple[Path, ...] = (_skills_dir(scope, project_root),)
        if scope == "global":
            data, _ = _read_profile_document(config_path, locked.directory_fd)
            try:
                config = GlobalConfig.model_validate(data)
            except Exception:
                raise ValueError("invalid configuration document") from None
            identity_roots = _effective_skill_roots(
                config, _skills_dir(scope, project_root)
            )
        return _add_skill_unlocked(
            source,
            scope,
            project_root,
            symlink,
            identity_roots=identity_roots,
        )


def _add_skill_unlocked(
    source: Path,
    scope: str,
    project_root: Path | None,
    symlink: bool,
    *,
    identity_roots: Iterable[Path] | None = None,
) -> Path:
    _validate_filesystem_skill_name(source.name)
    identity = _skill_manifest_identity(source)

    dest_dir = _skills_dir(scope, project_root)
    dest = dest_dir / source.name

    if dest.exists() or dest.is_symlink():
        raise FileExistsError(
            f"Skill '{source.name}' already exists at {dest}. Remove it first."
        )

    _assert_skill_identity_available(
        identity_roots or (dest_dir,), identity
    )

    dest_dir.mkdir(parents=True, exist_ok=True)

    if symlink:
        dest.symlink_to(source)
        logger.info("Symlinked skill '%s' → %s", source.name, dest)
    else:
        shutil.copytree(source, dest)
        logger.info("Copied skill '%s' → %s", source.name, dest)

    return dest


def _effective_skill_roots(
    config: GlobalConfig, destination: Path
) -> tuple[Path, ...]:
    roots: list[Path] = []
    seen: set[Path] = set()
    try:
        for root in (*config.skills_dirs, destination):
            canonical = root.expanduser().resolve(strict=False)
            if canonical not in seen:
                seen.add(canonical)
                roots.append(canonical)
    except OSError:
        raise ValueError("invalid skill registry") from None
    return tuple(roots)


def _assert_skill_identity_available(
    roots: Iterable[Path], identity: str
) -> None:
    """Reject ambiguous runtime identities before creating another binding."""

    try:
        for root in roots:
            if not root.exists():
                continue
            for bundle in root.iterdir():
                if not bundle.is_dir() or not (bundle / "SKILL.md").is_file():
                    continue
                try:
                    existing_identity = _skill_manifest_identity(bundle)
                except ValueError:
                    continue
                if existing_identity == identity:
                    raise ValueError("skill identity conflict")
    except ValueError:
        raise
    except OSError:
        raise ValueError("invalid skill registry") from None


# ---------------------------------------------------------------------------
# add-cli (CLI-Anything integration)
# ---------------------------------------------------------------------------


def add_cli(
    program: str,
    scope: str = "global",
    project_root: Path | None = None,
    cli_anything_args: list[str] | None = None,
) -> Path:
    """
    Wrap a traditional CLI program into a GearCore skill via CLI-Anything.

    Requires `cli-anything` on PATH (https://github.com/HKUDS/CLI-Anything).

    Workflow:
      1. Run `cli-anything generate <program>` to produce an interface spec
      2. Scaffold a skill bundle (SKILL.md + manifest.json) from the output
      3. Register the bundle via add_skill()

    Returns the final skill destination path.
    """
    if shutil.which("cli-anything") is None:
        raise RuntimeError(
            "cli-anything not found on PATH. "
            "Install it from https://github.com/HKUDS/CLI-Anything"
        )

    # --- Step 1: generate CLI interface via CLI-Anything ---
    cmd = ["cli-anything", "generate", program] + (cli_anything_args or [])
    logger.info("Running: %s", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0:
        raise RuntimeError(f"cli-anything failed for '{program}':\n{result.stderr}")

    # cli-anything is expected to produce JSON describing the interface
    try:
        cli_spec = json.loads(result.stdout)
    except json.JSONDecodeError:
        # Fallback: treat stdout as raw description text
        cli_spec = {"description": result.stdout.strip(), "commands": []}

    config_path = _config_path(scope, project_root)
    with _MutationLock(config_path, required=False) as locked:
        identity_roots: tuple[Path, ...] = (_skills_dir(scope, project_root),)
        if scope == "global":
            data, _ = _read_profile_document(config_path, locked.directory_fd)
            try:
                config = GlobalConfig.model_validate(data)
            except Exception:
                raise ValueError("invalid configuration document") from None
            identity_roots = _effective_skill_roots(
                config, _skills_dir(scope, project_root)
            )
        return _scaffold_cli_skill(
            program,
            scope,
            project_root,
            cli_spec,
            identity_roots=identity_roots,
        )


def _scaffold_cli_skill(
    program: str,
    scope: str,
    project_root: Path | None,
    cli_spec: dict[str, Any],
    *,
    identity_roots: Iterable[Path] | None = None,
) -> Path:
    """Write one CLI skill while the caller holds the registry lock."""

    skills_dir = _skills_dir(scope, project_root)
    skill_name = Path(program).name.replace(" ", "-").lower()
    _validate_filesystem_skill_name(skill_name)
    skill_path = skills_dir / skill_name

    if skill_path.exists():
        raise FileExistsError(
            f"Skill '{skill_name}' already exists at {skill_path}. Remove it first."
        )

    _assert_skill_identity_available(
        identity_roots or (skills_dir,), skill_name
    )

    skill_path.mkdir(parents=True, exist_ok=True)

    # manifest.json
    manifest = {
        "name": skill_name,
        "version": "1.0.0",
        "description": cli_spec.get("description", f"CLI wrapper for {program}"),
        "category": "cli",
        "mcp_servers": [],
        "activation": {
            "strategy": "manual",
            "triggers": [skill_name, program],
        },
    }
    with open(skill_path / "manifest.json", "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)

    # SKILL.md — generate from CLI-Anything spec
    commands_section = ""
    for cmd_spec in cli_spec.get("commands", []):
        name = cmd_spec.get("name", "")
        desc = cmd_spec.get("description", "")
        usage = cmd_spec.get("usage", "")
        commands_section += f"\n### `{name}`\n{desc}\n```\n{usage}\n```\n"

    skill_md = f"""---
name: {skill_name}
description: {manifest["description"]}
---

# {skill_name}

{manifest["description"]}

## Usage

Invoke via shell command: `{program}`

## Commands
{commands_section if commands_section else "_Run `" + program + " --help` for available commands._"}

## Notes

- Generated by CLI-Anything from `{program}`
- Adjust this SKILL.md to add workflow guidance specific to your use case
"""
    (skill_path / "SKILL.md").write_text(skill_md, encoding="utf-8")

    logger.info("Scaffolded CLI skill '%s' at %s", skill_name, skill_path)
    return skill_path


# ---------------------------------------------------------------------------
# remove
# ---------------------------------------------------------------------------


def remove_mcp(
    id: str, scope: str = "global", project_root: Path | None = None
) -> Path:
    """Remove an MCP server entry from config."""
    _validate_capability_id(id, "MCP")
    cfg_path = _config_path(scope, project_root)

    def remove_mutation(data: dict[str, Any]) -> None:
        if scope == "global":
            servers = data.get("registry", {}).get("mcp_servers", [])
            before = len(servers)
            data.setdefault("registry", {})["mcp_servers"] = [
                s for s in servers if s.get("id") != id
            ]
            if len(data["registry"]["mcp_servers"]) == before:
                raise KeyError(f"MCP server '{id}' not found in global config")
            try:
                validated_global = GlobalConfig.model_validate(data)
            except Exception:
                raise ValueError("invalid configuration document") from None
            if id in _globally_protected_ids(
                validated_global, "mcp_servers"
            ):
                raise ValueError("cannot remove a protected global MCP capability")
        else:
            removed = False
            servers = data.get("registry", {}).get("mcp_servers", [])
            remaining = [s for s in servers if s.get("id") != id]
            if len(remaining) != len(servers):
                data.setdefault("registry", {})["mcp_servers"] = remaining
                removed = True
            include = (
                data.get("scope", {}).get("mcp_servers", {}).get("include", [])
            )
            if id in include:
                include.remove(id)
                removed = True
            if not removed:
                raise KeyError(f"MCP server '{id}' not found in project config")
            try:
                ProjectConfig.model_validate(data)
            except Exception:
                raise ValueError("invalid configuration document") from None

    _mutate_yaml(cfg_path, remove_mutation, lock_required=False)
    logger.info("Removed MCP server '%s' from %s", id, cfg_path)
    return cfg_path


def remove_skill(
    name: str,
    scope: str = "global",
    project_root: Path | None = None,
) -> Path:
    """Delete a skill bundle directory from the skills dir."""
    _validate_filesystem_skill_name(name)
    config_path = _config_path(scope, project_root)
    with _MutationLock(config_path, required=False) as locked:
        dest = _resolve_skill_destination(name, scope, project_root)
        if scope == "global":
            data, _ = _read_profile_document(config_path, locked.directory_fd)
            try:
                config = GlobalConfig.model_validate(data)
            except Exception:
                raise ValueError("invalid configuration document") from None
            try:
                identity = _skill_manifest_identity(dest)
            except ValueError:
                identity = None
            if identity is not None:
                bindings = _count_skill_identity_bindings(
                    _effective_skill_roots(config, _skills_dir(scope, project_root)),
                    identity,
                )
                if bindings > 1:
                    raise ValueError("skill identity conflict")
            if identity in _protected_and_core_skill_ids(config):
                raise ValueError("cannot remove a protected or core global skill")
        return _remove_skill_unlocked(dest)


def _resolve_skill_destination(
    name: str,
    scope: str,
    project_root: Path | None,
) -> Path:
    dest_dir = _skills_dir(scope, project_root)
    direct = dest_dir / name
    candidates: list[Path] = []
    if direct.exists() or direct.is_symlink():
        candidates.append(direct)
    try:
        if dest_dir.exists():
            for bundle in dest_dir.iterdir():
                if bundle == direct or not bundle.is_dir():
                    continue
                try:
                    identity = _skill_manifest_identity(bundle)
                except ValueError:
                    continue
                if identity == name:
                    candidates.append(bundle)
    except OSError:
        raise ValueError("invalid skill registry") from None
    if len(candidates) > 1:
        raise ValueError("skill identity conflict")
    if not candidates:
        raise FileNotFoundError(f"Skill '{name}' not found")
    return candidates[0]


def _count_skill_identity_bindings(
    roots: Iterable[Path], identity: str
) -> int:
    count = 0
    try:
        for root in roots:
            if not root.exists():
                continue
            for bundle in root.iterdir():
                if not bundle.is_dir() or not (bundle / "SKILL.md").is_file():
                    continue
                try:
                    candidate = _skill_manifest_identity(bundle)
                except ValueError:
                    continue
                if candidate == identity:
                    count += 1
    except OSError:
        raise ValueError("invalid skill registry") from None
    return count


def _remove_skill_unlocked(dest: Path) -> Path:
    if dest.is_symlink():
        dest.unlink()
    else:
        shutil.rmtree(dest)
    logger.info("Removed skill '%s' from %s", dest.name, dest.parent)
    return dest.parent
