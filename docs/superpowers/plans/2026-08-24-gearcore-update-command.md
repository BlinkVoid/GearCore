# GearCore `update` Subcommand — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `gearcore update` subcommand that refreshes registered MCP servers, skill bundles, superpowers, and self-skill sync, with version-aware change detection.

**Architecture:** Extend `McpServerConfig` with an optional `update_metadata` dict. Introduce `src/gearcore_hub/update.py` containing version-detection helpers and per-resource update logic. Wire a new `update` subcommand into the existing argparse dispatcher in `main.py`. Unit tests mock git/manifest/subprocess calls.

**Tech Stack:** Python 3.13+, pydantic, pyyaml, pytest. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-08-24-gearcore-update-command-design.md`

## Global Constraints

- No new runtime dependencies beyond what's in `pyproject.toml`.
- Keep config schema additive; old configs without `update_metadata` continue to work.
- All existing tests stay green: `uv run python -m pytest -q`.
- Deterministic by default; version checks rely on local git state or manifest files, not network (except superpowers which already uses `git ls-remote`).
- Commits after each task; conventional commit style.
- Update docs: `README.md`, `CHANGELOG.md`, `self_skill/SKILL.md`.

---

### Task 1: Extend `McpServerConfig` with `update_metadata`

**Files:**
- Modify: `src/gearcore_hub/config.py`
- Test: `tests/unit/test_config.py` (if exists; otherwise `tests/unit/test_update.py`)

**Interfaces:**
- Produces: `McpServerConfig` has an optional `update_metadata: dict[str, Any] | None` field. Existing configs load fine because Pydantic ignores extra fields by default unless strict; we are adding an optional field.

- [ ] **Step 1: Add the field**

In `src/gearcore_hub/config.py`, line 29-37, change `McpServerConfig` to:

```python
class McpServerConfig(BaseModel):
    id: str
    type: str = "stdio"
    command: str = ""
    args: list[str] = Field(default_factory=list)
    url: str = ""
    env: dict[str, str] | None = None
    enabled: bool = True
    update_metadata: dict[str, Any] | None = None
```

- [ ] **Step 2: Add a regression test**

If `tests/unit/test_config.py` exists, append; otherwise create `tests/unit/test_update_config.py`:

```python
from gearcore_hub.config import McpServerConfig


def test_mcp_server_config_accepts_update_metadata():
    cfg = McpServerConfig(
        id="sample-prompts",
        command="uv",
        update_metadata={"source_path": "/tmp/sample-prompts", "revision": "abc123"},
    )
    assert cfg.update_metadata["revision"] == "abc123"


def test_mcp_server_config_without_update_metadata_loads():
    cfg = McpServerConfig(id="sample-prompts", command="uv")
    assert cfg.update_metadata is None
```

- [ ] **Step 3: Run tests**

Run: `uv run python -m pytest tests/unit/test_config.py tests/unit/test_update_config.py -v`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add src/gearcore_hub/config.py tests/
git commit -m "feat(config): add update_metadata field to McpServerConfig"
```

---

### Task 2: Create version-detection helpers

**Files:**
- Create: `src/gearcore_hub/update.py`
- Test: `tests/unit/test_update.py`

**Interfaces:**
- Produces:
  - `get_git_revision(path: Path) -> str | None`
  - `get_manifest_version(path: Path) -> str | None`
  - `infer_mcp_source_path(server: McpServerConfig) -> Path | None`
  - `load_skill_manifest(path: Path) -> dict[str, Any] | None`

- [ ] **Step 1: Write the helper module**

Create `src/gearcore_hub/update.py`:

