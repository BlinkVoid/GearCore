# Level-0 Skill Reveal Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Skills listed in `disclosure.core_skills` ("level 0") are revealed by default: embedded into the synced self-skill SKILL.md and printed in full by `gearcore list-skills`, so session-shaping skills like `continuity-core` reach the AI without an explicit `request-skill` hop.

**Architecture:** A new `render.py` module owns all instruction-rendering (shared by `request-skill`, `list-skills`, and sync so outputs cannot drift). `sync` post-processes the canonical SKILL.md, replacing a `<!-- GEARCORE:LEVEL0 -->` marker with a generated "Default skills" section from the **global** config. `cmd_list_skills` prints delimited full-instruction blocks for visible level-0 skills from the **effective** (project-aware) config. No config schema change — `disclosure.core_skills` gains the wider semantics.

**Tech Stack:** Python 3.13+, pydantic v2 models (existing), pytest, uv. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-07-07-level0-skill-reveal-design.md`

## Global Constraints

- No new dependencies; no config schema changes (`DisclosureConfig` stays as-is).
- Level-0 skills not visible in the current context are skipped **silently** in list-skills (matches `_auto_activate_core`); unregistered names log a warning in sync section generation.
- Marker string is exactly `<!-- GEARCORE:LEVEL0 -->` on its own line.
- MCP `serve` mode behavior unchanged.
- Follow repo style: module-level `logger = logging.getLogger("gearcore.<name>")`, section-divider comments, tests as plain classes/functions using `tmp_path` or `tempfile` (see `tests/test_skill_manager.py`).
- Run the full suite (`uv run pytest tests/ -q`) before every commit; all tests green.

---

### Task 1: `render.py` — shared instruction renderer

**Files:**
- Create: `src/gearcore_hub/render.py`
- Modify: `src/gearcore_hub/main.py` (function `cmd_request_skill`, currently lines 319–338)
- Test: `tests/test_render.py` (create)

**Interfaces:**
- Consumes: `SkillBundle` from `gearcore_hub.skill_manager` (fields: `.instructions: str`, `.manifest.mcp_servers: list[dict]`).
- Produces: `render_skill_instructions(bundle: SkillBundle) -> str` — later tasks call this from `cmd_list_skills` and it must match `request-skill` output byte-for-byte (minus trailing newline handling).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_render.py`:

```python
"""Tests for instruction rendering."""

from pathlib import Path

from gearcore_hub.render import render_skill_instructions
from gearcore_hub.skill_manager import SkillBundle, SkillManifest


def _bundle(name: str = "demo", mcp_servers: list | None = None) -> SkillBundle:
    return SkillBundle(
        path=Path("/nonexistent"),
        manifest=SkillManifest(
            name=name, description="a demo skill", mcp_servers=mcp_servers or []
        ),
        instructions=f"# {name}\n\nDo the thing.",
    )


class TestRenderSkillInstructions:
    def test_plain_skill_renders_instructions_only(self):
        out = render_skill_instructions(_bundle())
        assert out == "# demo\n\nDo the thing."

    def test_mcp_skill_appends_call_commands(self):
        bundle = _bundle(
            mcp_servers=[{"server_id": "fs", "tools": ["read_file", "write_file"]}]
        )
        out = render_skill_instructions(bundle)
        assert out.startswith("# demo")
        assert "## Available tools (via `gearcore call`)" in out
        assert "gearcore call fs read_file '<json_args>'" in out
        assert "gearcore call fs write_file '<json_args>'" in out
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_render.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'gearcore_hub.render'`

- [ ] **Step 3: Create `src/gearcore_hub/render.py`**

