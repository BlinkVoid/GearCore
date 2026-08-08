"""Small overlap-safe logging controls used by sanitized inspection paths."""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Iterator


class _DropRecord(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return False


@contextlib.contextmanager
def silence_logger(name: str) -> Iterator[None]:
    """Drop records from one logger without changing its global enabled state.

    Each caller installs its own filter, so nested and overlapping contexts do
    not re-enable logging while another caller still requires redaction.
    """

    selected = logging.getLogger(name)
    drop = _DropRecord()
    selected.addFilter(drop)
    try:
        yield
    finally:
        selected.removeFilter(drop)
