# GearCore: Skill Bundle Schema (v2)

Skill bundles are the primary unit of organization in GearCore. Each bundle packages procedural instructions (`SKILL.md`) with optional metadata (`manifest.json`) to define a scoped capability.

---

## Directory Layout

```
skills/
└── web-research/
    ├── SKILL.md          # Required — procedural instructions for the AI
    ├── manifest.json     # Optional — metadata, MCP mappings, activation config
    ├── scripts/          # Optional — executable hooks (validation, transforms)
    └── resources/        # Optional — templates, reference data
```

A valid skill bundle requires only a `SKILL.md` file. If `manifest.json` is missing, GearCore synthesises a minimal manifest using the directory name.

---

## SKILL.md

The primary file. Contains instructions the AI receives when the skill is activated via `request_skill`.

### Format

```markdown
---
name: web-research
description: Deep web research with source validation
---

# Web Research

## Overview
Workflow for high-signal technical research.

## Procedure
1. Use `browser_navigate` for initial discovery
2. Take screenshots to verify page content
3. Extract relevant information
4. Synthesize with citations

## Constraints
- Prefer official documentation over community blogs
- Close browser tabs after extraction
```

The YAML frontmatter (`name`, `description`) is optional. If present, it takes priority over `manifest.json` fields for tools that read `SKILL.md` directly (e.g. Kimi, Codex).

### Supported skill types

| Type | Description |
|------|-------------|
| `standard` | Default. Instructions as prose/steps. |
| `flow` | Contains a mermaid or d2 flowchart that some tools (Kimi) can execute as a structured workflow. |

---

## manifest.json

Optional metadata file. Used by GearCore for MCP tool mapping, conflict resolution, and activation strategy.

```json
{
  "name": "web-research",
  "version": "1.0.0",
  "description": "Web browsing and research via Playwright browser automation.",
  "category": "research",
  "mcp_servers": [
    {
      "server_id": "playwright",
      "tools": ["browser_navigate", "browser_screenshot", "browser_click", "browser_type"]
    }
  ],
  "scripts": [
    {
      "name": "validate_source",
      "path": "scripts/validate.py",
      "runtime": "python3"
    }
  ],
  "activation": {
    "strategy": "manual",
    "triggers": ["web", "browser", "research", "scrape"]
  }
}
```

### Fields

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | yes | Skill identifier (must match directory name by convention) |
| `version` | string | no | Semver version (default: `1.0.0`) |
| `description` | string | no | One-line description shown in `list_skills` |
| `category` | string | no | Grouping label (default: `general`) |
| `mcp_servers` | list | no | MCP server + tool bindings for progressive disclosure |
| `scripts` | list | no | Executable hooks (not yet invoked — reserved) |
| `activation` | dict | no | Activation strategy and trigger keywords |

#### `mcp_servers[]`

Maps this skill to specific tools on specific MCP backends. When the skill is activated, only these tools become visible in `list_tools`.

```json
{
  "server_id": "filesystem",
  "tools": ["read_file", "write_file", "list_directory"]
}
```

#### `activation`

| Field | Type | Description |
|-------|------|-------------|
| `strategy` | string | `manual` (default) — requires explicit `request_skill`. `auto` — loaded automatically as a core skill. `semantic` — reserved for future LLM-based activation. |
| `triggers` | list[string] | Keywords for future semantic matching |

---

## Skill Placement

| Location | Scope | Visibility |
|----------|-------|------------|
| `~/.config/gearcore/skills/<name>/` | Global | Always visible (unless filtered by project allowlist) |
| `~/.config/agents/skills/<name>/` | Global (shared) | Same as above; also discoverable by Kimi natively |
| `<project>/.gearcore/skills/<name>/` | Project-local | Only visible when GearCore is invoked with this project context |

---

## How Skills Relate to MCP Tools

Skills don't replace MCP tools — they **gate and organize** them:

```
Skill "code-ops" → activates → filesystem MCP tools (read_file, write_file, ...)
Skill "memory"   → activates → sample-memory MCP tools (mem_query, mem_store, ...)
```

Without skill activation, MCP tools remain registered but invisible to the AI. This is the progressive disclosure mechanism — the AI sees a lean bootstrap toolset and unlocks capabilities on demand.
