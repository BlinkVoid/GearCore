# Vendor Superpowers Skills into GearCore — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move the superpowers skill bundle from the external Swarm workspace into the GearCore repository, ship it with the package, and add an `update-superpowers` command with lightweight upstream drift detection.

**Architecture:** Add a `third_party/superpowers/` directory at the repo root containing the vendored skill bundles and a `.vendor.json` manifest. Use hatchling `force-include` to ship the directory inside the installed package. A new `src/gearcore_hub/vendor.py` module provides path resolution, manifest parsing, and update logic. `config.py` appends the bundled skills directory to `skills_dirs` when missing. `main.py` adds the `update-superpowers` subcommand and prints vendored provenance in `status`.

**Tech Stack:** Python 3.13+, Pydantic 2.x, hatchling, git CLI, pytest.

## Global Constraints

- Vendored skills are read-only defaults; user-owned skills in `~/.config/gearcore/skills/` take precedence.
- No automatic updates; users must run `gearcore update-superpowers` explicitly.
- No git submodules or subtrees; plain directory copy with metadata.
- Network failures during update must leave the existing bundle untouched.
- Existing project configs and allowlists continue to work unchanged.

---

## File Structure

| Path | Responsibility |
|------|----------------|
| `third_party/superpowers/.vendor.json` | Provenance metadata for the vendored bundle. |
| `third_party/superpowers/README.md` | Attribution, license note, update instructions. |
| `third_party/superpowers/skills/<name>/` | Vendored skill bundles (`SKILL.md` + `manifest.json`). |
| `src/gearcore_hub/vendor.py` | Path resolution, manifest model, update logic, upstream detection. |
| `src/gearcore_hub/config.py` | Append bundled superpowers path to default `skills_dirs`. |
| `src/gearcore_hub/main.py` | Add `update-superpowers` CLI command; show provenance in `status`. |
| `pyproject.toml` | Map repo-root `third_party/` into the wheel via `force-include`. |
| `tests/test_vendor.py` | Unit tests for vendor module. |
| `tests/test_config.py` | Test that bundled path is included in effective skills dirs. |

---

### Task 1: Create vendor module with path resolution and manifest model

**Files:**
- Create: `src/gearcore_hub/vendor.py`
- Test: `tests/test_vendor.py`

**Interfaces:**
- Produces: `bundled_superpowers_dir() -> Path | None`
- Produces: `load_vendor_manifest() -> VendorManifest | None`
- Produces: `get_upstream_commit(source: str, ref: str) -> str | None`
- Produces: `sync_vendor_bundle(manifest, source_dir, dest_root, dry_run=False) -> dict`
- Produces: `update_superpowers(dry_run=False) -> dict`

- [ ] **Step 1: Write the failing test**

Create `tests/test_vendor.py`:

```python
from pathlib import Path
import json
import pytest

from gearcore_hub.vendor import (
    bundled_superpowers_dir,
    load_vendor_manifest,
    VendorManifest,
)


def test_bundled_superpowers_dir_returns_path_when_skills_exist(tmp_path, monkeypatch):
    fake_root = tmp_path / "third_party" / "superpowers"
    fake_root.mkdir(parents=True)
    (fake_root / "skills").mkdir()
    monkeypatch.setattr("gearcore_hub.vendor.VENDOR_ROOT", fake_root)
    assert bundled_superpowers_dir() == fake_root / "skills"


def test_bundled_superpowers_dir_returns_none_when_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "gearcore_hub.vendor.VENDOR_ROOT", tmp_path / "third_party" / "superpowers"
    )
    assert bundled_superpowers_dir() is None


def test_load_vendor_manifest_parses_json(tmp_path, monkeypatch):
    fake_root = tmp_path / "third_party" / "superpowers"
    fake_root.mkdir(parents=True)
    manifest = {
        "name": "superpowers",
        "source": "https://github.com/obra/superpowers.git",
        "source_ref": "main",
        "vendored_commit": "abc123",
        "vendored_at": "2026-07-05",
        "paths": ["skills/*"],
    }
    (fake_root / ".vendor.json").write_text(json.dumps(manifest))
    monkeypatch.setattr("gearcore_hub.vendor.VENDOR_ROOT", fake_root)
    result = load_vendor_manifest()
    assert isinstance(result, VendorManifest)
    assert result.vendored_commit == "abc123"


def test_load_vendor_manifest_returns_none_when_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "gearcore_hub.vendor.VENDOR_ROOT", tmp_path / "third_party" / "superpowers"
    )
    assert load_vendor_manifest() is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_vendor.py -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'gearcore_hub.vendor'`.