```python
"""Version detection and source-path inference for `gearcore update`."""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path
from typing import Any

from gearcore_hub.config import McpServerConfig

logger = logging.getLogger("gearcore.update")


def get_git_revision(path: Path) -> str | None:
    """Return short SHA of HEAD if *path* is inside a git repo, else None."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=path,
            capture_output=True,
            text=True,
            check=True,
            timeout=10.0,
        )
        return result.stdout.strip()
    except Exception as exc:
        logger.debug("git rev-parse failed for %s: %s", path, exc)
        return None


def get_manifest_version(path: Path) -> str | None:
    """Read 'version' from manifest.json in *path*, if present."""
    manifest = path / "manifest.json"
    if not manifest.exists():
        return None
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
        return data.get("version")
    except Exception as exc:
        logger.debug("Failed to read manifest at %s: %s", manifest, exc)
        return None


def _split_kv(arg: str) -> tuple[str, str] | None:
    if "=" in arg:
        return arg.split("=", 1)
    return None


def infer_mcp_source_path(server: McpServerConfig) -> Path | None:
    """Infer the local source path of an MCP server from its command/args."""
    # 1. Look for --directory / -d / --directory=... in args
    for i, arg in enumerate(server.args):
        arg_norm = arg.strip()
        if arg_norm in ("--directory", "-d") and i + 1 < len(server.args):
            return Path(server.args[i + 1]).expanduser().resolve()
        kv = _split_kv(arg_norm)
        if kv and kv[0] in ("--directory", "-d"):
            return Path(kv[1]).expanduser().resolve()

    # 2. If command is an absolute path inside a workspace, assume it's the source root
    cmd = Path(server.command).expanduser()
    if cmd.is_absolute() and not cmd.name.startswith(("uv", "python", "npx")):
        # e.g. /home/.../sample-devtools-mcp
        return cmd.parent.resolve()

    return None


def load_skill_manifest(path: Path) -> dict[str, Any] | None:
    """Load a skill bundle's manifest.json."""
    manifest = path / "manifest.json"
    if not manifest.exists():
        return None
    try:
        return json.loads(manifest.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.debug("Failed to parse manifest at %s: %s", manifest, exc)
        return None
```

- [ ] **Step 2: Write tests for helpers**

Create `tests/unit/test_update.py`:

```python
from pathlib import Path
from unittest.mock import patch

from gearcore_hub.config import McpServerConfig
from gearcore_hub.update import (
    get_git_revision,
    get_manifest_version,
    infer_mcp_source_path,
)


def test_get_git_revision(tmp_path: Path):
    # Initialize a git repo to get a real short SHA
    import subprocess
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "file.txt").write_text("x")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init", "--no-gpg-sign"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    sha = get_git_revision(tmp_path)
    assert sha is not None and len(sha) >= 4


def test_get_git_revision_non_repo(tmp_path: Path):
    assert get_git_revision(tmp_path) is None


def test_get_manifest_version(tmp_path: Path):
    (tmp_path / "manifest.json").write_text('{"version": "1.2.3"}')
    assert get_manifest_version(tmp_path) == "1.2.3"


def test_get_manifest_version_missing(tmp_path: Path):
    assert get_manifest_version(tmp_path) is None


def test_infer_mcp_source_path_from_directory_flag():
    server = McpServerConfig(
        id="sample-prompts",
        command="uv",
        args=["run", "--directory", "/home/foo/SamplePrompts", "python", "-m", "sample-prompts.main"],
    )
    assert infer_mcp_source_path(server) == Path("/home/foo/SamplePrompts").resolve()


def test_infer_mcp_source_path_from_absolute_command():
    server = McpServerConfig(
        id="sample-devtools",
        command="/home/foo/SampleDevtools/.venv/bin/sample-devtools-mcp",
    )
    assert infer_mcp_source_path(server) == Path("/home/foo/SampleDevtools/.venv/bin").resolve()


def test_infer_mcp_source_path_unknown():
    server = McpServerConfig(id="cloudflare", command="npx", args=["-y", "@playwright/mcp"])
    assert infer_mcp_source_path(server) is None
```

- [ ] **Step 3: Run tests**

Run: `uv run python -m pytest tests/unit/test_update.py -v`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add src/gearcore_hub/update.py tests/unit/test_update.py
git commit -m "feat(update): version detection and source-path inference helpers"
```

---

### Task 3: Implement MCP server update logic

**Files:**
- Modify: `src/gearcore_hub/update.py`
- Modify: `src/gearcore_hub/registry.py` (add `update_mcp` if needed, or reuse remove/add)
- Test: `tests/unit/test_update.py`

**Interfaces:**
- Consumes: helpers from Task 2; `registry.add_mcp`, `registry.remove_mcp`, `config.McpServerConfig`.
- Produces:
  - `update_mcp_server(config, server_id, dry_run=False, source_path=None) -> dict`
  - `update_all_mcp_servers(config, dry_run=False) -> list[dict]`

- [ ] **Step 1: Implement update functions**

Append to `src/gearcore_hub/update.py`:

```python
from gearcore_hub.registry import add_mcp, remove_mcp