```python
"""
Rendering helpers for skill instructions and level-0 disclosure.

Shared by the request-skill and list-skills CLI commands and by sync,
so the different surfaces cannot drift apart.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping

from gearcore_hub.skill_manager import SkillBundle

logger = logging.getLogger("gearcore.render")


def render_skill_instructions(bundle: SkillBundle) -> str:
    """Full text an AI needs to use a skill: SKILL.md + `gearcore call` lines."""
    parts = [bundle.instructions]
    if bundle.manifest.mcp_servers:
        lines = ["", "---", "", "## Available tools (via `gearcore call`)", ""]
        for mcp_entry in bundle.manifest.mcp_servers:
            server_id = mcp_entry.get("server_id", "")
            for tool in mcp_entry.get("tools", []):
                lines.append(f"  gearcore call {server_id} {tool} '<json_args>'")
        parts.append("\n".join(lines))
    return "\n".join(parts)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_render.py -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Refactor `cmd_request_skill` to use the renderer**

In `src/gearcore_hub/main.py`, replace the body after the not-found check (the `print(skill.instructions)` line and the whole `if skill.manifest.mcp_servers:` block) so the function reads:

```python
def cmd_request_skill(config: EffectiveConfig, skill_name: str):
    sm = SkillManager(config)
    skill = sm.get_skill(skill_name)
    if not skill:
        print(
            f"Error: skill '{skill_name}' not found or not visible in this context.",
            file=sys.stderr,
        )
        sys.exit(1)
    print(render_skill_instructions(skill))
```

Add the import at the top of `main.py` alongside the other `gearcore_hub` imports:

```python
from gearcore_hub.render import render_skill_instructions
```

Note the one intentional output change: the tool lines previously came after a
literal `\n---\n` from a separate `print`; now the separator is part of one
string. Net output is identical except the old version printed a trailing
blank line between instructions and `---` — acceptable.

- [ ] **Step 6: Run the full suite and smoke-check the CLI**

Run: `uv run pytest tests/ -q`
Expected: all pass (42 tests: 40 existing + 2 new)

Run: `uv run gearcore request-skill continuity-core | head -8`
Expected: SKILL.md frontmatter/heading printed, no traceback.

- [ ] **Step 7: Commit**

```bash
git add src/gearcore_hub/render.py src/gearcore_hub/main.py tests/test_render.py
git commit -m "refactor: extract shared skill-instruction renderer"
```

---

### Task 2: Level-0 section generation + marker application (pure functions)

**Files:**
- Modify: `src/gearcore_hub/render.py`
- Test: `tests/test_render.py`

**Interfaces:**
- Consumes: `SkillBundle`, `render_skill_instructions` from Task 1.
- Produces (used by Task 3's sync integration):
  - `LEVEL0_MARKER: str` — `"<!-- GEARCORE:LEVEL0 -->"`
  - `render_level0_section(core_skills: list[str], skills: Mapping[str, SkillBundle]) -> str` — markdown section, `""` when nothing to show
  - `apply_level0_marker(content: str, section: str) -> str` — SKILL.md text with marker replaced (or marker line dropped when section is `""`)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_render.py`:

```python
from gearcore_hub.render import (
    LEVEL0_MARKER,
    apply_level0_marker,
    render_level0_section,
)


class TestRenderLevel0Section:
    def test_empty_core_skills_returns_empty(self):
        assert render_level0_section([], {}) == ""

    def test_unregistered_skill_skipped(self):
        assert render_level0_section(["ghost"], {}) == ""

    def test_section_lists_name_description_and_command(self):
        skills = {"continuity-core": _bundle("continuity-core")}
        out = render_level0_section(["continuity-core"], skills)
        assert "## Default skills — always relevant" in out
        assert "**continuity-core**" in out
        assert "a demo skill" in out
        assert "`gearcore request-skill continuity-core`" in out


class TestApplyLevel0Marker:
    CONTENT = "# GearCore\n\nintro text\n\n" + LEVEL0_MARKER + "\n\n## Workflow\n"

    def test_marker_replaced_with_section(self):
        out = apply_level0_marker(self.CONTENT, "## Default skills\n\n- x\n")
        assert LEVEL0_MARKER not in out
        assert "## Default skills" in out
        assert "## Workflow" in out

    def test_marker_line_dropped_when_section_empty(self):
        out = apply_level0_marker(self.CONTENT, "")
        assert LEVEL0_MARKER not in out
        assert "intro text" in out
        assert "## Workflow" in out

    def test_content_without_marker_unchanged(self):
        content = "# GearCore\n\nno marker here\n"
        assert apply_level0_marker(content, "anything") == content
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_render.py -v`
Expected: FAIL — `ImportError: cannot import name 'LEVEL0_MARKER'`