- [ ] **Step 3: Write minimal implementation**

Create `src/gearcore_hub/vendor.py`:

```python
"""Vendor bundle management for bundled skill dependencies."""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import tempfile
from datetime import date
from pathlib import Path

from pydantic import BaseModel

logger = logging.getLogger("gearcore.vendor")

VENDOR_ROOT = Path(__file__).parent / "third_party" / "superpowers"


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
    """Copy manifest.paths from source_dir to dest_root and update .vendor.json."""
    if dry_run:
        return {"changed": True, "dry_run": True}

    for pattern in manifest.paths:
        _copy_pattern(source_dir, pattern, dest_root)

    updated = manifest.model_copy(
        update={"vendored_commit": manifest.vendored_commit, "vendored_at": date.today().isoformat()}
    )
    (dest_root / ".vendor.json").write_text(
        updated.model_dump_json(indent=2) + "\n", encoding="utf-8"
    )
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
        )
        sync_vendor_bundle(
            manifest.model_copy(update={"vendored_commit": upstream}),
            clone_dir,
            VENDOR_ROOT,
        )

    return {"changed": True, "upstream": upstream}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_vendor.py -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/gearcore_hub/vendor.py tests/test_vendor.py
git commit -m "feat(vendor): add superpowers vendor bundle module"
```

---

### Task 2: Vendor the superpowers skill files and manifest

**Files:**
- Create: `third_party/superpowers/.vendor.json`
- Create: `third_party/superpowers/README.md`
- Create: `third_party/superpowers/skills/<name>/` (14 bundles)

**Interfaces:**
- Consumes: `VendorManifest` schema from Task 1.
- Produces: On-disk bundled skill tree ready for packaging.

- [ ] **Step 1: Copy skill bundles from Swarm**

Run:

```bash
mkdir -p third_party/superpowers/skills
cp -r third_party/superpowers/skills/* third_party/superpowers/skills/
```

- [ ] **Step 2: Write .vendor.json**

Create `third_party/superpowers/.vendor.json`:

```json
{
  "name": "superpowers",
  "source": "https://github.com/obra/superpowers.git",
  "source_ref": "main",
  "vendored_commit": "REPLACE_WITH_ACTUAL_SHA",
  "vendored_at": "2026-07-05",
  "paths": ["skills/*"]
}
```

Get the actual SHA:

```bash
cd third_party/superpowers
git rev-parse HEAD
```

Replace `REPLACE_WITH_ACTUAL_SHA` with the output.

- [ ] **Step 3: Write README.md**

Create `third_party/superpowers/README.md`:

```markdown
# Superpowers (vendored)

This directory contains a vendored copy of the
[superpowers](https://github.com/obra/superpowers) skill bundle.

It is shipped with GearCore so the foundational skills are available out of the
box without depending on an external workspace.

## Updating

```bash
gearcore update-superpowers
```

This clones the upstream repository and refreshes the bundled copy.

## License

See the upstream repository for license details.
```

- [ ] **Step 4: Verify the bundles**

Run:

```bash
ls third_party/superpowers/skills/
```

Expected directories:

```text
brainstorming
dispatching-parallel-agents
executing-plans
finishing-a-development-branch
receiving-code-review
requesting-code-review
subagent-driven-development
systematic-debugging
test-driven-development
using-git-worktrees
using-superpowers
verification-before-completion
writing-plans
writing-skills
```

- [ ] **Step 5: Commit**

```bash
git add third_party/
git commit -m "vendor(superpowers): bundle superpowers skills inside GearCore"
```

---

### Task 3: Configure hatchling to ship third_party in the wheel

**Files:**
- Modify: `pyproject.toml`

**Interfaces:**
- Produces: Wheel installs contain `gearcore_hub/third_party/superpowers/`.

