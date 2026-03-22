# GearCore: Configuration Schema (v2)

GearCore uses a layered YAML configuration system. The global config is the source of truth for all registrations. Project configs narrow scope via allowlists.

---

## File Locations

| Layer | Path | Purpose |
|-------|------|---------|
| Global | `~/.config/gearcore/config.yaml` | Full registry of MCPs, skill dirs, disclosure rules, resolution rules |
| Project | `<project>/.gearcore/config.yaml` | Allowlist subset, disclosure overrides, project context metadata |

---

## Global Config (`~/.config/gearcore/config.yaml`)

```yaml
version: 2

registry:
  mcp_servers:
    - id: filesystem
      type: stdio                # stdio | sse | http
      command: npx
      args:
        - -y
        - "@modelcontextprotocol/server-filesystem"
        - /home/user/workspace
      env:                       # optional environment variables
        KEY: value
      enabled: true              # false to keep registered but not started

    - id: hive-gateway
      type: sse
      url: http://127.0.0.1:7111/sse
      enabled: true

  skills_dirs:
    - ~/.config/gearcore/skills  # global user skills
    - ~/.config/agents/skills    # shared with kimi / other tools

disclosure:
  strategy: manual               # manual | semantic (semantic not yet implemented)
  activation_threshold: 0.85     # for future semantic activation
  core_skills:
    - first-principles-scientific-mindset
                                 # skill names to auto-activate on every session

resolution:
  auto_deduplicate: true
  categories:
    file_io:
      preferred: filesystem      # server id that wins conflicts
      strategy: namespace        # suppress_others | namespace | unify
      namespace_prefix: fs_      # prefix for non-preferred tools
    web_search:
      preferred: playwright
      strategy: suppress_others
```

### Fields

#### `registry.mcp_servers[]`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `id` | string | yes | Unique identifier for this server |
| `type` | string | yes | Transport: `stdio`, `sse`, or `http` |
| `command` | string | stdio only | Command to spawn |
| `args` | list[string] | no | Arguments to pass to command |
| `url` | string | sse/http only | Endpoint URL |
| `env` | dict | no | Environment variables for the process |
| `enabled` | bool | no | Default `true`. Set `false` to disable without removing |

#### `registry.skills_dirs[]`

List of paths to scan for skill bundles. Supports `~` expansion. Directories are scanned in order; later entries override earlier ones if skill names collide.

#### `disclosure`

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `strategy` | string | `manual` | `manual` = explicit `request_skill` only. `semantic` = reserved for future LLM-based activation |
| `activation_threshold` | float | `0.85` | Reserved for semantic strategy |
| `core_skills` | list[string] | `[]` | Skills auto-activated at session start |

#### `resolution.categories`

Each category entry controls how conflicting tool names from different servers are resolved.

| Field | Type | Description |
|-------|------|-------------|
| `preferred` | string | Server id whose tools win conflicts |
| `strategy` | string | `suppress_others`, `namespace`, or `unify` |
| `namespace_prefix` | string | Prefix for non-preferred tools (namespace strategy) |
| `unified_name` | string | Single name to expose (unify strategy) |

---

## Project Config (`<project>/.gearcore/config.yaml`)

```yaml
version: 2

context:
  name: "HIVE"
  description: "Worker orchestration and task dispatch"

scope:
  mcp_servers:
    include:                     # allowlist — only these global servers are visible
      - hive-gateway
      - filesystem

  skills:
    include:                     # allowlist — only these global skills are visible
      - first-principles-scientific-mindset
      - hive-worker
      - code-ops
      - memory

# Optional — overrides global disclosure for this project
disclosure:
  core_skills:
    - first-principles-scientific-mindset
    - hive-worker                # auto-activate when this project loads
  activation_threshold: 0.90
```

### Fields

#### `context`

| Field | Type | Description |
|-------|------|-------------|
| `name` | string | Human-readable project name (shown in `gearcore status`) |
| `description` | string | Optional project description |

#### `scope.mcp_servers.include`

List of MCP server ids from the global registry. Only servers listed here are started and visible when GearCore is invoked with this project context. Omit the `include` key entirely to allow all global servers.

#### `scope.skills.include`

List of skill names from the global registry. Only skills listed here are visible. Omit the `include` key entirely to allow all global skills.

**Project-local skills** (in `.gearcore/skills/`) are always included automatically and do not need to be listed here.

#### `disclosure` (optional)

Same schema as global. When present, completely overrides the global disclosure settings for this project context.

---

## Project Directory Structure

```
<project>/
  .gearcore/
    config.yaml              ← project config (allowlists + overrides)
    skills/                  ← project-local skill bundles
      my-custom-skill/
        SKILL.md
        manifest.json
```

---

## Config Precedence Summary

```
Global MCPs       →  filtered by project scope.mcp_servers.include
Global skills     →  filtered by project scope.skills.include
Project skills    →  always included when project context present
Disclosure rules  →  project overrides global if present
Resolution rules  →  always from global (not overridable per project)
```