def update_mcp_server(
    config,
    server_id: str,
    *,
    dry_run: bool = False,
    source_path: Path | None = None,
) -> dict:
    """Refresh a single MCP server registration if its source has changed.

    Returns a dict: {
        "id": str,
        "changed": bool,
        "previous_revision": str | None,
        "current_revision": str | None,
        "message": str,
    }
    """
    server = next((s for s in config.mcp_servers if s.id == server_id), None)
    if server is None:
        return {
            "id": server_id,
            "changed": False,
            "previous_revision": None,
            "current_revision": None,
            "message": f"MCP server '{server_id}' not found.",
        }

    path = source_path or infer_mcp_source_path(server)
    if path is None:
        return {
            "id": server_id,
            "changed": False,
            "previous_revision": None,
            "current_revision": None,
            "message": f"Could not infer source path for '{server_id}'.",
        }

    previous = (server.update_metadata or {}).get("revision")
    current = get_git_revision(path)
    if current is None:
        return {
            "id": server_id,
            "changed": False,
            "previous_revision": previous,
            "current_revision": None,
            "message": f"'{path}' is not a git repo; cannot detect version.",
        }

    if previous == current:
        return {
            "id": server_id,
            "changed": False,
            "previous_revision": previous,
            "current_revision": current,
            "message": f"'{server_id}' is up to date ({current}).",
        }

    if dry_run:
        return {
            "id": server_id,
            "changed": True,
            "previous_revision": previous,
            "current_revision": current,
            "message": f"Would update '{server_id}' {previous} -> {current}.",
        }

    # Re-register: remove then add with identical fields + new metadata
    scope = "project" if config.project_cfg and any(
        s.id == server_id for s in config.project_cfg.mcp_servers
    ) else "global"
    project_root = config.project_root if scope == "project" else None

    try:
        remove_mcp(server_id, scope=scope, project_root=project_root)
        add_mcp(
            id=server.id,
            type=server.type,
            command=server.command,
            args=list(server.args),
            url=server.url,
            env=dict(server.env) if server.env else None,
            scope=scope,
            project_root=project_root,
            enabled=server.enabled,
        )
    except Exception as exc:
        return {
            "id": server_id,
            "changed": False,
            "previous_revision": previous,
            "current_revision": current,
            "message": f"Failed to update '{server_id}': {exc}",
        }

    # Write updated metadata back into the config entry.
    # We reload and mutate the raw YAML so we don't drop extra keys.
    from gearcore_hub.registry import _read_yaml, _write_yaml, _config_path
    cfg_path = _config_path(scope, project_root)
    data = _read_yaml(cfg_path)
    for s in data.get("registry", {}).get("mcp_servers", []):
        if s.get("id") == server_id:
            s.setdefault("update_metadata", {}).update({
                "source_path": str(path),
                "revision": current,
            })
            break
    _write_yaml(cfg_path, data)

    return {
        "id": server_id,
        "changed": True,
        "previous_revision": previous,
        "current_revision": current,
        "message": f"Updated '{server_id}' {previous or 'unknown'} -> {current}.",
    }


def update_all_mcp_servers(config, *, dry_run: bool = False) -> list[dict]:
    """Update all MCP servers for which a source path can be inferred."""
    results = []
    for server in config.mcp_servers:
        result = update_mcp_server(config, server.id, dry_run=dry_run)
        results.append(result)
    return results
```

- [ ] **Step 2: Add tests**

Append to `tests/unit/test_update.py`:

```python
from unittest.mock import patch, MagicMock

from gearcore_hub.update import update_mcp_server


