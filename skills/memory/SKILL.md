# Skill: Persistent Memory (SampleMemory)

## Overview
Access to Swarm's persistent memory system for querying historical context, past decisions, and learned knowledge.

## Tools

### mem_query
Query memories with optional quadrant hint.
```bash
gearcore call sample-memory mem_query '{"query": "What was the auth approach for project X?", "quadrant_hint": "coding"}'
```

### mem_save
Store new knowledge to short-term buffer for future reference.
```bash
gearcore call sample-memory mem_save '{"content": "...", "summary": "...", "quadrants": ["coding"]}'
```

### add_task
Add a task to the memory system.
```bash
gearcore call sample-memory add_task '{"title": "Review auth middleware", "priority": "medium"}'
```

## Best Practices
- Query memory before starting complex tasks — past workers may have relevant context
- Use specific queries over vague ones
- Only store genuinely new, reusable knowledge — not task-specific ephemera
- Memory is shared across all workers — write clearly for future readers
