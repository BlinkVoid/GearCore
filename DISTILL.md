# DISTILL.md — GearCore Distilled Learnings

> Persistent knowledge for future agents working in this repo. Read this before making changes.

## What This Is

GearCore is a **unified skill + MCP hub** for AI CLI tools (Claude Code, Codex, Kimi, OpenCode).
One registration (`gearcore add-mcp` / `add-skill`) propagates to every client via `gearcore sync`,
which installs a self-skill into each tool's skill path. Tools stay hidden until the AI pulls them
via `request-skill` — progressive disclosure keeps the context window lean.

## Architecture Decisions (and WHY)

- **Skill-first, not MCP-first** (see DESIGN_RATIONALE.md): MCP pushes full tool schemas into every
  message (~2,400 tokens/server/conversation). A CLI skill is pull-model: zero cost at rest, only
  `list-skills`/`request-skill` output enters context. `gearcore serve` (MCP hub mode) is a fallback
  for clients without skill support, not the primary path. Do not "improve" it into the main path.
- **Stateless per-call**: `gearcore call` starts the backend, invokes one tool, exits. No persistent
  hub process; OS process lifecycle = session lifecycle. Shared process management exists only in
  `serve` mode (`process_manager.py`).
- **Layered config, narrow-only scoping**: built-in defaults → `~/.config/gearcore/config.yaml` →
  `<project>/.gearcore/config.yaml`. Projects can only *narrow* via allowlists (`scope.*.include`),
  never widen. Project-local skills (`.gearcore/skills/`) are always visible in that project and
  never outside it. Preserve this invariant in `config.py`/`skill_manager.py`.
- **Skill levels**: L0 skills (`disclosure.core_skills`) skip the request hop — auto-activated in
  `serve`, embedded into the synced self-skill, printed in full by `list-skills`. L1/L2 require
  explicit `request-skill`. Spec: `docs/superpowers/specs/2026-07-07-level0-skill-reveal-design.md`.
- **Conflict resolution**: duplicate tool names across backends resolved by `resolution.categories`
  in global config: `suppress_others`, `namespace`, or `unify`; default = server-id prefix.

## Known Pitfalls / Traps

- **`<!-- GEARCORE:LEVEL0 -->` marker** in `src/gearcore_hub/self_skill/SKILL.md` is replaced by
  `sync` with a generated Default-skills section. Never delete or hand-edit around the marker;
  level-0 rendering lives in `render.py` (shared renderer extracted after duplication bugs).
- **argparse subparser `dest` clobbering** (commit f59d5f2): `add-mcp --command` reused a dest that
  clobbered subcommand dispatch. When adding subcommands/flags, check for dest collisions in
  `main.py`.
- **Import discipline**: a premature `Mapping` import broke startup once (0e79b32). The CLI must
  start fast and clean — avoid heavy/unnecessary top-level imports; `main.py` is on the hot path
  for every AI tool invocation.
- **Symlinks are the distribution mechanism**: sync copies the self-skill to
  `~/.config/agents/skills/gearcore/` (canonical) and symlinks from `~/.claude/skills/`,
  `~/.codex/skills/`, `~/.kimi/skills/`, `~/.config/opencode/skills/`. Test sync changes against
  real symlink behavior, not just file copies.
- **Vendored superpowers skills** live under `src/gearcore_hub/third_party/` (f0a7a1c). Don't edit
  vendored content in place; re-vendor via `vendor.py`.

## Conventions

- Python 3.13+, `uv` for everything: `uv pip install -e ".[dev]"`.
- Verify with `uv run python verify_hub.py` and `uv run python verify_skills.py` (integration),
  plus the `tests/` suite. Run all three before claiming done.
- Config and skill formats are spec'd in CONFIG_SCHEMA.md / SKILL_SCHEMA.md — change schema and
  docs together. Architecture-level changes belong in ARCHITECTURE.md and a spec under `docs/`.
- CLI example commands in docs historically drifted from reality (581aa8e) — when renaming
  commands/flags, grep docs and README for stale examples. (Note: README quick-start shows the
  historical `geracore` typo in a few snippets; real binary is `gearcore`.)
- Commit style: conventional commits (`feat:`, `fix:`, `docs:`, `refactor:`, `test:`, `chore:`).