- [ ] **Step 1: Write the failing test**

No automated test required; this is verified by building the wheel.

- [ ] **Step 2: Add force-include to pyproject.toml**

Modify `pyproject.toml` line 51-52 from:

```toml
[tool.hatch.build.targets.wheel]
packages = ["src/gearcore_hub"]
```

to:

```toml
[tool.hatch.build.targets.wheel]
packages = ["src/gearcore_hub"]
force-include = {"third_party" = "gearcore_hub/third_party"}
```

- [ ] **Step 3: Build wheel and verify contents**

Run:

```bash
uv build --wheel
unzip -l dist/*.whl | grep "gearcore_hub/third_party/superpowers/skills/using-superpowers/SKILL.md"
```

Expected: the SKILL.md path appears in the wheel listing.

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml
git commit -m "build: ship bundled third_party skills in wheel"
```

---

### Task 4: Include bundled superpowers path in default skills_dirs

**Files:**
- Modify: `src/gearcore_hub/config.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: `bundled_superpowers_dir()` from `gearcore_hub.vendor`.
- Produces: `GlobalConfig.skills_dirs` always includes the bundled path if it exists and is not already listed.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_config.py`:

```python
from gearcore_hub.vendor import bundled_superpowers_dir


def test_default_skills_dirs_include_bundled_superpowers(monkeypatch, tmp_path):
    fake_root = tmp_path / "third_party" / "superpowers"
    (fake_root / "skills").mkdir(parents=True)
    monkeypatch.setattr("gearcore_hub.vendor.VENDOR_ROOT", fake_root)
    cfg = GlobalConfig()
    assert bundled_superpowers_dir() in cfg.skills_dirs
```

Wait, `GlobalConfig` is already imported in `tests/test_config.py`. Verify the import exists; if not, add it.

Run: `pytest tests/test_config.py::test_default_skills_dirs_include_bundled_superpowers -v`

Expected: FAIL because `GlobalConfig.skills_dirs` does not yet include the bundled path.

- [ ] **Step 2: Implement default skills_dirs logic**

Modify `src/gearcore_hub/config.py`:

Add import near the top:

```python
from gearcore_hub.vendor import bundled_superpowers_dir
```

Add helper before `GlobalConfig`:

```python
_DEFAULT_SKILLS_DIRS = [
    Path.home() / ".config" / "gearcore" / "skills",
    Path.home() / ".config" / "agents" / "skills",
]


def _default_skills_dirs() -> list[Path]:
    dirs = list(_DEFAULT_SKILLS_DIRS)
    bundled = bundled_superpowers_dir()
    if bundled is not None and bundled not in dirs:
        dirs.append(bundled)
    return dirs
```

Modify `GlobalConfig.skills_dirs` property:

```python
    @property
    def skills_dirs(self) -> list[Path]:
        raw = self.registry.get("skills_dirs", [])
        dirs: list[Path] = [Path(os.path.expanduser(str(p))) for p in raw]
        if not dirs:
            dirs = _default_skills_dirs()
        bundled = bundled_superpowers_dir()
        if bundled is not None and bundled not in dirs:
            dirs.append(bundled)
        return dirs
```

- [ ] **Step 3: Run test to verify it passes**

Run: `pytest tests/test_config.py::test_default_skills_dirs_include_bundled_superpowers -v`

Expected: PASS.

- [ ] **Step 4: Run existing config tests**

Run: `pytest tests/test_config.py -v`

Expected: All PASS.

- [ ] **Step 5: Commit**

```bash
git add src/gearcore_hub/config.py tests/test_config.py
git commit -m "feat(config): include bundled superpowers skills in default skills_dirs"
```

---

### Task 5: Add `update-superpowers` CLI command

**Files:**
- Modify: `src/gearcore_hub/main.py`
- Test: `tests/test_cli_parser.py` (or `tests/test_main.py` if it exists)

**Interfaces:**
- Consumes: `update_superpowers(dry_run=False)` from `gearcore_hub.vendor`.
- Produces: New subcommand `gearcore update-superpowers [--dry-run]`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_cli_parser.py` (or create `tests/test_main.py`):

