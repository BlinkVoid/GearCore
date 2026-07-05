# Vendor Superpowers Skills into GearCore

**Date:** 2026-07-05  
**Status:** Design approved, awaiting implementation plan  
**Related:** GearCore `config.py`, `registry.py`, `sync.py`, CLI surface

## Problem

GearCore currently exposes the foundational "superpowers" skills through symlinks that point into `/home/r345/workspace/HIVE/third_party/superpowers/skills/`. This means GearCore, which is intended to be a self-contained hub, depends on files outside its own repository. If the HIVE workspace is moved, renamed, or deleted, those skills break and `gearcore list-skills` reports broken symlinks.

## Goal

Move the superpowers skill bundle into the GearCore repository so GearCore owns, ships, and can update it independently. Add a lightweight update-detection mechanism so maintainers know when the vendored copy has drifted from upstream.

## Non-Goals

- Re-implement superpowers itself. Only its packaging and discovery inside GearCore changes.
- Force automatic updates. Users must explicitly run an update command.
- Add git submodules or subtrees. The first version is a plain directory copy with metadata.

## Design

### 1. Directory layout

```text
GearCore/
  third_party/
    superpowers/
      .vendor.json            # provenance + update metadata
      README.md              # attribution, license, update instructions
      skills/
        brainstorming/
        dispatching-parallel-agents/
        executing-plans/
        finishing-a-development-branch/
        receiving-code-review/
        requesting-code-review/
        subagent-driven-development/
        systematic-debugging/
        test-driven-development/
        using-git-worktrees/
        using-superpowers/
        verification-before-completion/
        writing-plans/
        writing-skills/
```

Each subdirectory is a standard skill bundle containing `SKILL.md` and `manifest.json`.

### 2. Vendoring manifest

`third_party/superpowers/.vendor.json` records where the bundle came from and when it was last refreshed.

```json
{
  "name": "superpowers",
  "source": "https://github.com/obra/superpowers.git",
  "source_ref": "main",
  "vendored_commit": "<full-sha>",
  "vendored_at": "2026-07-05",
  "paths": ["skills/*"]
}
```

This file is the source of truth for the `update-superpowers` command.

### 3. Default config integration

When GearCore scaffolds or validates the global config, the bundled superpowers skills directory is added to `skills_dirs` automatically. The resolved list should look like:

```yaml
skills_dirs:
  - ~/.config/gearcore/skills
  - ~/.config/agents/skills
  - <gearcore-install>/third_party/superpowers/skills
```

The exact path must be resolved relative to the installed package location at runtime, not at build time, so `uv tool install` and editable installs both work. The resolver uses the directory containing `src/gearcore_hub/__init__.py` (or the package root) as the anchor.

Existing user configs keep working unchanged; the bundled path is appended only when missing.

### 4. New CLI command: `gearcore update-superpowers`

Behavior:

1. Read `.vendor.json`.
2. Run `git ls-remote <source> <source_ref>` to find the current upstream commit.
3. If the upstream commit matches `vendored_commit`, print "already up to date" and exit.
4. Otherwise, clone or pull the source into a temporary directory.
5. Copy the files listed in `paths` into `third_party/superpowers/skills/`, overwriting existing bundles.
6. Update `.vendor.json` with the new commit SHA and date.
7. Print a summary of changed skills.

Safety:

- If the network call fails, the existing bundle and manifest remain untouched.
- If the source repository is unreachable, exit with a clear error message and non-zero status.
- The command operates only on the bundled path; it never modifies user-owned skills in `~/.config/gearcore/skills/`.

### 5. Status integration

`gearcore status` prints the vendored superpowers commit and, when possible, the current upstream commit. If they differ, it prints a one-line hint: `Run 'gearcore update-superpowers' to refresh.`

### 6. Conflict resolution

Skill names in the bundled directory may overlap with skills the user registered manually. GearCore's existing discovery order resolves this: directories are scanned in `skills_dirs` order, and later entries win. Because user-owned paths appear first, user overrides continue to take precedence without any code change. This is the intended behavior: bundled skills provide defaults, but users can shadow them.

### 7. Testing

- Unit test for `update-superpowers`: mock a local git source directory, run the update helper, and assert `.vendor.json` is updated and the skill files are copied.
- Unit test for config scaffolding: assert the bundled superpowers path is included in default `skills_dirs`.
- Integration test via CLI: after a fresh install, `gearcore list-skills` includes `using-superpowers` and the other superpowers skills without broken symlinks.

## Decisions

- `update-superpowers` will support `--dry-run` to preview upstream changes without writing files.
- The command remains specific (`gearcore update-superpowers`) because only one vendor bundle exists today. A generic `update-vendor` can be introduced later if more bundles are added.
- The bundled superpowers path is appended at the end of `skills_dirs` so user-owned skills keep precedence.

## Migration for existing installs

1. Remove the old symlinks from `~/.config/gearcore/skills/` that point to HIVE.
2. Ensure the global config includes the bundled `third_party/superpowers/skills/` path.
3. Run `gearcore list-skills` to confirm all superpowers skills are present.

No breaking changes are expected; existing project configs and allowlists continue to work.
