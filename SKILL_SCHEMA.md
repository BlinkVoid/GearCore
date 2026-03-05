# GearCore: Skill Bundle Schema Specification (V2)

Aligned with the **Agent Skills Open Standard (agentskills.io)**, GearCore Skill Bundles provide a standardized way to package procedural knowledge, scripts, and MCP tool connectivity.

---

## 1. Directory Structure (Standardized)

Each skill resides in its own folder. GearCore follows the `agentskills.io` layout:

```text
skills/
└── web-research/
    ├── SKILL.md          # Primary procedural instructions (The "Brain")
    ├── manifest.json     # Technical metadata & tool mapping (The "Skeleton")
    ├── scripts/          # Optional: Executable logic (Python/JS)
    │   └── validate.py
    └── resources/        # Optional: Data, templates, or reference docs
```

---

## 2. The `manifest.json` (V2)

Enhanced to support **Semantic Auto-Activation** and **Script Hooks**.

```json
{
  "name": "web-research",
  "version": "1.0.0",
  "description": "Deep web research with source validation and content ingestion.",
  "category": "research",
  "mcp_servers": [
    {
      "server_id": "brave-search",
      "tools": ["brave_search", "brave_local_search"],
      "alias_prefix": "web_"
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
    "strategy": "semantic",
    "triggers": ["research", "search", "lookup", "verify"],
    "priority": 10
  }
}
```

### Key Enhancements:
*   **`strategy: semantic`**: GearCore's Hub uses LLM-based intent recognition to **automatically** propose or load this skill when the conversation context matches the `triggers` or `description`.
*   **`scripts`**: Explicitly defines executable logic that Claude can call via the `code_execution` tool to perform complex validation or data processing locally.
*   **`priority`**: Helps GearCore's **Conflict Resolver** decide which skill to load if multiple skills match the same intent.

---

## 3. The `SKILL.md` (V2)

Follows the **Procedural Knowledge** mandate. It must focus on "How" to perform the task.

```markdown
# Skill: Web Research

## Overview
A workflow for high-signal technical research.

## Procedure
1. Use `web_search` for initial discovery.
2. Evaluate results using the `validate_source` script to filter for high-authority domains.
3. Ingest selected pages using `web_fetch_url`.
4. Synthesize with citations.

## Constraints
- Do not use `web_search` for queries that can be answered with local `read_file`.
- Prefer official documentation over community blogs.

## Triggers
- "Research the latest version of..."
- "Find the documentation for..."
```

---

## 4. Progressive Disclosure & Resource Management

GearCore manages the context window using a **Three-Tier Loading System**:

1.  **Tier 1: Global Index (Low Token Cost)**
    *   GearCore sends only the `name` and `description` of all available skills to the client.
2.  **Tier 2: Semantic Suggestion (Zero User Effort)**
    *   If the user prompt matches a skill's `triggers`, GearCore sends a notification: *"Skill 'Web Research' is available for this task. Load?"*
3.  **Tier 3: Active Context (Full Skill Load)**
    *   Once loaded, the `SKILL.md` instructions and full MCP tool schemas are injected into the active session.
    *   The **Shared Hub** ensures that the underlying MCP processes are ready to receive calls.

---

## 5. Hub Integration: The "Universal Process"

By using the **Shared Hub** model, GearCore ensures:
*   **Resource Efficiency**: Multiple skills can share the same `brave-search` MCP process.
*   **Script Safety**: Local scripts are executed in a sandboxed environment managed by the Hub.
*   **Consistency**: Conflict resolution happens centrally; the client always sees a clean, deduplicated set of tools.
