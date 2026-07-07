# Level-0 Skill Reveal — Design

**Date:** 2026-07-07
**Status:** Approved for planning

## Problem

GearCore's progressive disclosure hides every skill behind two hops:
the AI must (1) decide to invoke gearcore at all, then (2) run
`list-skills` and `request-skill <name>`. For skills that should shape
*every* session — e.g. `continuity-core` (session resume/handoff) —
this fails at hop 1: at session start the AI only sees gearcore's
one-line description, which says nothing about resume/handoff, so the
trigger words never connect. (Observed in practice: "update continuity
document" sent the model hunting through the filesystem instead of
loading the skill.)

GearCore already has half the concept: `disclosure.core_skills`
auto-activates skills' tools — but only in MCP `serve` mode. The CLI
flow that Claude Code, Codex, Kimi, and OpenCode actually use ignores
it.

## Decision

`disclosure.core_skills` becomes the single **"level 0"** concept,
honored on all surfaces. No config schema change; project
`.gearcore/config.yaml` overrides already work. Skills listed there are
revealed by default at three layers:

| Layer | Mechanism | When the AI sees it |
|---|---|---|
| 1. Instruction files | Manual generic pointer in each tool's global instructions | Session start, before any tool call |
| 2. Self-skill body | `sync` embeds a generated "Default skills" section | When the gearcore skill is loaded |
| 3. CLI output | `list-skills` inlines full instructions of level-0 skills | On first gearcore invocation |

### Layer 1 — instruction-file pointer (manual, one-time)

A short block added by hand to:

- `~/.claude/CLAUDE.md` (Claude Code)
- `~/.codex/AGENTS.md` (Codex)
- `~/.config/opencode/AGENTS.md` (OpenCode)

Wording names the **mechanism, not the skill**, so it never goes stale
when `core_skills` changes:

> **GearCore default skills** — when asked to resume, hand off, wrap
> up, or report project status — or before non-trivial project work —
> run `gearcore list-skills` (add `--project <path>` if a `.gearcore/`
> directory exists). It reveals default (level-0) skills inline, e.g.
> session continuity. Follow them.

A sync-managed block (gearcore writing these files itself) was
considered and deferred: it makes gearcore edit personal instruction
files and needs idempotent marker management — revisit if the manual
pointer proves annoying to maintain.

### Layer 2 — sync-time embedding

- The self-skill source (`src/gearcore_hub/self_skill/SKILL.md`) gains
  a placeholder marker: `<!-- GEARCORE:LEVEL0 -->`.
- During `_install_canonical`, `sync` replaces the marker with a
  generated **"Default skills — always relevant"** section: one bullet
  per skill in the *global* config's `core_skills`, with the skill's
  name, manifest description, and its
  `gearcore request-skill <name>` command.
- Empty `core_skills`, or a listed skill that isn't registered →
  bullet skipped (warning logged); empty section → marker removed,
  section omitted.
- The section refreshes on every `gearcore sync`; all four tool
  symlinks (claude/codex/kimi/opencode) see it immediately since they
  point at the canonical copy.

### Layer 3 — `list-skills` inline reveal

- `cmd_list_skills` prints, before the regular one-line listing, a
  clearly delimited block per visible level-0 skill containing the full
  `request-skill` output (instructions + `gearcore call` tool lines).
- Skills in `core_skills` that are **not visible** in the current
  context (project allowlist) are skipped silently — same semantics as
  the existing `_auto_activate_core`.
- The MCP `list_skills` tool in `serve` mode is unchanged; serve
  already auto-activates core skills' tools.

### Refactor

Instruction rendering currently lives inline in `cmd_request_skill`
(`main.py`). Extract a shared helper (e.g.
`render_skill_instructions(skill) -> str`) used by both
`request-skill` and the level-0 blocks in `list-skills`, so the two
outputs cannot drift.

## Testing

- Section generation: with core skills / empty list / listed-but-
  unregistered skill / marker present-or-absent in source SKILL.md.
- `list-skills` inlining: core skill visible / hidden by project
  allowlist / `core_skills` empty.
- Existing sync tests still pass (marker handling must not break plain
  copies).

## Rollout

1. Implement + tests.
2. Add `continuity-core` to `disclosure.core_skills` in
   `~/.config/gearcore/config.yaml`.
3. `gearcore sync` (after reinstalling the uv tool from source).
4. Add the Layer-1 pointer block to the three instruction files.
5. Verify: `gearcore list-skills` inlines continuity-core;
   `opencode debug skill` still lists gearcore; synced SKILL.md
   contains the Default skills section.

## Out of scope

- Sync-managed instruction-file blocks (deferred, see Layer 1).
- Semantic activation (`activation_threshold` remains reserved).
- Changing serve-mode disclosure behavior.