```python
def test_update_superpowers_parser():
    parser = build_parser()
    args = parser.parse_args(["update-superpowers"])
    assert args.command == "update-superpowers"
    assert args.dry_run is False

    args = parser.parse_args(["update-superpowers", "--dry-run"])
    assert args.dry_run is True
```

Run: `pytest tests/test_cli_parser.py::test_update_superpowers_parser -v`

Expected: FAIL because the subparser does not exist.

- [ ] **Step 2: Add subparser and handler**

In `src/gearcore_hub/main.py`, in `build_parser()` after the sync parser block (around line 508), add:

```python
    # update-superpowers
    p_update_sp = sub.add_parser(
        "update-superpowers",
        help="Update the bundled superpowers skills from upstream",
    )
    p_update_sp.add_argument(
        "--dry-run",
        action="store_true",
        help="Show whether an update is available without writing files",
    )
```

In `main()`, after the sync handler block (around line 630), add:

```python
    if command == "update-superpowers":
        from gearcore_hub.vendor import update_superpowers

        try:
            result = update_superpowers(dry_run=args.dry_run)
        except RuntimeError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            sys.exit(1)

        if result.get("changed"):
            upstream = result["upstream"]
            if result.get("dry_run"):
                print(
                    f"Update available: superpowers {upstream[:12]} "
                    "(run without --dry-run to apply)"
                )
            else:
                print(f"Updated superpowers to {upstream[:12]}")
        else:
            print(f"superpowers is up to date ({result['upstream'][:12]})")
        return
```

- [ ] **Step 3: Run test to verify it passes**

Run: `pytest tests/test_cli_parser.py::test_update_superpowers_parser -v`

Expected: PASS.

- [ ] **Step 4: Run all parser tests**

Run: `pytest tests/test_cli_parser.py -v`

Expected: All PASS.

- [ ] **Step 5: Commit**

```bash
git add src/gearcore_hub/main.py tests/test_cli_parser.py
git commit -m "feat(cli): add update-superpowers command"
```

---

### Task 6: Show vendored superpowers provenance in `status`

**Files:**
- Modify: `src/gearcore_hub/main.py`

**Interfaces:**
- Consumes: `load_vendor_manifest()`, `get_upstream_commit()` from `gearcore_hub.vendor`.
- Produces: `gearcore status` prints vendored commit and update hint.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_cli_parser.py` (or create `tests/test_main.py`):

```python
from unittest.mock import patch, MagicMock


def test_status_prints_vendor_manifest(capsys):
    manifest = MagicMock()
    manifest.vendored_commit = "abcdef1234567890"
    manifest.vendored_at = "2026-07-05"
    manifest.source = "https://github.com/obra/superpowers.git"
    manifest.source_ref = "main"

    config = load_config(global_config_path=Path("/nonexistent"))

    with patch("gearcore_hub.main.load_vendor_manifest", return_value=manifest), \
         patch("gearcore_hub.main.get_upstream_commit", return_value="abcdef1234567890"):
        cmd_status(config)

    captured = capsys.readouterr()
    assert "superpowers" in captured.out
    assert "abcdef1234567890"[:12] in captured.out
```

Run: `pytest tests/test_cli_parser.py::test_status_prints_vendor_manifest -v`

Expected: FAIL because `cmd_status` does not yet print vendor info.

- [ ] **Step 2: Implement status output**

Modify `src/gearcore_hub/main.py` `cmd_status()`:

After the disclosure block (before the final `print()`), add:

```python
    from gearcore_hub.vendor import load_vendor_manifest, get_upstream_commit

    manifest = load_vendor_manifest()
    if manifest:
        print("\nVendored skills:")
        print(f"  superpowers @ {manifest.vendored_commit[:12]} ({manifest.vendored_at})")
        upstream = get_upstream_commit(manifest.source, manifest.source_ref)
        if upstream and upstream != manifest.vendored_commit:
            print(
                f"  update available: {upstream[:12]} "
                "(run 'gearcore update-superpowers' to refresh)"
            )