- [ ] **Step 3: Implement in `src/gearcore_hub/render.py`**

Append:

```python
# ---------------------------------------------------------------------------
# Level-0 (default-reveal) disclosure
# ---------------------------------------------------------------------------

LEVEL0_MARKER = "<!-- GEARCORE:LEVEL0 -->"


def render_level0_section(
    core_skills: list[str], skills: Mapping[str, SkillBundle]
) -> str:
    """
    Markdown 'Default skills' section for the synced self-skill SKILL.md.
    Returns "" when no listed skill is registered.
    """
    bullets = []
    for name in core_skills:
        bundle = skills.get(name)
        if bundle is None:
            logger.warning("core_skills entry '%s' is not a registered skill", name)
            continue
        bullets.append(
            f"- **{name}** — {bundle.manifest.description}\n"
            f"  Load with: `gearcore request-skill {name}`"
        )
    if not bullets:
        return ""
    return (
        "## Default skills — always relevant\n\n"
        "These level-0 skills are revealed by default. `gearcore list-skills` prints\n"
        "their full instructions inline; load and follow them whenever their topic\n"
        "applies, before other project work:\n\n" + "\n".join(bullets) + "\n"
    )


def apply_level0_marker(content: str, section: str) -> str:
    """Replace the marker with *section*, or drop the marker line when empty."""
    if LEVEL0_MARKER not in content:
        return content
    if not section:
        lines = [
            line for line in content.splitlines() if line.strip() != LEVEL0_MARKER
        ]
        return "\n".join(lines) + ("\n" if content.endswith("\n") else "")
    return content.replace(LEVEL0_MARKER, section)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_render.py -v`
Expected: PASS (8 tests)

- [ ] **Step 5: Commit**

```bash
git add src/gearcore_hub/render.py tests/test_render.py
git commit -m "feat: level-0 section rendering and marker application"
```

---

### Task 3: Sync integration — embed the section at install time

**Files:**
- Modify: `src/gearcore_hub/config.py` (extract `load_global_config`)
- Modify: `src/gearcore_hub/sync.py` (`_install_canonical`)
- Modify: `src/gearcore_hub/self_skill/SKILL.md` (add marker + workflow note)
- Test: `tests/test_sync_level0.py` (create), `tests/test_config.py` (append)

**Interfaces:**
- Consumes: `render_level0_section`, `apply_level0_marker`, `LEVEL0_MARKER` (Task 2); `SkillManager`, `EffectiveConfig`.
- Produces:
  - `load_global_config(global_config_path: Path | None = None) -> GlobalConfig` in `config.py` (reused by `load_config`)
  - `embed_level0_section(skill_md: Path, global_config_path: Path | None = None) -> bool` in `sync.py` — returns True if the file was modified

- [ ] **Step 1: Write the failing tests**

Create `tests/test_sync_level0.py`:

