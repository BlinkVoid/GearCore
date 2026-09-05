---
name: gearcore
description: Unified skill and MCP hub with progressive disclosure and project-scoped context.
---

# GearCore

GearCore is a centralised hub that manages all registered MCPs and skills. It provides
progressive disclosure — tools are hidden until explicitly unlocked — so the context
window stays lean by default.

<!-- GEARCORE:LEVEL0 -->

## When to invoke GearCore

Invoke GearCore when you need to:
- Discover what skills are available in the current context
- Load a skill to get its instructions and tool commands
- Work within a project that has a `.gearcore/` directory (project-scoped context)

## How to invoke

**Check for project context first.** Before invoking, look for a `.gearcore/` directory
in the current working directory or any parent directory.

```
# No project context — global scope, all registered skills available
gearcore

# Project context — scoped to project allowlist + project-local skills
gearcore --project /absolute/path/to/project
```

When working inside a project directory tree that contains `.gearcore/`, always use
`--project <absolute_path>`. This gates disclosure to only the skills and MCPs relevant
to that project.

## Workflow

### 1. Discover available skills
```bash
gearcore list-skills
gearcore --project /absolute/path/to/project list-skills
```
Returns all skills visible in the current context with name, description,
and scope (global or project).

Level-0 default skills are printed in full at the top of the output — read and
follow them without a separate `request-skill` call.

### 2. Load a skill
```bash
gearcore request-skill <skill_name>
gearcore --project /absolute/path/to/project request-skill <skill_name>
```
Prints the skill's instructions (SKILL.md). Read and follow these instructions
to use the skill's capabilities with your native tools.

### 3. Use the skill's tools
Skills that require MCP backends provide tools via `gearcore call`:
```bash
gearcore call <server_id> <tool_name> '<json_args>'
```
Each `request-skill` output lists the exact `gearcore call` commands available.
This is a stateless one-shot invocation — GearCore connects to the backend,
makes the call, prints the result, and exits. Add `--json` for a machine-
readable envelope whose exit code classifies success (0) versus usage (2),
transport (3), MCP tool error (4), and nested command failure (5).

## Project-scoped context

When GearCore is invoked with `--project`, only the following are visible:

- **Global skills** listed in the project's `scope.skills.include` allowlist
- **Project-local skills** from `.gearcore/skills/` (always included)
- **MCPs** listed in the project's `scope.mcp_servers.include` allowlist

This prevents tool pollution across projects — e.g. a web-research skill registered
globally will not appear when working in a backend API project that hasn't allowlisted it.

## Adding new tools

```
# Register a new MCP server (global)
gearcore add-mcp --id <id> --type stdio --command <cmd> [--args ...]

# Onboard a whole core package (register discovered MCP servers and/or skills)
gearcore onboard <path-to-core> [--scope global|project]

# Register an existing skill bundle (global or project)
gearcore --project /path/to/project add-skill /path/to/skill --scope project

# Wrap a traditional CLI program into a skill via CLI-Anything
gearcore add-cli <program> [--scope global|project]
```

## Status check

```
gearcore status
```
Shows active MCP servers, loaded skills, and current context (global or project name).

## Sync self-skill to all AI CLI tools

```
gearcore sync              # auto-detect installed tools (claude, codex, kimi, opencode)
gearcore sync --tool kimi  # specific tool only
gearcore sync --dry-run    # preview without changes
gearcore sync --remove     # unlink from all tools
```

## Refresh registered resources

```
gearcore update                        # update everything, then re-sync self-skill
gearcore update mcp <id>               # refresh one MCP server from its source
gearcore update skill <name>           # refresh one skill bundle
gearcore update superpowers            # refresh vendored superpowers skills
gearcore update --dry-run              # preview pending changes without applying
```
