"""Vendor bundle management for bundled skill dependencies."""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
import time
from datetime import date
from pathlib import Path

from pydantic import BaseModel

logger = logging.getLogger("gearcore.vendor")

VENDOR_ROOT = Path(__file__).parent / "third_party" / "superpowers"

CACHE_TTL_SECONDS = 600.0


class VendorManifest(BaseModel):
    name: str
    source: str
    source_ref: str
    vendored_commit: str
    vendored_at: str
    paths: list[str]


def bundled_superpowers_dir() -> Path | None:
    """Return the bundled superpowers skills directory, or None if absent."""
    p = VENDOR_ROOT / "skills"
    return p if p.exists() else None


def load_vendor_manifest() -> VendorManifest | None:
    """Parse .vendor.json from the bundled superpowers directory."""
    p = VENDOR_ROOT / ".vendor.json"
    if not p.exists():
        return None
    try:
        return VendorManifest(**json.loads(p.read_text(encoding="utf-8")))
    except Exception as exc:
        logger.error("Failed to parse vendor manifest at %s: %s", p, exc)
        return None


def get_upstream_commit(source: str, ref: str) -> str | None:
    """Return the commit SHA for *ref* in *source* via git ls-remote, or None."""
    try:
        result = subprocess.run(
            ["git", "ls-remote", source, ref],
            capture_output=True,
            text=True,
            check=True,
            timeout=30.0,
        )
        lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        if lines:
            return lines[0].split()[0]
    except Exception as exc:
        logger.debug("git ls-remote failed for %s %s: %s", source, ref, exc)
    return None


def _cache_path() -> Path:
    cache_root = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    return cache_root / "gearcore" / "ls-remote.json"


def get_upstream_commit_cached(
    source: str, ref: str, *, ttl: float = CACHE_TTL_SECONDS
) -> str | None:
    """Like get_upstream_commit, but caches successful lookups for *ttl* seconds.

    Avoids a network round-trip on every `gearcore status` invocation.
    Failed lookups are not cached so transient network issues retry next call.
    """
    path = _cache_path()
    key = f"{source}#{ref}"
    data: dict = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except Exception as exc:
            logger.debug("Ignoring unreadable ls-remote cache: %s", exc)

    entry = data.get(key)
    if isinstance(entry, dict) and time.time() - entry.get("ts", 0) < ttl:
        sha = entry.get("sha")
        return sha if isinstance(sha, str) else None

    sha = get_upstream_commit(source, ref)
    if sha is None:
        return None

    data[key] = {"sha": sha, "ts": time.time()}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")
    except OSError as exc:
        logger.debug("Could not write ls-remote cache: %s", exc)
    return sha


def _copy_pattern(source_dir: Path, pattern: str, dest_root: Path) -> None:
    """Copy files/directories matching *pattern* from *source_dir* into *dest_root*."""
    if "*" in pattern:
        for item in source_dir.glob(pattern):
            rel = item.relative_to(source_dir)
            target = dest_root / rel
            if item.is_dir():
                if target.exists():
                    shutil.rmtree(target)
                shutil.copytree(item, target)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(item, target)
    else:
        src = source_dir / pattern
        target = dest_root / pattern
        if src.is_dir():
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(src, target)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, target)


def sync_vendor_bundle(
    manifest: VendorManifest,
    source_dir: Path,
    dest_root: Path,
    *,
    dry_run: bool = False,
) -> dict:
    """Copy manifest.paths from source_dir to dest_root and update .vendor.json.

    Writes to a temporary sibling directory first and atomically renames the
    result so a failed copy never leaves *dest_root* partially updated.
    """
    if dry_run:
        return {"changed": True, "dry_run": True}

    # Build the new tree beside the destination so we can swap atomically.
    tmp_dest = dest_root.with_name(dest_root.name + ".tmp")
    if tmp_dest.exists():
        shutil.rmtree(tmp_dest)
    if dest_root.exists():
        shutil.copytree(dest_root, tmp_dest, ignore_dangling_symlinks=True)
    else:
        tmp_dest.mkdir(parents=True)

    try:
        for pattern in manifest.paths:
            _copy_pattern(source_dir, pattern, tmp_dest)

        updated = manifest.model_copy(
            update={
                "vendored_commit": manifest.vendored_commit,
                "vendored_at": date.today().isoformat(),
            }
        )
        (tmp_dest / ".vendor.json").write_text(
            updated.model_dump_json(indent=2) + "\n", encoding="utf-8"
        )

        backup = dest_root.with_name(dest_root.name + ".bak")
        if backup.exists():
            shutil.rmtree(backup)
        if dest_root.exists():
            dest_root.rename(backup)
        try:
            tmp_dest.rename(dest_root)
        except Exception:
            if backup.exists() and not dest_root.exists():
                backup.rename(dest_root)
            raise
        if backup.exists():
            shutil.rmtree(backup)
    except Exception:
        if tmp_dest.exists():
            shutil.rmtree(tmp_dest)
        raise

    return {"changed": True}


def update_superpowers(*, dry_run: bool = False) -> dict:
    """Refresh the bundled superpowers skills from upstream."""
    manifest = load_vendor_manifest()
    if manifest is None:
        raise RuntimeError("No superpowers vendor manifest found.")

    upstream = get_upstream_commit(manifest.source, manifest.source_ref)
    if upstream is None:
        raise RuntimeError(
            f"Could not reach upstream {manifest.source} ({manifest.source_ref})."
        )

    if upstream == manifest.vendored_commit:
        return {"changed": False, "upstream": upstream}

    if dry_run:
        return {"changed": True, "upstream": upstream, "dry_run": True}

    with tempfile.TemporaryDirectory() as tmp:
        clone_dir = Path(tmp) / "superpowers"
        subprocess.run(
            [
                "git",
                "clone",
                "--depth",
                "1",
                "--branch",
                manifest.source_ref,
                manifest.source,
                str(clone_dir),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=120.0,
        )
        sync_vendor_bundle(
            manifest.model_copy(update={"vendored_commit": upstream}),
            clone_dir,
            VENDOR_ROOT,
        )

    return {"changed": True, "upstream": upstream}
