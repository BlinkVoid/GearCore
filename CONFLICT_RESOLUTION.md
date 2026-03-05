# GearCore: Conflict Resolution & Deduplication Strategy

When multiple MCP servers or Skill Bundles provide overlapping functionality (e.g., two different `search` tools or `read_file` tools), GearCore's **Conflict Resolver** intervenes to maintain context clarity and model performance.

---

## 1. The Three-Tier Resolution Logic

GearCore uses a cascading strategy to resolve tool collisions:

### Tier 1: Semantic Deduplication (The "Best-of-Breed" Rule)
*   **Mechanism**: If two tools have identical or near-identical schemas (e.g., `fetch_url` from two different servers), GearCore suppresses one.
*   **Configuration**: Defined in the `mcp_servers` configuration in the Hub.
*   **Example**: Both `brave-search` and `google-search` provide a `search` tool. GearCore only exposes one `search` tool to the client, routing requests to the "Primary" provider.

### Tier 2: Domain-Based Namespacing
*   **Mechanism**: If two tools do similar things but for different domains, GearCore prefixes them to provide clarity.
*   **Example**:
    *   `fs_read_file` (Local File System MCP)
    *   `git_read_file` (GitHub MCP)
*   **Benefit**: The model clearly understands the **source** of the data it is accessing.

### Tier 3: The "Expert Router" (Unified Interface)
*   **Mechanism**: GearCore creates a "Virtual Tool" that hides the complexity of multiple backends.
*   **Example: `smart_search(query)`**
    *   GearCore's Hub analyzes the query.
    *   If the query mentions "local files," it routes to the `ripgrep` MCP.
    *   If it's a "breaking news" query, it routes to the `brave-search` MCP.
    *   **Result**: The LLM only sees **one** search tool, reducing context bloat and decision fatigue.

---

## 2. Priority Matrix Configuration

GearCore uses a `priority_matrix.json` to manage these conflicts centrally.

```json
{
  "categories": {
    "file_operations": {
      "preferred_server": "local-filesystem",
      "fallback_servers": ["github-mcp", "dropbox-mcp"],
      "conflict_strategy": "suppress_others"
    },
    "web_search": {
      "preferred_server": "brave-search",
      "fallback_servers": ["duckduckgo"],
      "conflict_strategy": "unify",
      "unified_tool_name": "web_search"
    },
    "security_scan": {
      "preferred_server": "security-hub",
      "conflict_strategy": "namespace",
      "namespace_prefix": "sec_"
    }
  }
}
```

---

## 3. Handling "Tedious" Definitions (The Proxy Advantage)

Because GearCore acts as a **Shared Hub**, you only define your MCP servers **once** in the GearCore configuration. 

### Current State (Tedious):
*   Cursor: Manually add 10 MCP servers to `mcp.json`.
*   Claude Code: Manually add 10 MCP servers to config.
*   Agent: Manually code 10 MCP clients.

### GearCore State (Centralized):
1.  **Define in GearCore**: Add all 10 servers to `gearcore_config.yaml`.
2.  **Point Clients to GearCore**:
    *   Cursor `mcp.json` -> `{"gearcore": {"command": "gearcore-hub", "args": ["--port", "8000"]}}`
    *   Claude Code -> `mcp add gearcore http://localhost:8000/sse`
3.  **Result**: All clients immediately gain access to the **Conflict-Resolved, Deduplicated, and Skill-Bundled** toolset.

---

## 4. Implementation Roadmap

1.  **Semantic Matcher**: A utility that compares JSON schemas of all connected MCP tools to find overlaps.
2.  **Virtual Tool Injector**: Logic to create and expose "Smart Tools" (Routers) that delegate to multiple backends.
3.  **Conflict Dashboard**: A small UI or CLI command (`gearcore status`) that shows which tools are active and which are being suppressed due to conflicts.