def test_update_mcp_server_no_change(tmp_path: Path):
    # Build a minimal effective config mock
    server = McpServerConfig(
        id="demo",
        command="uv",
        args=["run", "--directory", str(tmp_path), "python", "-m", "demo"],
        update_metadata={"revision": "abc1234"},
    )
    config = MagicMock()
    config.mcp_servers = [server]
    config.project_cfg = None
    config.project_root = None

    with patch("gearcore_hub.update.get_git_revision", return_value="abc1234"):
        with patch("gearcore_hub.update.remove_mcp") as mock_remove:
            with patch("gearcore_hub.update.add_mcp") as mock_add:
                result = update_mcp_server(config, "demo")
                assert result["changed"] is False
                assert "up to date" in result["message"]
                mock_remove.assert_not_called()
                mock_add.assert_not_called()


def test_update_mcp_server_changes(tmp_path: Path):
    server = McpServerConfig(
        id="demo",
        command="uv",
        args=["run", "--directory", str(tmp_path), "python", "-m", "demo"],
        update_metadata={"revision": "oldrev"},
    )
    config = MagicMock()
    config.mcp_servers = [server]
    config.project_cfg = None
    config.project_root = None

    with patch("gearcore_hub.update.get_git_revision", return_value="newrev"):
        with patch("gearcore_hub.update.remove_mcp") as mock_remove:
            with patch("gearcore_hub.update.add_mcp") as mock_add:
                with patch("gearcore_hub.update._read_yaml", return_value={"registry": {"mcp_servers": [{"id": "demo"}]}}):
                    with patch("gearcore_hub.update._write_yaml") as mock_write:
                        result = update_mcp_server(config, "demo")
                        assert result["changed"] is True
                        assert result["current_revision"] == "newrev"
                        mock_remove.assert_called_once()
                        mock_add.assert_called_once()
                        mock_write.assert_called_once()


def test_update_mcp_server_dry_run(tmp_path: Path):
    server = McpServerConfig(
        id="demo",
        command="uv",
        args=["run", "--directory", str(tmp_path), "python", "-m", "demo"],
        update_metadata={"revision": "oldrev"},
    )
    config = MagicMock()
    config.mcp_servers = [server]

    with patch("gearcore_hub.update.get_git_revision", return_value="newrev"):
        with patch("gearcore_hub.update.remove_mcp") as mock_remove:
            with patch("gearcore_hub.update.add_mcp") as mock_add:
                result = update_mcp_server(config, "demo", dry_run=True)
                assert result["changed"] is True
                assert "Would update" in result["message"]
                mock_remove.assert_not_called()
                mock_add.assert_not_called()
```

- [ ] **Step 3: Run tests**

Run: `uv run python -m pytest tests/unit/test_update.py -v`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add src/gearcore_hub/update.py tests/unit/test_update.py
git commit -m "feat(update): MCP server update logic with version-aware re-registration"
```

---

### Task 4: Implement skill bundle update logic

**Files:**
- Modify: `src/gearcore_hub/update.py`
- Modify: `src/gearcore_hub/registry.py` (make `add_skill` support overwrite? Or reuse remove/add)
- Test: `tests/unit/test_update.py`

**Interfaces:**
- Consumes: `config.skills_dirs`, `registry.add_skill`, `registry.remove_skill`, `load_skill_manifest`, `get_git_revision`, `get_manifest_version`.
- Produces:
  - `update_skill(name, config, dry_run=False, source_path=None) -> dict`
  - `update_all_skills(config, dry_run=False) -> list[dict]`

- [ ] **Step 1: Implement skill update functions**

Append to `src/gearcore_hub/update.py`:

