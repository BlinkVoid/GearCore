---
name: gearcore
description: Unified skill and MCP hub with progressive disclosure and project-scoped context.
---

# GearCore

GearCore is a centralised hub that manages all registered MCPs and skills. It provides
progressive disclosure — tools are hidden until explicitly unlocked — so the context
window stays lean by default.

## When to invoke GearCore

Invoke GearCore when you need to:
- Discover what tools or skills are available
- Unlock a specific skill to gain access to its tools
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
After invoking, call:
```
list_skills
```
Returns all skills visible in the current context with name, description, category,
and scope (global or project).

### 2. Unlock a skill
```
request_skill <skill_name>
```
Returns the SKILL.md instructions for that skill and makes its tools available
in subsequent `list_tools` calls.

### 3. Use the tools
Once a skill is activated, its tools appear in `list_tools`. Call them directly.

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

# Register an existing skill bundle (global or project)
gearcore add-skill /path/to/skill [--scope project --project /path/to/project]

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
gearcore sync              # auto-detect installed tools (claude, codex, kimi)
gearcore sync --tool kimi  # specific tool only
gearcore sync --dry-run    # preview without changes
gearcore sync --remove     # unlink from all tools
```
