# GearCore: Centralized Configuration Specification (`gearcore.yaml`)

This YAML file is the primary interface for managing your MCP servers, skills, and conflict resolution rules. It eliminates the need to manually configure individual clients (Cursor, Claude Code, etc.).

---

## 1. Global Settings
Basic configuration for the GearCore Hub service.

```yaml
hub:
  port: 8686
  host: "127.0.0.1"
  log_level: "info"
  # How long to keep a shared MCP process alive without active clients
  process_timeout_ms: 3600000 
  # Strategy for progressive disclosure: "manual" or "semantic"
  disclosure_strategy: "semantic"
```

---

## 2. Managed MCP Servers (Shared Backends)
Define each MCP server once. GearCore handles the lifecycle and multiplexing.

```yaml
mcp_servers:
  - id: "brave-search"
    type: "stdio" # or "sse"
    command: "npx"
    args: ["-y", "@modelcontextprotocol/server-brave-search"]
    env:
      BRAVE_API_KEY: "your-api-key"
    enabled: true

  - id: "local-fs"
    type: "stdio"
    command: "npx"
    args: ["-y", "@modelcontextprotocol/server-filesystem", "E:/workspace"]
    enabled: true

  - id: "github-mcp"
    type: "stdio"
    command: "npx"
    args: ["-y", "@modelcontextprotocol/server-github"]
    env:
      GITHUB_PERSONAL_ACCESS_TOKEN: "your-pat"
    enabled: true
```

---

## 3. Conflict Resolution & Priority Matrix
Rules for handling overlapping tools across different servers.

```yaml
resolution:
  # Tier 1: Semantic Deduplication
  # If schemas match 95%+, only show the preferred server's tool.
  auto_deduplicate: true

  # Tier 2 & 3: Category Priorities
  categories:
    search:
      preferred: "brave-search"
      fallback: ["google-search", "duckduckgo"]
      strategy: "unify" # Expose as a single 'web_search' tool
      unified_name: "web_search"

    file_io:
      preferred: "local-fs"
      strategy: "namespace" # Expose as 'fs_read_file', 'git_read_file', etc.
      namespace_prefix: "fs_"

    security:
      preferred: "security-hub"
      strategy: "suppress_others" # Only show the preferred server's tools
```

---

## 4. Skill Bundle Discovery
Tell GearCore where to look for your `agentskills.io` compatible bundles.

```yaml
skills:
  directory: "./skills"
  # Skills to always load by default into every session
  core_skills:
    - "system-management"
    - "context-optimizer"
  # Automatic loading threshold (0.0 to 1.0) for semantic triggers
  activation_threshold: 0.85
```

---

## 5. Client Sync (Optional)
GearCore can automatically update your local `mcp.json` files for other clients to point them to the Hub.

```yaml
client_sync:
  cursor:
    enabled: true
    path: "%APPDATA%/Cursor/User/globalStorage/saoudrizwan.claude-dev/settings/mcp.json"
  claude_code:
    enabled: true
    path: "~/.claudecode/config.json"
```

---

## 6. Usage Flow

1.  **Edit `gearcore.yaml`**: Add a new MCP server or tweak a priority rule.
2.  **Restart/Reload Hub**: `gearcore-hub --reload`
3.  **Automatic Propagation**:
    *   The **Shared Process Manager** spins up the new server.
    *   The **Conflict Resolver** updates the virtual toolset.
    *   The **Virtual Server** notifies all connected clients (`Cursor`, `Claude Code`) that the toolset has changed via `list_changed`.
    *   **Context Optimization**: The actual tool schemas for the new server are hidden from the LLM until a Skill trigger is met.
