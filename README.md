# GearCore

[![Python 3.13+](https://img.shields.io/badge/python-3.13+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![PyPI](https://img.shields.io/pypi/v/gearcore.svg)](https://pypi.org/project/gearcore/)
[![MCP](https://img.shields.io/badge/MCP-1.26+-green.svg)](https://modelcontextprotocol.io)

**One skill to rule them all.**

> 🌐 [Project website](https://blinkvoid.github.io/GearCore/)

GearCore is a unified skill and MCP hub that aggregates all your AI tools behind a single, progressively-disclosed interface. Instead of copying the same MCP server configs into Claude, Codex, Kimi, and every new project, you register everything **once** in GearCore and expose it as one native skill that every AI CLI tool discovers automatically.

```
┌─────────────────────────────────────────────────────────────┐
│  BEFORE: Context window bloat                               │
│                                                             │
│  Claude: 12 MCP servers × 200 tokens each = 2,400 tokens   │
│  Kimi:   Same 12 configs, copied again                     │
│  Codex:  Same 12 configs, copied again                     │
│  Every project: repeat                                     │
└─────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────┐
│  AFTER: GearCore                                            │
│                                                             │
│  Claude → gearcore skill → request_skill("web-research")   │
│  Kimi   → gearcore skill → request_skill("filesystem")     │
│  Codex  → gearcore skill → request_skill("memory")         │
│                                                             │
│  Tools hidden until needed. Context window stays lean.     │
└─────────────────────────────────────────────────────────────┘
```

*The numbers above are illustrative, not measured. The structural claim —
one config instead of N copies per tool per project — is what matters;
exact token costs depend on each client's MCP schema serialization.*

## Features

- **🎭 Appears as a native skill** — AI tools invoke `gearcore` directly via their skill discovery. No MCP config duplication.
- **🔒 Progressive disclosure** — Tools stay hidden until you explicitly unlock a skill via `request_skill`. Your context window stays clean.
- **📁 Project scoping** — Each project can allowlist only the skills it needs via `.gearcore/config.yaml`.
- **🧠 Core reasoning discipline** — Auto-activated zero-tool skills (like `first-principles-scientific-mindset`) set default reasoning norms without adding tool noise.
- **⓪ Level-0 skills** — `disclosure.core_skills` marks skills revealed by default: `list-skills` prints their full instructions and `sync` embeds them into the self-skill.
- **⚔️ Conflict resolution** — When multiple MCP servers expose the same tool name, GearCore deduplicates, namespaces, or unifies them automatically.
- **🔄 One sync to all tools** — `gearcore sync` installs the self-skill into Claude, Codex, Kimi, and OpenCode in one command.

## Client support

Verified end-to-end on 2026-08-25: `gearcore sync` installs the self-skill into each client's
skill-discovery directory (symlink to a canonical copy), the link resolves, and the skill loads.
The MCP hub handshake was verified with a live stdio client (`verify_hub.py`).

| Client | Install target | Discovery | Hub handshake |
|---|---|---|---|
| Claude Code | `~/.claude/skills/gearcore` | ✅ | ✅ |
| Codex CLI | `~/.codex/skills/gearcore` | ✅ | ✅ |
| Kimi CLI | `~/.kimi/skills/gearcore` | ✅ | ✅ |
| OpenCode | `~/.config/opencode/skills/gearcore` | ✅ | ✅ |

## Installation

Requires **Python 3.13+** and **[uv](https://docs.astral.sh/uv/)**.

```bash
# Install the CLI
uv tool install gearcore

# Install the self-skill into Claude, Codex, Kimi, OpenCode
gearcore sync
```

## Quick Start

### 1. Check your setup

```bash
gearcore status
```

### 2. Register an MCP server

```bash
# Filesystem access
gearcore add-mcp --id filesystem --type stdio \
  --command npx --args -y @modelcontextprotocol/server-filesystem /home/user/workspace

# Web research via Playwright
gearcore add-mcp --id playwright --type stdio \
  --command npx --args -y @playwright/mcp
```

### 3. Register a skill bundle

```bash
# A skill is just a directory with SKILL.md + manifest.json
gearcore add-skill /path/to/my-skill
```

### 4. Or onboard a whole core package or plugin

```bash
gearcore onboard /path/to/core
```

Discovers `skills/*/SKILL.md` and MCP scripts in `pyproject.toml`, then registers what is found.

If the directory is a **Codex-compatible plugin root** (contains
`.codex-plugin/plugin.json`), the whole plugin is registered instead: the
plugin — including sibling `commands`, `orchestration`, `scripts`, `config`,
`configs`, `tests`, and `docs` — is linked into
`~/.config/gearcore/plugins/<name>` (or `<project>/.gearcore/plugins/<name>`
with `--scope project`), and its skills are registered through the installed
plugin root. `--copy-skills` copies the whole plugin instead of symlinking;
`--dry-run` previews the plan and the support components that will be
preserved. See [PLUGIN_SCHEMA.md](PLUGIN_SCHEMA.md).

> GearCore preserves plugin commands, orchestration, scripts, configs, tests,
> and docs but does not auto-execute arbitrary plugin content.

```bash
# Uninstall a plugin: removes the registration and its skill links only
gearcore remove plugin <name>
```

### 5. Updating resources

```bash
# Update everything (MCP servers, skills, superpowers, self-skill sync)
gearcore update

# Update a single MCP server or skill
gearcore update mcp sample-prompts
gearcore update skill memory

# Preview changes without applying
gearcore update --dry-run
```

### 6. See what's available

```bash
gearcore list-skills
# GearCore skills (global context):
#   web-research — Web browsing and research via Playwright
#   filesystem — Secure filesystem access
#   memory — Persistent memory via SampleMemory
```

### 7. AI tools use it

Once synced, Kimi/Claude/Codex/OpenCode loads GearCore as a skill and follows this flow:

```
AI: gearcore list-skills
→ sees: web-research, filesystem, memory

AI: gearcore request-skill web-research
→ SKILL.md injected into context
→ Playwright tools unlocked: browser_navigate, browser_click, ...

AI: gearcore call playwright browser_navigate '{"url": "https://example.com"}'
→ result returned
```

## How It Works

```
┌─────────────┐   ┌─────────────┐   ┌─────────────┐
│ Claude Code │   │  Codex CLI  │   │  Kimi CLI   │
└──────┬──────┘   └──────┬──────┘   └──────┬──────┘
       │                 │                 │
       └─────────┬───────┴─────────┬───────┘
                 ▼                 ▼
        ┌─────────────────────────────────┐
        │   ~/.config/agents/skills/      │
        │         gearcore/SKILL.md       │
        └─────────────────┬───────────────┘
                          │
                          ▼
              ┌───────────────────────┐
              │    GearCore CLI       │
              │  ┌─────────────────┐  │
              │  │  Config Loader  │  │  ← global + project layers
              │  │  Skill Manager  │  │  ← visibility gating
              │  │ Process Manager │  │  ← shared MCP backends
              │  │Conflict Resolver│  │  ← dedup / namespace
              │  └─────────────────┘  │
              └───────────┬───────────┘
                          │
          ┌───────────────┼───────────────┐
          ▼               ▼               ▼
    ┌──────────┐  ┌──────────┐  ┌──────────┐
    │filesystem│  │playwright│  │ sample-memory  │
    │  (stdio) │  │  (stdio) │  │  (sse)   │
    └──────────┘  └──────────┘  └──────────┘
```

### Progressive Disclosure Flow

```
gearcore serve starts
    ↓
list_tools → returns only: list_skills, request_skill (bootstrap)
    ↓
AI calls list_skills → sees available skills
    ↓
AI calls request_skill("web-research") → SKILL.md injected, tools unlocked
    ↓
list_tools → now includes browser_navigate, browser_click, ...
```

### Layered Configuration

```
~/.config/gearcore/
  config.yaml          ← global: all MCPs, all skills, disclosure rules
  skills/              ← global skill bundles
  plugins/             ← global registered plugins (whole-plugin onboarding)

<project>/.gearcore/
  config.yaml          ← project: allowlist subset, project-local MCP defs, overrides, context name
  skills/              ← project-local skills (always visible in project)
  plugins/             ← project-registered plugins
```

**Resolution order:** built-in defaults → global → project. Projects *narrow* global scope via allowlists. Project-local definitions (`.gearcore/skills/` and project `registry.mcp_servers`) are always visible in that project, never outside it; a project MCP def overrides a global one with the same id.

## CLI Reference

| Command | Description |
|---------|-------------|
| `gearcore list-skills` | List available skills in current context |
| `gearcore request-skill <name>` | Unlock a skill and expose its tools |
| `gearcore call <server> <tool> '<json>'` | One-shot tool invocation on an MCP backend (add `--json` for structured machine output) |
| `gearcore status` | Show effective config and running context |
| `gearcore serve` | Run the MCP hub (used automatically by AI tools) |
| `gearcore add-mcp` | Register a new MCP server (`--scope project` for a project-local def, add `--allowlist` to allowlist an existing global server instead) |
| `gearcore add-skill <path>` | Register a skill bundle |
| `gearcore onboard <core-path>` | Discover and register MCP servers and/or skills from a core package; registers whole Codex-compatible plugins (`.codex-plugin/plugin.json`) at the scope's plugins dir |
| `gearcore add-cli <program>` | Wrap a CLI program into a skill |
| `gearcore remove mcp\|skill\|plugin <name>` | Remove an MCP, skill, or plugin registration |
| `gearcore sync` | Install self-skill to Claude / Codex / Kimi / OpenCode |
| `gearcore update [mcp\|skill\|superpowers] [name]` | Version-aware refresh of registered resources, then re-sync |

All commands accept `--project <path>` for project-scoped context and `-v` for verbose output.

### Structured output for `call`

`gearcore call --json` (schema `gearcore.call/1`) prints exactly one
deterministic JSON envelope to stdout and sends diagnostics to stderr. Shell
automation classifies outcomes via `status`/`ok` and the exit code:

| Outcome | `status` | Exit |
|---------|----------|------|
| Tool succeeded | `success` | 0 |
| Unknown/disabled server, bad JSON args | `usage_error` | 2 |
| Backend failed to start or transport broke | `transport_error` | 3 |
| MCP result `isError` | `mcp_tool_error` | 4 |
| `devcore` server command tool returned `ok: false` | `nested_command_failure` | 5 |

Example envelope (pretty-printed here; actual output is minified):

```json
{
  "schema": "gearcore.call/1",
  "server": "devcore",
  "tool": "devcore_run",
  "ok": false,
  "status": "nested_command_failure",
  "mcp_is_error": false,
  "content": [
    {"type": "text", "text": "{\"ok\": false, \"exit_code\": 1, ...}"}
  ],
  "structured_content": null
}
```

Content blocks are normalized in order: text is preserved verbatim; image,
audio, and binary resource payloads are represented by type, media type, byte
length, and sha256 digest (raw binary is never printed); resource links keep
their metadata; `structuredContent` from the backend is passed through as
`structured_content` without stringification.

The nested adapter applies only to the server id `devcore` and the exact
command tools `devcore_run` and `devcore_poll`, and only when the result is a
single JSON text object satisfying the DevCore run contract
(`ok`/`exit_code`/`timed_out`/`elapsed_seconds` with
`ok == (exit_code == 0 and not timed_out)`). A generic domain tool's `ok`
field is never interpreted on any server.

Legacy text mode (without `--json`) keeps its historical stdout shape. Its
documented behavior change: MCP tool errors and nested DevCore command
failures now exit with code 1 (they previously exited 0); transport failures
already exited 1.

## Writing a Skill

A skill bundle is just a directory with two files:

```
my-skill/
  SKILL.md       ← instructions for the AI (markdown + YAML frontmatter)
  manifest.json  ← metadata: name, description, MCP server mappings
```

**SKILL.md:**

```markdown
---
name: my-skill
description: What this skill does
---

# My Skill

When the user asks about X, do Y.

## Tools

Use `gearcore call my-server <tool> '<args>'` to invoke tools.
```

**manifest.json:**

```json
{
  "name": "my-skill",
  "version": "1.0.0",
  "description": "What this skill does",
  "category": "general",
  "mcp_servers": [
    {
      "server_id": "my-server",
      "tools": ["tool_a", "tool_b"]
    }
  ]
}
```

See [SKILL_SCHEMA.md](SKILL_SCHEMA.md) for the full specification.

## Documentation

- [ARCHITECTURE.md](ARCHITECTURE.md) — system design and data flow
- [CONFIG_SCHEMA.md](CONFIG_SCHEMA.md) — config file specification
- [SKILL_SCHEMA.md](SKILL_SCHEMA.md) — skill bundle format
- [PLUGIN_SCHEMA.md](PLUGIN_SCHEMA.md) — Codex-compatible plugin format and onboarding
- [CONFLICT_RESOLUTION.md](CONFLICT_RESOLUTION.md) — deduplication strategy
- [DESIGN_RATIONALE.md](DESIGN_RATIONALE.md) — why skill-first, not MCP-first

## Development

```bash
# Clone
git clone https://github.com/BlinkVoid/GearCore.git
cd GearCore

# Install in editable mode
uv pip install -e ".[dev]"

# Run integration tests
uv run python verify_hub.py
uv run python verify_skills.py

# Run the hub manually
uv run gearcore serve
```

## License

[MIT](LICENSE)
