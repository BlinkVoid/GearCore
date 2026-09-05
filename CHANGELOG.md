# Changelog

All notable changes to GearCore will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- `gearcore call --json` (schema `gearcore.call/1`): opt-in structured output
  emitting exactly one deterministic JSON envelope on stdout with diagnostics
  on stderr. The envelope carries server/tool identity, `ok`/`status`,
  MCP `isError`, ordered normalized content blocks (binary payloads are
  represented by type, media type, byte length, and sha256 digest — never
  printed raw), and pass-through `structured_content`. Exit codes classify
  success (0), usage errors (2), transport errors (3), MCP tool errors (4),
  and nested DevCore command failures (5). The nested adapter is gated to the
  `devcore` server id and exact `devcore_run`/`devcore_poll` command tools and
  validates the DevCore run contract
  (`ok`/`exit_code`/`timed_out`/`elapsed_seconds`); generic domain payloads
  with an `ok` field are never interpreted
- Whole-plugin onboarding: `gearcore onboard` now detects Codex-compatible
  plugin roots (`.codex-plugin/plugin.json`), validates the manifest name and
  skills path, registers the whole plugin at the scope-specific plugins
  directory (`~/.config/gearcore/plugins/<name>` or
  `<project>/.gearcore/plugins/<name>`; symlink by default, full copy with
  `--copy-skills`), and registers discovered skills through the installed
  plugin root. Preflight is atomic — conflicting plugin/skill destinations
  (including broken symlinks) cause no mutations — and re-onboarding
  equivalent roots and links is a no-op. Plugin commands, orchestration,
  scripts, configs, tests, and docs are preserved as-is; GearCore does not
  auto-execute arbitrary plugin content (schema: `PLUGIN_SCHEMA.md`)
- `gearcore remove plugin <name>`: removes only the registered plugin path and
  skill symlinks pointing inside it; external symlink sources are never deleted
- Rendered skill instructions (`request-skill`, `request_skill`) now include
  the absolute registered and resolved skill bundle locations, with guidance
  that relative resources resolve from the bundle root (applies to
  plugin-backed and ordinary skills)

### Changed
- `gearcore call` (legacy text mode) is now failure-aware: MCP results with
  `isError` and `devcore` command tools returning `ok: false` exit 1 instead
  of 0. The printed stdout shape is unchanged; transport failures already
  exited 1
- `--dry-run` output for plugin onboarding identifies the plugin action and
  the preserved top-level support components (commands, orchestration,
  scripts, config, configs, tests, docs)

## [2.2.0] - 2026-08-25

### Added
- `gearcore update` command with version-aware refresh for MCP servers,
  skill bundles, and superpowers, followed by self-skill re-sync
- `gearcore onboard` command: discovers MCP scripts and skill bundles in a
  core package directory and registers them in one step
- Parallel MCP backend startup with per-backend timeout and failure tracking;
  one slow or OAuth-blocked backend no longer delays the hub (contract locked
  by regression tests)
- Level-0 skill reveal: `disclosure.core_skills` now also embeds a default-skills
  section into the synced self-skill and inlines full instructions in
  `gearcore list-skills` (spec: docs/superpowers/specs/2026-07-07-level0-skill-reveal-design.md)
- Project landing page at https://blinkvoid.github.io/GearCore/

### Changed
- `gearcore status` caches upstream version lookups for 10 minutes instead of
  hitting the network on every invocation

## [2.1.0] - 2026-05-01

### Fixed
- Clean shutdown: eliminated anyio task-boundary errors during MCP backend teardown
- Conflict resolver no longer mutates backend Tool objects in-place
- `gearcore call` now respects project scope (uses effective config instead of global config)

### Added
- `--verbose` / `-v` flag for debug logging
- Timeouts: 15s for backend startup, 10s for `list_tools`, 60s for tool calls
- Integration verification scripts (`verify_hub.py`, `verify_skills.py`)

### Changed
- Default log level changed from INFO to WARNING for cleaner production output
- Server version synced to 2.1.0
- Process manager uses manual context manager tracking instead of AsyncExitStack for safer cleanup

## [2.0.0] - 2026-03-23

### Changed
- Complete architectural redesign: GearCore is now a **skill** that AI CLI tools discover natively, not an MCP server that clients configure
- Added progressive disclosure: tools hidden until skill is explicitly unlocked
- Added layered configuration: global registry + project-scoped overrides
- Added conflict resolution for overlapping tool names from multiple MCP backends
- Added `gearcore sync` to install self-skill into Claude, Codex, and Kimi

## [1.0.0] - 2026-03-05

### Added
- Initial release as an MCP hub aggregating tools from multiple backends
- Basic stdio and SSE transport support
- Skill bundle loading with SKILL.md discovery
