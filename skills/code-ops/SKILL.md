# Skill: Code Operations

## Overview
Filesystem tools for reading, writing, and managing files within the allowed workspace directories.

## Workflow
1. `list_allowed_directories` — Check where you have access
2. `list_directory` — Explore project structure
3. `read_file` — Read existing code before modifying
4. `write_file` — Create or overwrite files
5. `search_files` — Find files by name pattern

## Best Practices
- Always read a file before overwriting it
- Use `list_allowed_directories` to verify you're within bounds
- Avoid listing very large directories (e.g., node_modules, .git)
- Create parent directories with `create_directory` before writing nested files
- Prefer editing specific sections over full file rewrites
