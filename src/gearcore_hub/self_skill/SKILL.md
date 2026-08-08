---
name: gearcore
description: Unified skill and MCP hub with progressive disclosure, capability profiles, protected global bindings, and project-scoped context.
---

# GearCore

GearCore is a centralised hub that manages registered MCPs and skills. It provides
progressive disclosure and applies one effective capability profile before any
backend starts, keeping the context window lean and constrained sessions narrow.

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
makes the call, prints the result, and exits.

## Project-scoped context

For a legacy version-2 configuration, `--project` exposes:

- **Global skills** listed in the project's `scope.skills.include` allowlist
- **Project-local skills** from `.gearcore/skills/` (always included)
- **Global MCPs** listed in the project's `scope.mcp_servers.include` allowlist
- **Project-local MCP definitions**, which are exposed and shadow a same-ID
  global definition

Those shadowing semantics describe a wholly v2 setup. When the global policy is
v3, its protected global binding still takes precedence even if the project file
is v2.

For version 3, the selected global profile applies first and a same-name project
profile entry can narrow it with `include` and `deny`. Protected global MCP and
skill bindings survive project omissions, denies, and collisions. A project
cannot select a profile, set a default, or declare protection.

Project-local capabilities are not unconditionally visible under v3: project
includes filter them, global/profile and project denies apply, and a constrained
or envelope-enforced launch applies its profile include as the capability
ceiling. Protected collisions still resolve to the trusted global binding.

The default `operator` profile is suitable for a human-started CLI. A
HIVE-started worker must use a signed `constrained: true` profile envelope and
must not receive operator-only capabilities such as `hive-dispatcher`. Profiles
are defense-in-depth policy; authenticated MCP servers and launcher containment
are the hostile-process boundary.

## Launch policy

Global launch options must precede the subcommand:

```bash
gearcore --profile operator status
gearcore \
  --context-envelope /run/hive/launch-envelope.json \
  --envelope-public-key /etc/hive/worker-envelope-public-key.json \
  status
```

A valid envelope is authoritative. Invalid explicit envelope input is
diagnostic-only and never falls back to the default operator. `status` exposes
stable `profile`, `source`, `enforced_profile`, `constrained`, active, denied,
protected, and diagnostics fields without backend definitions or secrets.

## Adding new tools

```
# Register a new MCP server (global)
gearcore add-mcp --id <id> --type stdio --command <cmd> [--args ...]

# --args is the final GearCore option; all following tokens are child argv

# Create or replace a global capability profile (repeat options as needed)
gearcore profile-set <name> [--mcp-include <id>] [--mcp-deny <id>] \
  [--mcp-protect <id>] [--skill-include <name>] [--skill-deny <name>] \
  [--skill-protect <name>] [--core-skill <name>] \
  [--constrained] [--default]

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

Authenticated MCP configuration contains only a `credential_ref`. Credential
values live in owner-only, non-symlink files under
`~/.config/gearcore/credentials/` and must never be placed in YAML, arguments,
configured `env` entries, URLs, or the ambient parent environment. The sole
stdio exception is GearCore materializing the referenced value at transport
start into the named private child environment without mutating `os.environ` or
retained configuration; temporary parameter/mapping references are not retained
after child creation.

## Sync self-skill to all AI CLI tools

```
gearcore sync              # auto-detect installed tools (claude, codex, kimi, opencode)
gearcore sync --tool kimi  # specific tool only
gearcore sync --dry-run    # preview without changes
gearcore sync --remove     # unlink from all tools
```
