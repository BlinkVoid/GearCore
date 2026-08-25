# Design: `gearcore update` Subcommand

**Date:** 2026-08-24
**Status:** Implemented

## Problem

GearCore has no `update` subcommand. Users must manually `remove` + `add-mcp`, or run `update-superpowers` separately, or re-run `onboard` and `sync`. This is friction, especially after modifying a local core package like SamplePrompts. We want a single `gearcore update` command that refreshes registered resources and is version-aware.

## Goals

- Add `gearcore update` with subcommands for all resource types:
  - `gearcore update` — update everything (MCP servers, skills, superpowers, self-skill sync)
  - `gearcore update mcp <id>` — update one MCP server
  - `gearcore update skill <name>` — update one skill bundle
  - `gearcore update superpowers` — refresh bundled superpowers skills
  - `gearcore update --dry-run` — show what would change without changing
- Version-aware: detect whether the source has changed since registration (git revision / manifest version). Only re-register if changed.
- Preserve GearCore's existing config schema; store update metadata alongside existing entries.

## Non-goals

- Auto-updating from PyPI/npm/crates.io (no remote package managers yet).
- Updating AI CLI tools themselves — `sync` already handles GearCore self-skill.
- Modifying the MCP protocol or hub runtime.

## Section 1 — CLI design

New subcommand under `build_parser()`:

```python
p_update = sub.add_parser("update", help="Refresh registered MCP servers, skills, and bundled resources")
p_update.add_argument("resource", nargs="?", choices=["mcp", "skill", "superpowers"], help="Resource type to update")
p_update.add_argument("name", nargs="?", help="MCP id or skill name (requires resource)")
p_update.add_argument("--dry-run", action="store_true", help="Show pending changes without applying")
p_update.add_argument("--source-path", help="Override source path for mcp/skill update")
```

Dispatch in `main()`:

```python
if command == "update":
    from gearcore_hub.update import cmd_update
    cmd_update(config, args)
    return
```

## Section 2 — Version detection

Create `src/gearcore_hub/update.py` with helper functions:

1. `get_git_revision(path: Path) -> str | None`: run `git rev-parse HEAD` and return short SHA if path is inside a git repo.
2. `get_manifest_version(path: Path) -> str | None`: read `manifest.json` and return `"version"` if present.
3. `read_update_metadata(config, id, kind) -> dict`: from config YAML, read `update_metadata` block stored under each entry.
4. `has_changed(current: str, previous: str | None) -> bool`: compare; if previous is None, treat as changed (first update).

## Section 3 — Update strategies per resource

### MCP servers

For each registered MCP server:
- Try to infer source path from `--directory <path>` or `--directory=<path>` in args, or from the command path if it is a local script.
- If path found, get `git rev-parse HEAD`.
- If changed (or `--source-path` provided), call `remove_mcp(id)` then `add_mcp(...)` with the existing config.
- Store new `update_metadata: {source_path, revision}` in the config entry.

### Skills

For each registered skill:
- Read the skill source directory (stored in `skills_dirs` or project-local `.gearcore/skills/`).
- Check `manifest.json` version or git revision of the directory.
- If changed, re-register via `add_skill` (or remove + add).
- Store `update_metadata`.

### Superpowers

Delegate to existing `update_superpowers()` in `vendor.py`. Print result.

### Self-skill sync

After all updates, run `sync()` so AI CLI tools pick up any changes.

## Section 4 — Config metadata

Extend `CONFIG_SCHEMA.md` (or config model) so each `mcp_servers` entry and skill entry can optionally carry:

```yaml
update_metadata:
  source_path: ~/src/SamplePrompts
  revision: b68963c
  updated_at: "2026-08-24T21:56:00"
```

This is additive; old configs without metadata still work and are treated as "needs update".

## Section 5 — Testing

Add tests in `tests/`:
- `test_update_mcp_no_change`: git revision unchanged → no remove/add calls.
- `test_update_mcp_changed`: git revision changed → remove + add invoked.
- `test_update_superpowers_delegates`: calls vendor `update_superpowers`.
- `test_update_dry_run`: prints pending changes, does not mutate config.

Use temp directories + `subprocess` mock or monkeypatch for git.

## Section 6 — Documentation

- Update `README.md` with `gearcore update` usage examples.
- Update `CHANGELOG.md`.
- Update `src/gearcore_hub/self_skill/SKILL.md` to mention the new command.

## Approaches

- **A — Best-effort path inference with metadata (recommended):** Infer source paths from existing registration args, store update metadata, compare git/manifest versions. Minimal schema change; works for local dev packages.
- **B — Explicit re-registration without metadata:** `gearcore update mcp <id>` just re-runs `add-mcp` after removing, no version checks. Simpler but not version-aware and may churn unnecessarily.
- **C — Full package-manager integration:** Track PyPI/npm versions. Much larger scope; deferred.

**Decision:** Approach A for this round.