```python
"""Tests for level-0 section embedding during sync."""

from pathlib import Path

from gearcore_hub.render import LEVEL0_MARKER
from gearcore_hub.sync import embed_level0_section


def _write_global_config(tmp_path: Path, skills_dir: Path, core: list[str]) -> Path:
    cfg = tmp_path / "config.yaml"
    lines = [
        "version: 2",
        "registry:",
        "  skills_dirs:",
        f"    - {skills_dir}",
        "disclosure:",
        f"  core_skills: [{', '.join(core)}]",
    ]
    cfg.write_text("\n".join(lines) + "\n")
    return cfg


def _make_skill(base: Path, name: str) -> None:
    d = base / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: continuity stuff\n---\n\n# {name}")
    (d / "manifest.json").write_text(
        f'{{"name": "{name}", "description": "continuity stuff"}}'
    )


class TestEmbedLevel0Section:
    def test_marker_replaced_with_core_skill(self, tmp_path):
        skills_dir = tmp_path / "skills"
        _make_skill(skills_dir, "continuity-core")
        cfg = _write_global_config(tmp_path, skills_dir, ["continuity-core"])

        skill_md = tmp_path / "SKILL.md"
        skill_md.write_text(f"# GearCore\n\n{LEVEL0_MARKER}\n\n## Workflow\n")

        changed = embed_level0_section(skill_md, global_config_path=cfg)

        assert changed is True
        text = skill_md.read_text()
        assert LEVEL0_MARKER not in text
        assert "**continuity-core**" in text
        assert "continuity stuff" in text

    def test_marker_dropped_when_no_core_skills(self, tmp_path):
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        cfg = _write_global_config(tmp_path, skills_dir, [])

        skill_md = tmp_path / "SKILL.md"
        skill_md.write_text(f"# GearCore\n\n{LEVEL0_MARKER}\n\n## Workflow\n")

        changed = embed_level0_section(skill_md, global_config_path=cfg)

        assert changed is True
        text = skill_md.read_text()
        assert LEVEL0_MARKER not in text
        assert "Default skills" not in text
        assert "## Workflow" in text

    def test_file_without_marker_untouched(self, tmp_path):
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        cfg = _write_global_config(tmp_path, skills_dir, ["anything"])

        skill_md = tmp_path / "SKILL.md"
        original = "# GearCore\n\nno marker\n"
        skill_md.write_text(original)

        changed = embed_level0_section(skill_md, global_config_path=cfg)

        assert changed is False
        assert skill_md.read_text() == original
```

Append to `tests/test_config.py`:

```python
def test_load_global_config_reads_core_skills(tmp_path):
    from gearcore_hub.config import load_global_config

    cfg = tmp_path / "config.yaml"
    cfg.write_text("disclosure:\n  core_skills:\n    - continuity-core\n")
    g = load_global_config(cfg)
    assert g.disclosure.core_skills == ["continuity-core"]


def test_load_global_config_missing_file_gives_defaults(tmp_path):
    from gearcore_hub.config import load_global_config

    g = load_global_config(tmp_path / "nope.yaml")
    assert g.disclosure.core_skills == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_sync_level0.py tests/test_config.py -v`
Expected: FAIL — `ImportError: cannot import name 'embed_level0_section'` and `cannot import name 'load_global_config'`

- [ ] **Step 3: Extract `load_global_config` in `config.py`**

In `src/gearcore_hub/config.py`, add below `find_project_root` and change the start of `load_config` to use it:

```python
def load_global_config(global_config_path: Path | None = None) -> GlobalConfig:
    """Load only the global layer (no project detection)."""
    g_path = global_config_path or GLOBAL_CONFIG_PATH
    return GlobalConfig(**_load_yaml(g_path))
```

In `load_config`, replace:

```python
    g_path = global_config_path or GLOBAL_CONFIG_PATH
    g_data = _load_yaml(g_path)
    global_cfg = GlobalConfig(**g_data)
```

with:

```python
    global_cfg = load_global_config(global_config_path)
```

- [ ] **Step 4: Implement `embed_level0_section` in `sync.py`**

