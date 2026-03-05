# Skill: Filesystem Operations

## Overview
Guidelines for securely and efficiently interacting with the local filesystem.

## Workflow
1. Use `list_allowed_directories` to see where you can work.
2. Use `list_directory` to explore the structure.
3. Use `read_file` to ingest content.

## Best Practices
- Avoid listing large directories (e.g., node_modules).
- Always verify the file path before reading.
