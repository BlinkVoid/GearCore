# GearCore

[![Python 3.13+](https://img.shields.io/badge/python-3.13+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![MCP](https://img.shields.io/badge/MCP-1.26+-green.svg)](https://modelcontextprotocol.io)

**One skill to rule them all.**

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

## Features

- **🎭 Appears as a native skill** — AI tools invoke `gearcore` directly via their skill discovery. No MCP config duplication.
- **🔒 Progressive disclosure** — Tools stay hidden until you explicitly unlock a skill via `request_skill`. Your context window stays clean.
- **📁 Project scoping** — Each project can allowlist only the skills it needs via `.gearcore/config.yaml`.
- **🛡️ Capability profiles** — A default operator and signed constrained workers get separate, cwd-independent MCP and skill policies.
- **🔐 Protected and authenticated capabilities** — Trusted global bindings survive project collisions, while credentials remain file-backed references and are materialized only at transport start.
- **🧠 Core reasoning discipline** — Auto-activated zero-tool skills (like `first-principles-scientific-mindset`) set default reasoning norms without adding tool noise.
- **⓪ Level-0 skills** — `disclosure.core_skills` marks skills revealed by default: `list-skills` prints their full instructions and `sync` embeds them into the self-skill.
- **⚔️ Conflict resolution** — When multiple MCP servers expose the same tool name, GearCore deduplicates, namespaces, or unifies them automatically.
- **🔄 One sync for installed tools** — `gearcore sync` links the self-skill only to clients selected with `--tool` or detected on `PATH`.

## Installation

Requires **Python 3.13+** and **[uv](https://docs.astral.sh/uv/)**.

```bash
# Install the CLI
uv tool install git+https://github.com/BlinkVoid/GearCore.git

# Or install from local source
uv tool install /path/to/GearCore

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

For `add-mcp`, `--args` is a delimiter and must be the final GearCore option.
Place `--env`, `--scope`, `--disabled`, and every other GearCore option before
it. Every following token is stored as child argv verbatim—even `--help`,
`--scope`, another `--args`, or `--`. Attached `--args=VALUE` is also supported:
`VALUE` becomes the first child argument and the following remainder stays child
argv. Thus `--args=--help` passes `--help`, while `--args=` passes an empty first
child argument. A detached empty argument (`--args ""`) is preserved too.

### 3. Register a skill bundle

```bash
# A skill is just a directory with SKILL.md + manifest.json
gearcore add-skill /path/to/my-skill
```

### 4. See what's available

```bash
gearcore list-skills
# GearCore skills (global context):
#   web-research — Web browsing and research via Playwright
#   filesystem — Secure filesystem access
#   memory — Persistent memory via MemCore
```

### 5. AI tools use it

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
    │filesystem│  │playwright│  │ memcore  │
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

<project>/.gearcore/
  config.yaml          ← project: allowlist subset, project-local MCP defs, overrides, context name
  skills/              ← project-local skills (legacy v2: included in project)
```

**Version 2 resolution:** built-in defaults → global → project. Projects narrow
global scope via allowlists. Project-local definitions are visible only in that
project, and a project MCP definition can override a same-ID global definition.

**Version 3 resolution:** the globally selected profile applies first. Project
v3 overlays can only narrow its MCP and skill policies. Global/profile and
project denies apply to project-local IDs, and a v3 project `include` filters
them. An unconstrained operator profile can still admit a project-local addition
unless a project overlay narrows it; constrained or envelope-enforced launches
apply the profile include as a capability ceiling, and an approved alternate
under an envelope also receives the enforced skill-binding ceiling. A protected
global MCP or skill remains pinned to the trusted global binding even when a
v2/v3 project omits, denies, or collides with it. See
[CONFIG_SCHEMA.md](CONFIG_SCHEMA.md) for the complete migration and precedence
contract.

## Operator and constrained worker profiles

A v3 configuration can make a human-started GearCore session an operator while
requiring a signed envelope for a constrained worker:

```yaml
version: 3
registry:
  mcp_servers:
    - id: hive-dispatcher
      type: stdio
      command: /opt/hive/bin/dispatcher-mcp
      args: [serve]
      auth:
        credential_ref: hive-dispatcher-operator
        stdio_environment: HIVE_DISPATCHER_CREDENTIAL
    - id: hive-gateway
      type: http
      url: http://127.0.0.1:8765/mcp
      auth:
        credential_ref: hive-worker-gateway
        http_scheme: bearer
profiles:
  default: operator
  entries:
    operator:
      scope:
        mcp_servers:
          include: [hive-dispatcher, hive-gateway]
          protected: [hive-dispatcher]
        skills:
          include: [chrono-core, hive-dispatcher]
          protected: [hive-dispatcher]
      disclosure:
        core_skills: [chrono-core, hive-dispatcher]
    hive-worker:
      constrained: true
      scope:
        mcp_servers:
          include: [hive-gateway]
          deny: [hive-dispatcher]
        skills:
          include: [hive-worker, chrono-core]
          deny: [hive-dispatcher]
      disclosure:
        core_skills: [hive-worker, chrono-core]
```

Profiles are policy selection and defense in depth. The authenticated MCP server
and HIVE launcher containment are the hostile-process boundary. This GearCore
feature is the dependency that a HIVE integration can consume; it does not by
itself activate envelope issuance or worker containment in HIVE.

Create profiles idempotently with repeatable options:

```bash
gearcore profile-set operator \
  --mcp-include hive-dispatcher --mcp-include hive-gateway \
  --mcp-protect hive-dispatcher \
  --skill-include hive-dispatcher --skill-include chrono-core \
  --skill-protect hive-dispatcher \
  --core-skill chrono-core --core-skill hive-dispatcher \
  --default

gearcore profile-set hive-worker \
  --mcp-include hive-gateway --mcp-deny hive-dispatcher \
  --skill-include hive-worker --skill-include chrono-core \
  --skill-deny hive-dispatcher \
  --core-skill hive-worker --core-skill chrono-core --constrained
```

`profile-set` is global-only, validates protected/core definitions, replaces the
named profile atomically, and prints `unchanged` when the requested state already
exists. The operator example requires trusted global `chrono-core` and
`hive-dispatcher` skill bundles plus an enabled global `hive-dispatcher` MCP
definition before it runs.

## Signed launches and credentials

A trusted launcher passes both files; all launch-policy options precede the
subcommand:

```bash
gearcore \
  --config /etc/gearcore/config.yaml \
  --context-envelope /run/hive/launch-envelope.json \
  --envelope-public-key /etc/hive/worker-envelope-public-key.json \
  status
```

The key document is exactly `{ "version": 1, "issuer": "...",
"public_key": "<base64url Ed25519 key>" }`. The signature covers the envelope's
`version`, `profile`, `issuer`, `launch_id`, `execution_id`, `task_id`,
`issued_at`, `expires_at`, and `nonce` using sorted, compact JSON. A valid
envelope is authoritative. An accompanying `--profile` is accepted only when its
effective capabilities and bindings are a subset of the signed profile.

Invalid explicit envelope input never falls back to the operator. Status becomes
diagnostic-only, `call` exits non-zero, and `serve` starts no backend.

Credentials live by default at
`~/.config/gearcore/credentials/<credential_ref>`. Use an owner-controlled,
non-group/other-writable directory (normally `0700`) and regular, non-symlink,
owner-only files (normally `0600`). YAML holds only `credential_ref` plus either
`stdio_environment` for stdio or `http_scheme: bearer` for SSE/HTTP. Never put a
token in YAML, CLI arguments, configured `env`, URLs, or the ambient parent
environment. The only stdio exception is GearCore resolving the reference at
backend start into the named private child environment; it does not mutate
`os.environ` or retained configuration and does not retain its temporary
parameter/mapping references after child creation. Missing or unsafe credentials
fail closed.

## CLI Reference

| Command | Description |
|---------|-------------|
| `gearcore list-skills` | List available skills in current context |
| `gearcore request-skill <name>` | Unlock a skill and expose its tools |
| `gearcore call <server> <tool> '<json>'` | One-shot tool invocation on an MCP backend |
| `gearcore status` | Show effective config and running context |
| `gearcore serve` | Run the MCP hub (used automatically by AI tools) |
| `gearcore profile-set <name>` | Atomically create or replace a global v3 profile (`--mcp-*`, `--skill-*`, `--core-skill`, `--constrained`, `--default`) |
| `gearcore add-mcp` | Register a new MCP server (`--scope project` for a project-local def, add `--allowlist` to allowlist an existing global server instead) |
| `gearcore add-skill <path>` | Register a skill bundle |
| `gearcore add-cli <program>` | Wrap a CLI program into a skill |
| `gearcore remove mcp\|skill <name>` | Remove an MCP or skill |
| `gearcore sync` | Install self-skill to Claude / Codex / Kimi / OpenCode |

Runtime commands accept `--project <path>`, `--config <path>`, `--profile <name>`,
`--context-envelope <path>`, `--envelope-public-key <path>`, and `-v`. Global
options precede the subcommand. `profile-set` rejects `--project` because profile
authority and protection are global-only.

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
