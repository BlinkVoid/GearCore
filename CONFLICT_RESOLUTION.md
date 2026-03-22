# GearCore: Conflict Resolution (v2)

When multiple MCP backends expose tools with the same name, GearCore's conflict resolver ensures the AI sees a clean, unambiguous toolset.

---

## Resolution Strategies

### 1. suppress_others

Only the preferred server's tool is exposed. All others are dropped.

```yaml
resolution:
  categories:
    web_search:
      preferred: playwright
      strategy: suppress_others
```

**Result:** If both `playwright` and `brave-search` offer a `search` tool, only `playwright`'s version appears.

**When to use:** One backend is strictly better for this category and you never want the alternative.

### 2. namespace

Non-preferred tools get a prefix. The preferred tool keeps its original name.

```yaml
resolution:
  categories:
    file_io:
      preferred: filesystem
      strategy: namespace
      namespace_prefix: fs_
```

**Result:**
- `filesystem` → `read_file` (unchanged — preferred)
- `github` → `fs_read_file` (prefixed)

**When to use:** Both tools are useful but serve different domains. The prefix disambiguates.

### 3. unify

A single tool name is exposed, routed to the preferred backend.

```yaml
resolution:
  categories:
    search:
      preferred: brave-search
      strategy: unify
      unified_name: web_search
```

**Result:** `brave_search` → exposed as `web_search`. Other search tools suppressed.

**When to use:** You want a single canonical name for a capability regardless of backend.

---

## Default Behaviour

Tools not matching any configured category get **server-id namespacing** by default:

```
server_id: filesystem, tool: read_file  →  filesystem_read_file
server_id: github, tool: read_file      →  github_read_file
```

This prevents silent collisions without requiring explicit configuration for every tool.

---

## Category Matching

Categories are matched by tool name against a configurable mapping. The current implementation uses a built-in mapping for common tool names:

| Tool name | Category |
|-----------|----------|
| `read_file` | `file_io` |
| `write_file` | `file_io` |
| `list_directory` | `file_io` |
| `search` | `web_search` |
| `brave_search` | `web_search` |

Tools not in this mapping fall through to default namespacing.

**Known limitation:** This mapping is hardcoded in `conflict_resolver.py`. A future version should move it to the YAML config for extensibility.

---

## Configuration Reference

All resolution config lives in the **global** config only (`~/.config/gearcore/config.yaml`). Project configs cannot override resolution rules.

```yaml
resolution:
  auto_deduplicate: true          # enable conflict detection

  categories:
    <category_name>:
      preferred: <server_id>      # wins conflicts in this category
      strategy: suppress_others | namespace | unify
      namespace_prefix: <prefix>  # for namespace strategy
      unified_name: <name>        # for unify strategy
```

---

## Routing Map

When conflicts are resolved, GearCore maintains an internal `resolved_tool_map` that tracks:

```
resolved_name → { server_id, original_name }
```

This ensures `call_tool` routes to the correct backend even when tool names have been prefixed or aliased.
