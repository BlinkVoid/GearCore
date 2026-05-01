# Contributing to GearCore

Thanks for your interest in contributing! GearCore is a small, focused project and we want to keep it that way.

## Getting Started

1. Fork the repository
2. Clone your fork: `git clone https://github.com/YOUR_USERNAME/gearcore`
3. Install dependencies: `uv pip install -e ".[dev]"`
4. Create a branch: `git checkout -b feature/your-feature`

## Development Setup

```bash
# Run the hub locally
uv run gearcore serve

# Run verification scripts
uv run python verify_hub.py
uv run python verify_skills.py

# Test a specific command
uv run gearcore status
uv run gearcore list-skills
```

## Code Style

- Follow PEP 8
- Use type hints for public APIs
- Keep functions focused and small
- Add docstrings for modules and public classes/functions

## Pull Request Process

1. Ensure your changes work with `verify_hub.py` and `verify_skills.py`
2. Update documentation if you change behavior
3. Keep commits focused and atomic
4. Open a PR with a clear description of the problem and solution

## Reporting Issues

When reporting bugs, please include:
- Your OS and Python version
- The output of `gearcore status`
- Steps to reproduce
- Expected vs actual behavior

## Scope

GearCore intentionally stays focused on:
- Skill discovery and progressive disclosure
- MCP hub aggregation
- Layered project-scoped configuration

Features outside this scope (e.g., a GUI, a web dashboard, a marketplace) are better suited as separate projects that integrate with GearCore.
