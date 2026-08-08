# DISTILL.md — GearCore Distilled Learnings

> Persistent knowledge for future agents working in this repo. Read this before making changes.

## What This Is

GearCore is a **unified skill + MCP hub** for AI CLI tools (Claude Code, Codex, Kimi, OpenCode).
One registration (`gearcore add-mcp` / `add-skill`) can propagate via `gearcore sync` to explicitly
selected or PATH-detected clients, which receive the self-skill in their discovery paths. Tools stay
hidden until the AI pulls them via `request-skill` — progressive disclosure keeps the context lean.

## Architecture Decisions (and WHY)

- **Skill-first, not MCP-first** (see DESIGN_RATIONALE.md): MCP pushes full tool schemas into every
  message (~2,400 tokens/server/conversation). A CLI skill is pull-model: zero cost at rest, only
  `list-skills`/`request-skill` output enters context. `gearcore serve` (MCP hub mode) is a fallback
  for clients without skill support, not the primary path. Do not "improve" it into the main path.
- **Stateless per-call**: `gearcore call` starts the backend, invokes one tool, exits. No persistent
  hub process; OS process lifecycle = session lifecycle. Shared process management exists only in
  `serve` mode (`process_manager.py`).
- **Layered config has versioned semantics**: built-in defaults →
  `~/.config/gearcore/config.yaml` → `<project>/.gearcore/config.yaml`. In a wholly v2 setup,
  project allowlists narrow globals, project-local skills/MCPs are included, and a local MCP
  shadows a same-ID global. Do not preserve that as a universal invariant. Under v3 the selected
  global profile resolves first; v3 project entries can narrow with include/deny, denies apply to
  local IDs, constrained/envelope launches impose a profile capability ceiling, and protected
  globals survive v2/v3 project omissions, denies, and collisions. An envelope-approved alternate
  also receives the enforced skill-binding ceiling.
- **Profiles are not containment**: the default operator is for a human-started CLI; a launcher
  must sign a constrained worker envelope. Profiles are policy/defense in depth. Authenticated MCP
  servers plus launcher process/filesystem/network isolation form the hostile-process boundary.
- **Credentials are reference-only**: YAML retains `credential_ref`, never a token. The store
  accepts only safe current-user files. `SharedMCPServer` materializes the secret at start into a
  private stdio child environment or ephemeral SSE/streamable-HTTP bearer header, without mutating
  the parent environment or retaining temporary credential mappings.
- **`add-mcp --scope project` has two modes**: default writes a project-local definition into
  `registry.mcp_servers`; `--allowlist` appends an existing global id to `scope.mcp_servers.include`.
  Without `--allowlist`, passing a global id creates a shadowing project-local def — almost never
  what you want for an already-global server.
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
- **`add-mcp --args` is a delimiter**: it uses `argparse.REMAINDER` and must be the final GearCore
  option. Every following token is child argv verbatim, including known GearCore flags, `--help`,
  repeated `--args`, and literal `--`; put `--env`, `--scope`, and other registry options first.
  `_GearCoreArgumentParser` only compensates for Python subparsers returning a literal `--` suffix
  as extras. It also canonicalizes argparse's attached `--args=VALUE` spelling so `VALUE` is the
  first child argument and the following remainder cannot be rerouted as GearCore options;
  `--args=` and detached `--args ""` both preserve an empty child argument. Parser default `None`
  distinguishes an absent action from an explicitly empty remainder.
- **Import discipline**: a premature `Mapping` import broke startup once (0e79b32). The CLI must
  start fast and clean — avoid heavy/unnecessary top-level imports; `main.py` is on the hot path
  for every AI tool invocation.
- **Symlinks are the distribution mechanism**: sync copies the self-skill to
  `~/.config/agents/skills/gearcore/` (canonical) and symlinks from `~/.claude/skills/`,
  `~/.codex/skills/`, `~/.kimi/skills/`, `~/.config/opencode/skills/`. Test sync changes against
  real symlink behavior, not just file copies. Only explicitly selected or PATH-detected clients
  are linked; do not describe sync as unconditionally linking all four.
- **Sync consumes the existing `EffectiveConfig`**: never reload global configuration during
  self-skill rendering. Embed level-0 instructions from the effective selected profile's
  `disclosure.core_skills`; this is what keeps v3 operator/worker selection and Task7's
  single-config boundary consistent while preserving v2 behavior.
- **Vendored superpowers skills** live under `src/gearcore_hub/third_party/` (f0a7a1c). Don't edit
  vendored content in place; re-vendor via `vendor.py`.

## Conventions

- Python 3.13+, `uv` for everything: `uv pip install -e ".[dev]"`.
- Verify with `uv run python verify_hub.py` and `uv run python verify_skills.py` (integration),
  plus the `tests/` suite. Run all three before claiming done.
- Config and skill formats are spec'd in CONFIG_SCHEMA.md / SKILL_SCHEMA.md — change schema and
  docs together. Architecture-level changes belong in ARCHITECTURE.md and a spec under `docs/`.
- CLI example commands in docs historically drifted from reality (581aa8e) — when renaming
  commands/flags, grep docs and README for stale examples and execute the documented shape in a
  temporary configuration.
- Commit style: conventional commits (`feat:`, `fix:`, `docs:`, `refactor:`, `test:`, `chore:`).
