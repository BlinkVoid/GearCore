# Design Rationale: Skill-First Architecture

## Decision

GearCore registers itself as a **CLI skill** in AI agent tools (Claude Code, Codex,
Kimi), not as an MCP server in their `mcpServers` config.

The `serve` subcommand (MCP hub mode) exists as a fallback for clients that support
MCP but not skills. It is not the primary integration path.

## Why Not MCP?

### 1. Context Window Tax

Every MCP server registered in `settings.json` injects its full tool schema into the
system prompt on every message — regardless of whether those tools are used. This is
the "tool tax."

**Direct MCP registration (all tools loaded upfront):**
```
Message 1:  [system: 12 tool schemas = ~2,400 tokens]  + user prompt
Message 2:  [system: 12 tool schemas = ~2,400 tokens]  + user prompt
Message 3:  [system: 12 tool schemas = ~2,400 tokens]  + user prompt
...
Total tax over 20 messages: ~48,000 tokens (wasted if tools unused)
```

**GearCore skill (progressive disclosure):**
```
Message 1:  [system: 0 extra tool tokens]  + user prompt
Message 5:  user triggers gearcore → list-skills output: ~200 tokens (one-shot)
Message 6:  user requests skill → SKILL.md: ~400 tokens (one-shot)
Message 7+: gearcore call <tool>: ~50 tokens per invocation
...
Total cost over 20 messages: ~800 tokens (only what was used)
```

The saving scales with the number of registered tools and conversation length.
See `benchmarks/` for empirical measurements.

### 2. Pull vs Push Model

MCP is a **push** model — connecting a server immediately exposes all its tools.
This defeats progressive disclosure by design. Even if the MCP server only advertises
`list_skills` and `request_skill` as initial tools, the protocol requires the client to
call `list_tools` and inject the results into context. The server cannot selectively
withhold tools from the schema listing without violating the MCP spec.

A CLI skill is a **pull** model — the AI decides when to invoke `gearcore`, what to
ask for, and only the response text enters the context. No schema injection occurs
unless the AI explicitly requests it.

### 3. Cross-Client Portability

| Client | MCP Support | Repo-scoped `.mcp.json` | Skill/Instruction Support |
|--------|-------------|-------------------------|---------------------------|
| Claude Code | Yes | Yes (dynamic) | Yes (`~/.claude/skills/`) |
| Codex | Yes | No | Yes (`~/.codex/skills/`) |
| Kimi | Yes | No | Yes (`~/.config/agents/skills/`) |
| Gemini CLI | Partial | No | Partial |

Claude Code can dynamically load `.mcp.json` per-repo, but other clients cannot.
A skill installed via `gearcore sync` works identically across all clients because
every agent can shell out to a CLI binary. The skill is the universal integration
surface; MCP is client-specific.

### 4. No Persistent Process

Each `gearcore call <server> <tool> '<args>'` is stateless:
1. Starts the backend MCP server
2. Calls the tool
3. Prints the result
4. Stops the server

No long-running hub process to manage. No reconnection issues. No zombie processes
consuming memory between conversations. The OS process lifecycle is the session
lifecycle.

In `serve` mode (MCP hub), the process must stay alive for the duration of the
client session, manage backend connections, handle reconnections, and clean up on
exit. This complexity exists only for the fallback path.

### 5. Project Scoping is Trivial

With a skill, project scoping is a CLI flag: `gearcore --project /path/to/project`.
The flag selects the config layer, filters visibility, and returns scoped results.

With an MCP server, project scoping requires either:
- A separate server instance per project (process sprawl)
- A stateful context-switch protocol (complexity)
- Encoding the project path in environment variables at server startup (inflexible)

## When MCP Mode Makes Sense

The `gearcore serve` fallback is appropriate when:
- The client supports MCP but has no skill/instruction mechanism at all
- The client needs real-time tool updates (schema changes mid-session)
- Performance-critical scenarios where subprocess overhead per call is unacceptable

These are edge cases. For the primary use case — AI CLI agents doing development
work — the skill path is strictly better.

## Summary

| Property | Skill (CLI) | MCP Server |
|----------|-------------|------------|
| Context cost at rest | Zero | Full tool schema on every message |
| Disclosure model | Pull (on-demand) | Push (all upfront) |
| Cross-client support | Universal (shell out) | Client-specific |
| Process lifecycle | Stateless per-call | Long-running |
| Project scoping | CLI flag | Per-instance or stateful |
| Complexity | Low | Medium-High |