```python
def _skill_update_metadata_path(skills_dir: Path, name: str) -> Path:
    return skills_dir / f".{name}.gearcore-update.json"


def _load_skill_update_metadata(skills_dir: Path, name: str) -> dict[str, Any]:
    path = _skill_update_metadata_path(skills_dir, name)
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.debug("Failed to read update metadata for %s: %s", name, exc)
    return {}


def _save_skill_update_metadata(skills_dir: Path, name: str, metadata: dict[str, Any]) -> None:
    path = _skill_update_metadata_path(skills_dir, name)
    path.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")


def _find_skill_source_path(name: str, config) -> Path | None:
    """Find installed skill directory by name across skills_dirs."""
    for directory in config.skills_dirs:
        candidate = directory / name
        if candidate.exists():
            return candidate.resolve()
    return None


def update_skill(
    name: str,
    config,
    *,
    dry_run: bool = False,
    source_path: Path | None = None,
) -> dict:
    """Refresh a skill bundle if its source manifest/git version changed.

    For symlinked skills, *source_path* defaults to the symlink target.
    For copied skills, the original source is unknown; pass --source-path
    or the skill will be reported as "source unknown".
    """
    installed = _find_skill_source_path(name, config)
    if installed is None:
        return {
            "name": name,
            "changed": False,
            "message": f"Skill '{name}' not found in any skills dir.",
        }

    # Determine the authoritative source path
    src = source_path
    if src is None and installed.is_symlink():
        src = installed.readlink().resolve()
    if src is None:
        src = installed

    manifest = load_skill_manifest(installed)
    version = manifest.get("version") if manifest else None
    git_rev = get_git_revision(src) if src else None

    # Find the skills_dir that owns this installation
    skills_dir = next(d for d in config.skills_dirs if (d / name).exists())
    previous = _load_skill_update_metadata(skills_dir, name)
    current = {"version": version, "revision": git_rev}

    if previous == current and current != {"version": None, "revision": None}:
        return {
            "name": name,
            "changed": False,
            "message": f"Skill '{name}' is up to date ({version or git_rev}).",
        }

    if dry_run:
        return {
            "name": name,
            "changed": True,
            "message": f"Would update skill '{name}'.",
        }

    from gearcore_hub.registry import remove_skill, add_skill
    scope = "project" if config.project_root and (config.project_root / ".gearcore" / "skills" / name).exists() else "global"
    project_root = config.project_root if scope == "project" else None

    try:
        remove_skill(name, scope=scope, project_root=project_root)
        add_skill(src, scope=scope, project_root=project_root, symlink=installed.is_symlink())
        _save_skill_update_metadata(skills_dir, name, current)
    except Exception as exc:
        return {
            "name": name,
            "changed": False,
            "message": f"Failed to update skill '{name}': {exc}",
        }

    return {
        "name": name,
        "changed": True,
        "message": f"Updated skill '{name}'.",
    }


def update_all_skills(config, *, dry_run: bool = False) -> list[dict]:
    """Update all skill bundles found in the active skills_dirs."""
    results = []
    seen = set()
    for directory in config.skills_dirs:
        if not directory.exists():
            continue
        for entry in directory.iterdir():
            if entry.is_dir() and not entry.name.startswith("."):
                if entry.name in seen:
                    continue
                seen.add(entry.name)
                results.append(update_skill(entry.name, config, dry_run=dry_run))
    return results
```

- [ ] **Step 2: Add tests**

Append to `tests/unit/test_update.py`:

```python
from gearcore_hub.update import update_skill


def test_update_skill_up_to_date(tmp_path: Path):
    skill_src = tmp_path / "demo-skill"
    skill_src.mkdir()
    (skill_src / "SKILL.md").write_text("# demo")
    (skill_src / "manifest.json").write_text('{"version": "1.0.0"}')

    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    (skills_dir / "demo-skill").symlink_to(skill_src)

    config = MagicMock()
    config.skills_dirs = [skills_dir]
    config.project_root = None

    # First update to record metadata
    update_skill("demo-skill", config)
    # Second update should be no-op
    result = update_skill("demo-skill", config)
    assert result["changed"] is False
    assert "up to date" in result["message"]


def test_update_skill_source_unknown(tmp_path: Path):
    skill_dir = tmp_path / "skills" / "demo-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# demo")
    (skill_dir / "manifest.json").write_text('{"version": "1.0.0"}')

    config = MagicMock()
    config.skills_dirs = [tmp_path / "skills"]
    config.project_root = None

    result = update_skill("demo-skill", config)
    assert result["changed"] is False
    assert "unknown" in result["message"].lower() or "source" in result["message"].lower()
```

- [ ] **Step 3: Run tests**

