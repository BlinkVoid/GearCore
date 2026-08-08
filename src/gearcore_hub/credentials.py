"""Secret-safe file-backed credential references."""

from __future__ import annotations

import os
import stat
from pathlib import Path

from pydantic import SecretStr

_HAS_SECURE_FILE_API = (
    all(hasattr(os, name) for name in ("O_CLOEXEC", "O_DIRECTORY", "O_NOFOLLOW"))
    and hasattr(os, "getuid")
    and os.open in os.supports_dir_fd
    and os.stat in os.supports_dir_fd
    and os.stat in os.supports_follow_symlinks
)


class CredentialError(RuntimeError):
    """A credential could not be loaded without violating store policy."""


def validate_credential_id(credential_id: str) -> str:
    """Return a safe single-component credential identifier.

    Error messages deliberately omit the supplied value because a caller may
    have accidentally passed secret material instead of a reference.
    """

    if not isinstance(credential_id, str):
        raise CredentialError("invalid credential reference")
    if (
        not credential_id
        or credential_id != credential_id.strip()
        or credential_id in {".", ".."}
        or credential_id.startswith(".")
        or Path(credential_id).is_absolute()
        or "/" in credential_id
        or "\\" in credential_id
        or "\x00" in credential_id
    ):
        raise CredentialError("invalid credential reference")
    return credential_id


class CredentialStore:
    """Read owner-only credential files without following symlinks."""

    def __init__(self, root: Path | None = None):
        self.root = root or Path.home() / ".config" / "gearcore" / "credentials"

    @staticmethod
    def _validate_file(metadata: os.stat_result) -> None:
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_mode & 0o077
        ):
            raise CredentialError("unsafe credential file")

    def read(self, credential_id: str) -> SecretStr:
        """Load one credential into a redacting Pydantic secret wrapper."""

        safe_id = validate_credential_id(credential_id)
        if not _HAS_SECURE_FILE_API:
            raise CredentialError("secure credential access is unavailable")

        root_fd: int | None = None
        credential_fd: int | None = None
        try:
            root_flags = os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW
            root_fd = os.open(self.root, root_flags)
            before = os.stat(safe_id, dir_fd=root_fd, follow_symlinks=False)
            self._validate_file(before)

            file_flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
            credential_fd = os.open(safe_id, file_flags, dir_fd=root_fd)
            after = os.fstat(credential_fd)
            self._validate_file(after)
            if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
                raise CredentialError("credential changed during validation")

            with os.fdopen(credential_fd, "r", encoding="utf-8") as credential_file:
                credential_fd = None
                value = credential_file.read().strip()
        except CredentialError:
            raise
        except UnicodeError:
            raise CredentialError("credential encoding is invalid") from None
        except (OSError, TypeError, ValueError):
            raise CredentialError("credential unavailable") from None
        finally:
            if credential_fd is not None:
                os.close(credential_fd)
            if root_fd is not None:
                os.close(root_fd)

        if not value:
            raise CredentialError("empty credential")
        return SecretStr(value)