```

- [ ] **Step 3: Run test to verify it passes**

Run: `pytest tests/test_cli_parser.py::test_status_prints_vendor_manifest -v`

Expected: PASS.

- [ ] **Step 4: Run all tests**

Run: `pytest tests/ -v`

Expected: All PASS.

- [ ] **Step 5: Commit**

```bash
git add src/gearcore_hub/main.py tests/test_cli_parser.py
git commit -m "feat(status): show vendored superpowers provenance and update hint"
```

---

### Task 7: Add unit test for update logic with mocked source

**Files:**
- Modify: `tests/test_vendor.py`

**Interfaces:**
- Consumes: `sync_vendor_bundle()` from Task 1.
- Produces: Test verifying copies update skills and rewrites `.vendor.json`.

- [ ] **Step 1: Write the test**

Add to `tests/test_vendor.py`:

```python
from gearcore_hub.vendor import sync_vendor_bundle


def test_sync_vendor_bundle_copies_skills_and_updates_manifest(tmp_path):
    source_dir = tmp_path / "source"
    skills_dir = source_dir / "skills"
    skills_dir.mkdir(parents=True)
    (skills_dir / "using-superpowers").mkdir()
    (skills_dir / "using-superpowers" / "SKILL.md").write_text("# Using Superpowers")
    (skills_dir / "using-superpowers" / "manifest.json").write_text(
        '{"name": "using-superpowers"}'
    )

    dest_root = tmp_path / "dest"
    dest_root.mkdir()
    manifest = VendorManifest(
        name="superpowers",
        source="https://example.com/superpowers.git",
        source_ref="main",
        vendored_commit="old123",
        vendored_at="2026-01-01",
        paths=["skills/*"],
    )

    sync_vendor_bundle(
        manifest.model_copy(update={"vendored_commit": "new456"}),
        source_dir,
        dest_root,
    )

    assert (dest_root / "skills" / "using-superpowers" / "SKILL.md").exists()
    updated = json.loads((dest_root / ".vendor.json").read_text())
    assert updated["vendored_commit"] == "new456"
    assert updated["vendored_at"] != "2026-01-01"
```

- [ ] **Step 2: Run test to verify it passes**

Run: `pytest tests/test_vendor.py::test_sync_vendor_bundle_copies_skills_and_updates_manifest -v`

Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/test_vendor.py
git commit -m "test(vendor): cover sync_vendor_bundle with mocked source"
```

---

### Task 8: Manual migration on the current machine

**Files:**
- (No repo changes — local environment cleanup.)

**Interfaces:**
- Produces: Old Swarm symlinks removed; GearCore uses bundled copy.

- [ ] **Step 1: Remove stale Swarm symlinks**

Run:

```bash
cd ~/.config/gearcore/skills
for name in brainstorming dispatching-parallel-agents executing-plans finishing-a-development-branch receiving-code-review requesting-code-review subagent-driven-development systematic-debugging test-driven-development using-git-worktrees using-superpowers verification-before-completion writing-plans writing-skills; do
  if [ -L "$name" ] && readlink "$name" | grep -q "/Swarm/"; then
    rm "$name"
  fi
done
```

- [ ] **Step 2: Reinstall GearCore and verify**

Run:

```bash
uv tool install --reinstall ~/workspace/GearCore
gearcore status
gearcore list-skills | grep using-superpowers
```

Expected: `gearcore status` shows the vendored superpowers commit. `gearcore list-skills` lists `using-superpowers` without broken symlink warnings.

- [ ] **Step 3: Test update-superpowers --dry-run**

Run:

```bash
gearcore update-superpowers --dry-run
```

Expected: Either "superpowers is up to date" or "Update available" with no filesystem changes.

---

## Self-Review

**Spec coverage:**
- Vendor into `third_party/superpowers/skills/` → Task 2.
- `.vendor.json` manifest → Task 2.
- Default config integration → Task 4.
- `update-superpowers` command → Task 5.
- Status integration → Task 6.
- Update detection → Tasks 1 and 6.
- Conflict resolution / user precedence → Task 4 appends bundled path; existing scan order preserves user precedence.
- Testing → Tasks 1, 4, 5, 6, 7.
- Migration → Task 8.

**Placeholder scan:** All steps include exact file paths, code blocks, and commands. No TBD/TODO placeholders.

**Type consistency:** `VendorManifest` fields match `.vendor.json` and usages in `update_superpowers`, `sync_vendor_bundle`, `cmd_status`.