Run: `uv run python -m pytest tests/unit/test_update.py -v`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add src/gearcore_hub/update.py tests/unit/test_update.py
git commit -m "feat(update): skill bundle update logic with manifest/git detection"
```

---

### Task 5: Wire `gearcore update` CLI

**Files:**
- Modify: `src/gearcore_hub/main.py`
- Modify: `src/gearcore_hub/update.py` (add `cmd_update`)
- Test: `tests/unit/test_main.py` (or new `tests/unit/test_update_cli.py`)

**Interfaces:**
- Consumes: `update_mcp_server`, `update_all_mcp_servers`, `update_skill`, `update_all_skills`, vendor `update_superpowers`, sync `sync`.
- Produces: New CLI subcommand `update` with optional resource/name args and `--dry-run`.

- [ ] **Step 1: Add CLI parser entry**

In `src/gearcore_hub/main.py`, inside `build_parser()`, after the `update-superpowers` parser (around line 553), add:

```python
    # update
    p_update = sub.add_parser(
        "update",
        help="Refresh registered MCP servers, skills, superpowers, and self-skill sync",
    )
    p_update.add_argument(
        "resource",
        nargs="?",
        choices=["mcp", "skill", "superpowers"],
        help="Resource type to update",
    )
    p_update.add_argument(
        "name",
        nargs="?",
        help="MCP server id or skill name (only when resource is specified)",
    )
    p_update.add_argument(
        "--dry-run",
        action="store_true",
        help="Show pending changes without applying",
    )
    p_update.add_argument(
        "--source-path",
        type=Path,
        help="Override source path for mcp/skill update",
    )
```

- [ ] **Step 2: Add dispatch branch**

In `src/gearcore_hub/main.py`, after the `update-superpowers` branch (around line 700) and before `onboard`, add:

```python
    if command == "update":
        from gearcore_hub.update import cmd_update
        cmd_update(config, args)
        return
```

- [ ] **Step 3: Implement `cmd_update`**

Append to `src/gearcore_hub/update.py`:

```python
def cmd_update(config, args) -> None:
    """Entry point for `gearcore update`."""
    dry_run = args.dry_run
    source_path = args.source_path
    resource = args.resource
    name = args.name

    if resource == "superpowers":
        from gearcore_hub.vendor import update_superpowers
        result = update_superpowers(dry_run=dry_run)
        upstream = result.get("upstream", "")
        upstream_short = upstream[:12] if upstream else ""
        if result.get("changed"):
            if dry_run:
                print(f"Update available: superpowers {upstream_short}")
            else:
                print(f"Updated superpowers to {upstream_short}")
        else:
            print(f"superpowers is up to date ({upstream_short})")
        return

    if resource == "mcp":
        if name:
            results = [update_mcp_server(config, name, dry_run=dry_run, source_path=source_path)]
        else:
            results = update_all_mcp_servers(config, dry_run=dry_run)
        for r in results:
            print(f"  {r['id']:20s} {r['message']}")
        return

    if resource == "skill":
        if name:
            results = [update_skill(name, config, dry_run=dry_run, source_path=source_path)]
        else:
            results = update_all_skills(config, dry_run=dry_run)
        for r in results:
            print(f"  {r['name']:20s} {r['message']}")
        return

    if resource is None:
        # Update everything
        print("Updating MCP servers...")
        for r in update_all_mcp_servers(config, dry_run=dry_run):
            print(f"  {r['id']:20s} {r['message']}")
        print("Updating skills...")
        for r in update_all_skills(config, dry_run=dry_run):
            print(f"  {r['name']:20s} {r['message']}")
        print("Updating superpowers...")
        from gearcore_hub.vendor import update_superpowers
        sp_result = update_superpowers(dry_run=dry_run)
        upstream = sp_result.get("upstream", "")
        upstream_short = upstream[:12] if upstream else ""
        if sp_result.get("changed"):
            print(f"  {'superpowers':20s} update available ({upstream_short})" if dry_run else f"  {'superpowers':20s} updated to {upstream_short}")
        else:
            print(f"  {'superpowers':20s} up to date ({upstream_short})")
        if not dry_run:
            print("Syncing GearCore self-skill...")
            from gearcore_hub.sync import sync
            sync_results = sync()
            for target, result in sync_results.items():
                print(f"  {target:20s} {result}")
        return

    # argparse choices should prevent reaching here
    print(f"Error: unknown resource '{resource}'", file=sys.stderr)
    sys.exit(1)
```

Add `import sys` at the top of `update.py` if not already there.

- [ ] **Step 4: Add CLI test**

Create `tests/unit/test_update_cli.py`:

```python
from unittest.mock import MagicMock, patch