Add to `src/gearcore_hub/sync.py` (imports go at module top; `EffectiveConfig`/`SkillManager` imports are deliberately deferred into the function to keep `sync`'s import graph light for `--remove`/detection paths):

```python
def embed_level0_section(
    skill_md: Path, global_config_path: Path | None = None
) -> bool:
    """
    Replace the LEVEL0 marker in *skill_md* with the default-skills section
    generated from the global config. Global scope only — the canonical
    self-skill is shared by every project. Returns True if the file changed.
    """
    from gearcore_hub.config import EffectiveConfig, load_global_config
    from gearcore_hub.render import (
        LEVEL0_MARKER,
        apply_level0_marker,
        render_level0_section,
    )
    from gearcore_hub.skill_manager import SkillManager

    content = skill_md.read_text(encoding="utf-8")
    if LEVEL0_MARKER not in content:
        return False

    global_cfg = load_global_config(global_config_path)
    effective = EffectiveConfig(global_cfg, None, None)
    sm = SkillManager(effective)
    section = render_level0_section(global_cfg.disclosure.core_skills, sm.skills)

    new_content = apply_level0_marker(content, section)
    if new_content == content:
        return False
    skill_md.write_text(new_content, encoding="utf-8")
    return True
```

Wire it into `_install_canonical`: immediately after the `shutil.copytree(SELF_SKILL_SOURCE, CANONICAL_DIR)` line inside `if not dry_run:`, add:

```python
        if embed_level0_section(CANONICAL_DIR / "SKILL.md"):
            logger.info("Embedded level-0 default-skills section into canonical SKILL.md")
```

- [ ] **Step 5: Add the marker to the self-skill source**

In `src/gearcore_hub/self_skill/SKILL.md`, insert after the intro paragraph (after the line `window stays lean by default.` and its following blank line, before `## When to invoke GearCore`):

```markdown
<!-- GEARCORE:LEVEL0 -->

```

And in the same file, in section `### 1. Discover available skills`, append after "and scope (global or project).":

```markdown
Level-0 default skills are printed in full at the top of the output — read and
follow them without a separate `request-skill` call.
```

- [ ] **Step 6: Run the tests**

Run: `uv run pytest tests/ -q`
Expected: all pass (42 + 3 sync-level0 + 2 config = 47)

- [ ] **Step 7: Verify sync end-to-end (touches real home — expected)**

```bash
uv run gearcore sync --tool opencode
grep -q "GEARCORE:LEVEL0" ~/.config/agents/skills/gearcore/SKILL.md && echo "MARKER LEAKED" || echo "marker absent (ok)"
```
Expected: sync reports `canonical installed`; then `marker absent (ok)` — with no `core_skills` configured yet the marker is dropped, and the raw marker must never appear in the installed file.

- [ ] **Step 8: Commit**

```bash
git add src/gearcore_hub/config.py src/gearcore_hub/sync.py src/gearcore_hub/self_skill/SKILL.md tests/test_sync_level0.py tests/test_config.py
git commit -m "feat: embed level-0 default-skills section into self-skill at sync"
```

---

### Task 4: `list-skills` inline reveal

**Files:**
- Modify: `src/gearcore_hub/main.py` (function `cmd_list_skills`, currently lines 283–311)
- Test: `tests/test_list_skills_level0.py` (create)

**Interfaces:**
- Consumes: `render_skill_instructions` (Task 1); `EffectiveConfig.disclosure.core_skills`; `SkillManager.visible_skill_names`, `.skills`.
- Produces: CLI output contract — level-0 blocks delimited by `=== LEVEL-0 SKILL: <name> ===` / `=== END LEVEL-0 SKILL: <name> ===`, printed after the context heading and before the one-line listing.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_list_skills_level0.py`:

```python
"""Tests for level-0 inline reveal in cmd_list_skills."""

from pathlib import Path

from gearcore_hub.config import DisclosureConfig, EffectiveConfig, GlobalConfig, ProjectConfig
from gearcore_hub.main import cmd_list_skills


def _make_skill(base: Path, name: str) -> None:
    d = base / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(f"---\nname: {name}\n---\n\n# {name}\n\nInstructions body.")
    (d / "manifest.json").write_text(f'{{"name": "{name}", "description": "desc"}}')


def _effective(tmp_path, core_skills, project_cfg=None, project_root=None):
    skills_dir = tmp_path / "skills"
    global_cfg = GlobalConfig(
        registry={"skills_dirs": [str(skills_dir)]},
        disclosure=DisclosureConfig(core_skills=core_skills),
    )
    return EffectiveConfig(global_cfg, project_cfg, project_root)


class TestListSkillsLevel0:
    def test_core_skill_instructions_inlined(self, tmp_path, capsys):
        _make_skill(tmp_path / "skills", "continuity-core")
        _make_skill(tmp_path / "skills", "other-skill")

        cmd_list_skills(_effective(tmp_path, ["continuity-core"]))

        out = capsys.readouterr().out
        assert "=== LEVEL-0 SKILL: continuity-core ===" in out
        assert "Instructions body." in out
        assert "=== END LEVEL-0 SKILL: continuity-core ===" in out
        # regular listing still present, for both skills
        assert "other-skill" in out

    def test_no_core_skills_no_blocks(self, tmp_path, capsys):
        _make_skill(tmp_path / "skills", "other-skill")

        cmd_list_skills(_effective(tmp_path, []))

        out = capsys.readouterr().out
        assert "LEVEL-0" not in out
        assert "other-skill" in out

    def test_core_skill_hidden_by_allowlist_skipped_silently(self, tmp_path, capsys):
        _make_skill(tmp_path / "skills", "continuity-core")
        project_root = tmp_path / "proj"
        (project_root / ".gearcore").mkdir(parents=True)
        project_cfg = ProjectConfig(scope={"skills": {"include": []}})

        cmd_list_skills(
            _effective(
                tmp_path,
                ["continuity-core"],
                project_cfg=project_cfg,
                project_root=project_root,
            )
        )

        out = capsys.readouterr().out
        assert "LEVEL-0" not in out
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_list_skills_level0.py -v`
Expected: FAIL — no `LEVEL-0` blocks in output (feature absent); first and third tests fail on the assertions, second may pass.

- [ ] **Step 3: Implement in `cmd_list_skills`**

Replace the function in `src/gearcore_hub/main.py` with:

```python
def cmd_list_skills(config: EffectiveConfig):
    sm = SkillManager(config)
    skills = sm.list_available_skills()
    ctx = config.context_name
    print(f"GearCore skills ({ctx} context):\n")
    if not skills:
        print("  (no skills visible in this context)")
        return

    # Level-0 skills: reveal full instructions inline, before the listing.
    level0 = [
        name
        for name in config.disclosure.core_skills
        if name in sm.visible_skill_names
    ]
    for name in level0:
        bundle = sm.skills[name]
        print(f"=== LEVEL-0 SKILL: {name} ===")
        print("(revealed by default — read and follow these instructions now)\n")
        print(render_skill_instructions(bundle))
        print(f"=== END LEVEL-0 SKILL: {name} ===\n")

    broken = [s for s in skills if s["status"] == "broken"]
    healthy = [s for s in skills if s["status"] != "broken"]
    for s in healthy:
        tags = []
        if s["name"] in level0:
            tags.append("[level-0]")
        if s["status"] == "active":
            tags.append("[active]")
        if s["scope"] == "project":
            tags.append("[project]")
        tag_str = " ".join(tags)
        if tag_str:
            tag_str = " " + tag_str
        print(f"  {s['name']}{tag_str} — {s['description']}")
    if broken:
        print(f"\n  BROKEN SYMLINKS ({len(broken)}):")
        print(
            "  Fix with: gearcore remove <name> && gearcore add-skill --symlink <new-path>"
        )
        for s in broken:
            print(
                f"    {s['name']} → {s['description'].removeprefix('BROKEN SYMLINK → ')}"
            )
```

(`render_skill_instructions` is already imported in `main.py` from Task 1.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_list_skills_level0.py tests/ -q`
Expected: all pass (50 total)

- [ ] **Step 5: Commit**

```bash
git add src/gearcore_hub/main.py tests/test_list_skills_level0.py
git commit -m "feat: list-skills reveals level-0 skills' full instructions inline"
```

---

### Task 5: Documentation

**Files:**
- Modify: `ARCHITECTURE.md` (Self-Skill & Sync section, ~line 133; Progressive Disclosure Flow, ~line 92; Module Map, ~line 156)
- Modify: `CONFIG_SCHEMA.md` (`core_skills` rows, lines 86 and ~125)
- Modify: `README.md` (Features list, ~line 35)
- Modify: `CHANGELOG.md` (`[Unreleased]` → `Added`)

**Interfaces:** none (prose only). Cross-link the spec `docs/superpowers/specs/2026-07-07-level0-skill-reveal-design.md`.

- [ ] **Step 1: ARCHITECTURE.md**

In **Self-Skill & Sync**, after the numbered list, add:

```markdown
3. Replaces the `<!-- GEARCORE:LEVEL0 -->` marker in the canonical `SKILL.md` with a generated "Default skills" section from the global config's `disclosure.core_skills` (see `docs/superpowers/specs/2026-07-07-level0-skill-reveal-design.md`)
```

In **Progressive Disclosure Flow**, after the existing flow block, add:

```markdown
**Level-0 skills:** names in `disclosure.core_skills` skip the request hop on every surface — auto-activated in `serve` mode, embedded into the synced self-skill, and printed in full by `gearcore list-skills`. Design: `docs/superpowers/specs/2026-07-07-level0-skill-reveal-design.md`.
```

In **Module Map**, add a row:

```markdown
| `render.py` | Shared instruction rendering, level-0 section generation |
```

- [ ] **Step 2: CONFIG_SCHEMA.md**

Change the `core_skills` description (line 86) from "Skills auto-activated at session start" to:

```markdown
| `core_skills` | list[string] | `[]` | Level-0 skills: auto-activated in serve mode, embedded in the synced self-skill, revealed inline by `list-skills` |
```

- [ ] **Step 3: README.md**

In Features, change the progressive-disclosure bullet's neighborhood by adding after the "Core reasoning discipline" bullet:

```markdown
- **⓪ Level-0 skills** — `disclosure.core_skills` marks skills revealed by default: `list-skills` prints their full instructions and `sync` embeds them into the self-skill.
```

- [ ] **Step 4: CHANGELOG.md**

Under `## [Unreleased]` / `### Added`, append:

```markdown
- Level-0 skill reveal: `disclosure.core_skills` now also embeds a default-skills
  section into the synced self-skill and inlines full instructions in
  `gearcore list-skills` (spec: docs/superpowers/specs/2026-07-07-level0-skill-reveal-design.md)
```

- [ ] **Step 5: Run suite, commit**

Run: `uv run pytest tests/ -q` — all pass.

```bash
git add ARCHITECTURE.md CONFIG_SCHEMA.md README.md CHANGELOG.md
git commit -m "docs: document level-0 skill reveal"
```

---

### Task 6: Rollout — configure, sync, instruction files, verify

**Files:**
- Modify: `~/.config/gearcore/config.yaml` (user config — read it first, preserve existing content)
- Modify: `~/.claude/CLAUDE.md`, `~/.codex/AGENTS.md`, `~/.config/opencode/AGENTS.md` (append block; create file if missing)

**Interfaces:** consumes the finished feature; nothing downstream.

- [ ] **Step 1: Add continuity-core to global core_skills**

Read `~/.config/gearcore/config.yaml` first. Merge (do not clobber) so that it contains:

```yaml
disclosure:
  core_skills:
    - continuity-core
```

Keep any existing `disclosure.strategy` / `activation_threshold` / other keys.

- [ ] **Step 2: Reinstall the CLI and sync**

The installed tool is a non-editable uv install from this directory; it must be rebuilt:

```bash
uv tool install --reinstall /home/r345/workspace/GearCore
gearcore sync
```

Expected: `canonical installed`, tool links `linked`/`already linked`.

- [ ] **Step 3: Verify the three reveal layers**

```bash
grep -A6 "Default skills" ~/.config/agents/skills/gearcore/SKILL.md
gearcore list-skills | head -40
opencode debug skill 2>/dev/null | grep -o '"name": "gearcore"'
```

Expected: (1) section present with `**continuity-core**`; (2) `=== LEVEL-0 SKILL: continuity-core ===` block with full instructions; (3) `"name": "gearcore"` still discovered by opencode.

- [ ] **Step 4: Add the generic pointer to the three instruction files**

Append this exact block (with a preceding blank line) to `~/.claude/CLAUDE.md`, `~/.codex/AGENTS.md`, and `~/.config/opencode/AGENTS.md` — create the file if it does not exist. Read each file first; skip any file that already contains "GearCore default skills".

```markdown
## GearCore default skills

When asked to resume, hand off, wrap up, or report project status — or before
non-trivial work in a workspace project — run `gearcore list-skills` first
(add `--project <absolute_path>` if a `.gearcore/` directory exists in the
project tree). It reveals default (level-0) skills inline, e.g. session
continuity. Read and follow them.
```

- [ ] **Step 5: Final verification and handoff**

```bash
uv run pytest tests/ -q
gearcore list-skills | sed -n '1,10p'
```

Expected: suite green; level-0 block at top of listing. Then record a continuity handoff for the session (`continuity handoff --cwd "$PWD" --summary ...`).
