# Skill: Persistent Memory (SampleMemory)

## Overview
Access to Swarm's persistent memory system for querying historical context, past decisions, and learned knowledge.

## Workflow
1. `mem_query` — Ask a natural language question against stored memories
2. `mem_search` — Search for specific topics or keywords
3. `mem_store` — Save new knowledge for future reference (use sparingly)

## Best Practices
- Query memory before starting complex tasks — past workers may have relevant context
- Use specific queries ("What was the authentication approach for project X?") over vague ones
- Only store genuinely new, reusable knowledge — not task-specific ephemera
- Memory is shared across all workers — write clearly for future readers
