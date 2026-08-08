"""Security tests for file-backed MCP credential references."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest
from pydantic import SecretStr

from gearcore_hub.credentials import CredentialError, CredentialStore

SENTINEL = "sentinel-credential-value-149"


def _credential_file(root: Path, name: str = "dispatcher") -> Path:
    root.mkdir(mode=0o700)
    path = root / name
    path.write_text(f"{SENTINEL}\n", encoding="utf-8")
    path.chmod(0o600)
    return path


def test_reads_regular_owner_only_credential_as_secret(tmp_path: Path):
    root = tmp_path / "credentials"
    _credential_file(root)

    secret = CredentialStore(root).read("dispatcher")

    assert isinstance(secret, SecretStr)
    assert secret.get_secret_value() == SENTINEL
    assert SENTINEL not in str(secret)
    assert SENTINEL not in repr(secret)


@pytest.mark.parametrize(
    "credential_id",
    [
        "",
        "   ",
        ".",
        "..",
        ".hidden",
        "../dispatcher",
        "nested/dispatcher",
        r"nested\dispatcher",
        "/absolute/dispatcher",
    ],
)
def test_rejects_separator_traversal_absolute_and_dot_names(
    tmp_path: Path, credential_id: str
):
    root = tmp_path / "credentials"
    _credential_file(root)

    with pytest.raises(CredentialError) as exc_info:
        CredentialStore(root).read(credential_id)

    message = str(exc_info.value)
    assert credential_id not in message or not credential_id
    assert str(root) not in message
    assert SENTINEL not in message


def test_rejects_symlink_without_reading_target(tmp_path: Path):
    root = tmp_path / "credentials"
    root.mkdir(mode=0o700)
    target = tmp_path / "target"
    target.write_text(SENTINEL, encoding="utf-8")
    target.chmod(0o600)
    (root / "dispatcher").symlink_to(target)

    with pytest.raises(CredentialError) as exc_info:
        CredentialStore(root).read("dispatcher")

    assert SENTINEL not in str(exc_info.value)
    assert str(target) not in str(exc_info.value)


def test_rejects_directory(tmp_path: Path):
    root = tmp_path / "credentials"
    root.mkdir(mode=0o700)
    (root / "dispatcher").mkdir(mode=0o700)

    with pytest.raises(CredentialError, match="unsafe credential file"):
        CredentialStore(root).read("dispatcher")


def test_rejects_foreign_owner_safely_simulated(tmp_path: Path, monkeypatch):
    root = tmp_path / "credentials"
    _credential_file(root)
    real_fstat = os.fstat
    credential_metadata = os.stat(root / "dispatcher")

    def foreign_file_fstat(fd: int):
        metadata = real_fstat(fd)
        if stat.S_ISREG(metadata.st_mode):
            values = list(metadata)
            values[4] = credential_metadata.st_uid + 1
            return os.stat_result(values)
        return metadata

    monkeypatch.setattr(os, "fstat", foreign_file_fstat)

    with pytest.raises(CredentialError, match="unsafe credential file"):
        CredentialStore(root).read("dispatcher")


def test_rejects_foreign_owned_credential_root_safely_simulated(
    tmp_path: Path, monkeypatch
):
    root = tmp_path / "credentials"
    _credential_file(root)
    real_fstat = os.fstat
    root_metadata = os.stat(root)

    def foreign_root_fstat(fd: int):
        metadata = real_fstat(fd)
        if stat.S_ISDIR(metadata.st_mode):
            values = list(metadata)
            values[4] = root_metadata.st_uid + 1
            return os.stat_result(values)
        return metadata

    monkeypatch.setattr(os, "fstat", foreign_root_fstat)

    with pytest.raises(CredentialError, match="unsafe credential root"):
        CredentialStore(root).read("dispatcher")


@pytest.mark.parametrize("mode", [0o720, 0o707, 0o777])
def test_rejects_group_or_other_writable_credential_root(tmp_path: Path, mode: int):
    root = tmp_path / "credentials"
    _credential_file(root)
    root.chmod(mode)

    with pytest.raises(CredentialError, match="unsafe credential root"):
        CredentialStore(root).read("dispatcher")


@pytest.mark.parametrize("mode", [0o640, 0o604, 0o666])
def test_rejects_group_or_other_permissions(tmp_path: Path, mode: int):
    root = tmp_path / "credentials"
    path = _credential_file(root)
    path.chmod(mode)

    with pytest.raises(CredentialError, match="unsafe credential file"):
        CredentialStore(root).read("dispatcher")


def test_rejects_missing_credential_without_path_leakage(tmp_path: Path):
    root = tmp_path / "secret-credential-root"
    root.mkdir(mode=0o700)

    with pytest.raises(CredentialError) as exc_info:
        CredentialStore(root).read("secret-missing-id")

    message = str(exc_info.value)
    assert "secret-missing-id" not in message
    assert "secret-credential-root" not in message


@pytest.mark.parametrize("contents", ["", "\n", " \t\r\n"])
def test_rejects_empty_or_blank_credential(tmp_path: Path, contents: str):
    root = tmp_path / "credentials"
    root.mkdir(mode=0o700)
    path = root / "dispatcher"
    path.write_text(contents, encoding="utf-8")
    path.chmod(0o600)

    with pytest.raises(CredentialError, match="empty credential") as exc_info:
        CredentialStore(root).read("dispatcher")

    assert contents not in str(exc_info.value) or not contents


def test_rejects_file_replaced_between_validation_and_open(tmp_path: Path, monkeypatch):
    root = tmp_path / "credentials"
    original_path = _credential_file(root)
    replacement = tmp_path / "replacement"
    replacement.write_text("replacement-secret", encoding="utf-8")
    replacement.chmod(0o600)
    real_open = os.open
    replaced = False

    def replacing_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal replaced
        if path == "dispatcher" and dir_fd is not None and not replaced:
            replaced = True
            os.replace(replacement, original_path)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", replacing_open)

    with pytest.raises(CredentialError, match="changed during validation") as exc_info:
        CredentialStore(root).read("dispatcher")

    assert "replacement-secret" not in str(exc_info.value)


def test_fifo_replacement_is_opened_nonblocking_then_rejected(
    tmp_path: Path, monkeypatch
):
    root = tmp_path / "credentials"
    original_path = _credential_file(root)
    fifo = tmp_path / "replacement-fifo"
    os.mkfifo(fifo, mode=0o600)
    real_open = os.open
    replaced = False

    def replacing_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal replaced
        if path == "dispatcher" and dir_fd is not None and not replaced:
            replaced = True
            os.replace(fifo, original_path)
            if not flags & os.O_NONBLOCK:
                raise AssertionError("credential FIFO was opened in blocking mode")
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", replacing_open)

    with pytest.raises(CredentialError, match="unsafe credential file"):
        CredentialStore(root).read("dispatcher")


def test_default_root_is_user_gearcore_credential_directory():
    store = CredentialStore()

    assert store.root == Path.home() / ".config" / "gearcore" / "credentials"