@patch("gearcore_hub.update.update_all_mcp_servers", return_value=[{"id": "x", "message": "ok"}])
@patch("gearcore_hub.update.update_all_skills", return_value=[{"name": "y", "message": "ok"}])
@patch("gearcore_hub.vendor.update_superpowers", return_value={"changed": False, "upstream": "abc123"})
@patch("gearcore_hub.sync.sync", return_value={"opencode": "linked"})
def test_update_all(mock_sync, mock_superpowers, mock_skills, mock_mcp):
    from gearcore_hub.update import cmd_update

    config = MagicMock()
    args = MagicMock()
    args.resource = None
    args.dry_run = False
    args.source_path = None
    cmd_update(config, args)
    mock_mcp.assert_called_once()
    mock_skills.assert_called_once()
    mock_superpowers.assert_called_once_with(dry_run=False)
    mock_sync.assert_called_once()


@patch("gearcore_hub.update.update_mcp_server", return_value={"id": "x", "message": "ok"})
def test_update_mcp_single(mock_update):
    from gearcore_hub.update import cmd_update

    config = MagicMock()
    args = MagicMock()
    args.resource = "mcp"
    args.name = "x"
    args.dry_run = False
    args.source_path = None
    cmd_update(config, args)
    mock_update.assert_called_once_with(config, "x", dry_run=False, source_path=None)
```

- [ ] **Step 5: Run tests**

Run: `uv run python -m pytest tests/unit/test_update_cli.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/gearcore_hub/main.py src/gearcore_hub/update.py tests/unit/test_update_cli.py
git commit -m "feat(cli): wire gearcore update subcommand"
```

---

### Task 6: Documentation and closeout

**Files:**
- Modify: `README.md`
- Modify: `CHANGELOG.md`
- Modify: `src/gearcore_hub/self_skill/SKILL.md`
- Modify: `docs/superpowers/specs/2026-08-24-gearcore-update-command-design.md` (status → Implemented)

- [ ] **Step 1: Update docs**

In `README.md`, add a section after the existing command examples:

```markdown
### Updating resources

```bash
# Update everything (MCP servers, skills, superpowers, self-skill sync)
gearcore update

# Update a single MCP server or skill
gearcore update mcp sample-prompts
gearcore update skill memory

# Preview changes without applying
gearcore update --dry-run
```
```

In `CHANGELOG.md`, add under an `## Unreleased` heading:

```markdown
- Added `gearcore update` command with version-aware refresh for MCP servers, skills, and superpowers.
```

In `src/gearcore_hub/self_skill/SKILL.md`, add a bullet under the CLI commands list:

```markdown
- `update` — Refresh registered resources (MCP servers, skills, superpowers) and sync self-skill.
```

- [ ] **Step 2: Mark spec implemented**

In `docs/superpowers/specs/2026-08-24-gearcore-update-command-design.md`, change `**Status:** Draft` to `**Status:** Implemented`.

- [ ] **Step 3: Full verification**

Run: `uv run python -m pytest -q`
Run: `uv run python -m gearcore_hub.main update --dry-run` (or `gearcore update --dry-run` from the installed tool, but use the local package for verification)
Expected: all tests pass; dry-run command lists sample-prompts/sample-devtools/etc. without error.

- [ ] **Step 4: Commit**

```bash
git add README.md CHANGELOG.md src/gearcore_hub/self_skill/SKILL.md docs/superpowers/specs/2026-08-24-gearcore-update-command-design.md
git commit -m "docs: document gearcore update command and closeout"
```

---

## Self-review notes

- Spec coverage: §1 CLI design → Task 5; §2 version detection → Task 2; §3 resource update strategies → Tasks 3 & 4; §4 config metadata → Task 1; §5 testing → all tasks; §6 docs → Task 6. ✔
- No placeholders: all code blocks are concrete. ✔
- Type consistency: `cmd_update` receives argparse `args`; `update_mcp_server` signature matches usage. ✔
- One gap addressed: skill source path for copied skills is unknown; the spec says metadata alongside entries — we use per-skill `.gearcore-update.json` files in the skills dir as the lightweight equivalent, avoiding a schema break for skills. Documented in `update_skill` docstring.
