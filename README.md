# GearCore

**Unified skill and MCP hub with progressive disclosure and project-scoped context.**

GearCore is a CLI tool that acts as a single entry point for all your registered MCP servers and skill bundles. Instead of copying the same MCP config into Claude, Codex, and Kimi, you register everything once in GearCore and expose it as a single skill that all three tools discover natively.

## Key Ideas

- **Appears as a skill**, not an MCP — AI CLI tools (Claude Code, Codex, Kimi) invoke `gearcore` directly via their native skill discovery
- **Progressive disclosure** — tools are hidden until a skill is explicitly unlocked via `request_skill`, keeping the context window lean
- **Core reasoning discipline** — lightweight auto-activated skills can set default reasoning norms without exposing extra tools
- **Layered config** — global registry at `~/.config/gearcore/config.yaml`, project overrides at `<project>/.gearcore/config.yaml`
- **Project scoping** — each project allowlists only the skills/MCPs relevant to it; project-local skills live in `.gearcore/skills/`
- **Conflict resolution** — deduplicates, namespaces, or unifies overlapping tools from multiple MCP servers

## Install

```bash
# Requires Python 3.13+ and uv
uv tool install /path/to/GearCore

# Install the self-skill to all detected AI CLI tools
gearcore sync
```

## Quick Start

```bash
# Check effective config
gearcore status

# Register an MCP server
gearcore add-mcp --id filesystem --type stdio --command npx --args -y @modelcontextprotocol/server-filesystem /home/user/workspace

# Register a skill bundle
gearcore add-skill /path/to/my-skill

# Wrap a traditional CLI program into a skill
gearcore add-cli ffmpeg

# Sync self-skill to claude/codex/kimi
gearcore sync
```

## How AI Tools Use GearCore

Once `gearcore sync` has run, the self-skill is installed into `~/.claude/skills/`, `~/.codex/skills/`, and `~/.kimi/skills/` (via symlinks from the canonical `~/.config/agents/skills/gearcore/`).

When an AI tool loads the gearcore skill, it:

1. Checks if the current project has a `.gearcore/` directory
2. Invokes `gearcore --project <path>` (scoped) or `gearcore` (global)
3. Calls `list_skills` to see what's available
4. Calls `request_skill <name>` to unlock a skill and get its instructions + tools

## Layered Configuration

```
~/.config/gearcore/
  config.yaml          ← global: all MCPs, all skills, disclosure + resolution rules
  skills/              ← global skill bundles

<project>/.gearcore/
  config.yaml          ← project: allowlist subset, overrides, project context
  skills/              ← project-local skills (only visible within this project)
```

**Resolution order:** global → project (project narrows via allowlist, never widens).

When invoked with `--project`, only the allowlisted globals + project-local skills are visible. Without `--project`, everything in the global registry is visible.

See [CONFIG_SCHEMA.md](CONFIG_SCHEMA.md) for the full specification.

## Project Structure

```
src/gearcore_hub/
  main.py              ← CLI entry point + serve (MCP hub runtime)
  config.py            ← layered config loader (global → project)
  skill_manager.py     ← two-phase skill loading with visibility gating
  process_manager.py   ← shared MCP server process lifecycle
  conflict_resolver.py ← tool deduplication and namespacing
  registry.py          ← add-mcp, add-skill, add-cli, remove commands
  sync.py              ← self-skill install/symlink to AI CLI tools
  self_skill/          ← SKILL.md + manifest.json for GearCore itself
```

## CLI Reference

| Command | Description |
|---------|-------------|
| `gearcore list-skills` | List available skills in current context |
| `gearcore request-skill <name>` | Print a skill's instructions (SKILL.md) |
| `gearcore call <server> <tool> '<json>'` | Invoke a tool on an MCP backend (stateless) |
| `gearcore status` | Show effective config and context |
| `gearcore serve` | Run the MCP hub (fallback for non-skill clients) |
| `gearcore add-mcp` | Register a new MCP server |
| `gearcore add-skill <path>` | Register a skill bundle |
| `gearcore add-cli <program>` | Wrap a CLI via [CLI-Anything](https://github.com/HKUDS/CLI-Anything) |
| `gearcore remove mcp\|skill <name>` | Remove an MCP or skill |
| `gearcore sync` | Install self-skill to AI CLI tools |

All commands accept `--project <path>` for project-scoped context.

## Documentation

- [DESIGN_RATIONALE.md](DESIGN_RATIONALE.md) — why skill-first, not MCP-first
- [ARCHITECTURE.md](ARCHITECTURE.md) — system design and data flow
- [CONFIG_SCHEMA.md](CONFIG_SCHEMA.md) — config file specification (global + project)
- [SKILL_SCHEMA.md](SKILL_SCHEMA.md) — skill bundle format
- [CONFLICT_RESOLUTION.md](CONFLICT_RESOLUTION.md) — deduplication and namespacing strategy
- [RESEARCH.md](RESEARCH.md) — background research and problem analysis

## Included Core Skill

`first-principles-scientific-mindset` is included as a zero-tool core skill. It keeps
default reasoning anchored on deriving from fundamentals, making assumptions explicit,
forming falsifiable hypotheses, and validating conclusions against evidence.
