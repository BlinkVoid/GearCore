# Changelog

All notable changes to GearCore will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- `gearcore sync` now targets OpenCode: symlinks the self-skill into
  `~/.config/opencode/skills/gearcore/` and auto-detects `opencode` on PATH
- Level-0 skill reveal: `disclosure.core_skills` now also embeds a default-skills
  section into the synced self-skill and inlines full instructions in
  `gearcore list-skills` (spec: docs/superpowers/specs/2026-07-07-level0-skill-reveal-design.md)
- Version-3 capability profiles with cwd-independent defaults, constrained
  workers, include/deny policy, and protected global MCP/skill bindings
- Canonical Ed25519 launch envelopes with issuer-bound public-key documents,
  expiry checks, authoritative worker selection, and no-broader alternates
- Owner-controlled credential references and authenticated stdio, SSE, and
  streamable-HTTP client transports
- Atomic, idempotent `profile-set` and stable redacted status fields for live
  activation gates

### Security
- Invalid explicit envelopes and missing credentials fail closed without
  default-profile fallback or backend startup
- Capability conformance coverage now exercises status, skill discovery,
  requests, one-shot calls, and serve across stdio/SSE/HTTP, including hostile
  dispatcher definitions and project collisions
- Configuration/auth models, diagnostics, exceptions, status, and transport
  cleanup were hardened against plaintext secret retention and definition leaks

### Fixed
- `add-mcp --args` is now an explicit final-option delimiter, so dash-prefixed
  child argv is preserved without ambiguity
- Self-skill sync now renders level-0 skills from the already-selected effective
  v3 profile instead of reloading legacy global disclosure

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
