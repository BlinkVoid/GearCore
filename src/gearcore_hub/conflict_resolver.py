import logging
from typing import Any

from mcp.types import Tool

logger = logging.getLogger("gearcore.conflict_resolver")


class ConflictResolver:
    """Applies resolution rules to aggregated MCP tools."""

    def __init__(self, resolution_config: dict[str, Any]):
        self.config = resolution_config
        self.categories = resolution_config.get("categories", {})
        self.auto_deduplicate = resolution_config.get("auto_deduplicate", True)

    def resolve(
        self, aggregated_tools: list[dict[str, Any]]
    ) -> tuple[list[Tool], dict[str, dict[str, str]]]:
        """
        Takes a list of dicts: {"server_id": str, "tool": Tool, "original_name": str}
        Returns:
            - resolved_tools: List of Tool objects with namespaced/aliased names
            - tool_map: Dict mapping resolved_name -> {"server_id": ..., "original_name": ...}
        """
        resolved_tools: list[Tool] = []
        tool_map: dict[str, dict[str, str]] = {}

        # Group tools by original name to detect conflicts
        by_name: dict[str, list[dict[str, Any]]] = {}
        for entry in aggregated_tools:
            name = entry["original_name"]
            if name not in by_name:
                by_name[name] = []
            by_name[name].append(entry)

        for original_name, entries in by_name.items():
            # Find if this tool belongs to a configured category
            category_cfg = self._get_category_for_tool(original_name)

            if len(entries) == 1 and not category_cfg:
                # No conflict and no specific rule: Use default namespacing for safety
                entry = entries[0]
                new_name = f"{entry['server_id']}_{original_name}"
                resolved_tools.append(
                    entry["tool"].model_copy(update={"name": new_name})
                )
                tool_map[new_name] = {
                    "server_id": entry["server_id"],
                    "original_name": original_name,
                }
                continue

            # Apply Category-based Strategy
            if category_cfg:
                strategy = category_cfg.get("strategy", "namespace")
                preferred = category_cfg.get("preferred")

                if strategy == "suppress_others":
                    # Only include the preferred one
                    for entry in entries:
                        if entry["server_id"] == preferred:
                            resolved_tools.append(entry["tool"])
                            tool_map[original_name] = {
                                "server_id": entry["server_id"],
                                "original_name": original_name,
                            }
                            break

                elif strategy == "namespace":
                    prefix = category_cfg.get(
                        "namespace_prefix", f"{entries[0]['server_id']}_"
                    )
                    for entry in entries:
                        if entry["server_id"] == preferred:
                            # Preferred server keeps original name
                            resolved_tools.append(entry["tool"])
                            tool_map[original_name] = {
                                "server_id": entry["server_id"],
                                "original_name": original_name,
                            }
                        else:
                            new_name = f"{prefix}{original_name}"
                            resolved_tools.append(
                                entry["tool"].model_copy(update={"name": new_name})
                            )
                            tool_map[new_name] = {
                                "server_id": entry["server_id"],
                                "original_name": original_name,
                            }

                elif strategy == "unify":
                    unified_name = category_cfg.get("unified_name", original_name)
                    for entry in entries:
                        if entry["server_id"] == preferred:
                            resolved_tools.append(
                                entry["tool"].model_copy(update={"name": unified_name})
                            )
                            tool_map[unified_name] = {
                                "server_id": entry["server_id"],
                                "original_name": original_name,
                            }
                            break
            else:
                # Conflict detected but no rule: Default to server-id namespacing
                for entry in entries:
                    new_name = f"{entry['server_id']}_{original_name}"
                    resolved_tools.append(
                        entry["tool"].model_copy(update={"name": new_name})
                    )
                    tool_map[new_name] = {
                        "server_id": entry["server_id"],
                        "original_name": original_name,
                    }

        return resolved_tools, tool_map

    def _get_category_for_tool(self, tool_name: str) -> dict[str, Any] | None:
        """Determine if a tool falls into a prioritized category."""
        # Simple mapping for the PoC. In production, this would use a
        # tool-to-category registry or semantic analysis.
        mapping = {
            "read_file": "file_io",
            "write_file": "file_io",
            "list_directory": "file_io",
            "search": "web_search",
            "brave_search": "web_search",
        }

        cat_id = mapping.get(tool_name)
        return self.categories.get(cat_id) if cat_id else None
