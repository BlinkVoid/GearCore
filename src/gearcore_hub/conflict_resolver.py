import logging
from typing import List, Dict, Any, Optional, Set
from mcp.types import Tool

logger = logging.getLogger("gearcore.conflict_resolver")

class ConflictResolver:
    """Applies resolution rules to aggregated MCP tools."""
    def __init__(self, resolution_config: Dict[str, Any]):
        self.config = resolution_config
        self.categories = resolution_config.get("categories", {})
        self.auto_deduplicate = resolution_config.get("auto_deduplicate", True)

    def resolve(self, aggregated_tools: List[Dict[str, Any]]) -> List[Tool]:
        """
        Takes a list of dicts: {"server_id": str, "tool": Tool}
        Returns a resolved list of Tool objects with namespaced/aliased names.
        """
        resolved_tools: List[Tool] = []
        seen_names: Set[str] = set()

        # Group tools by original name to detect conflicts
        by_name: Dict[str, List[Dict[str, Any]]] = {}
        for entry in aggregated_tools:
            name = entry["tool"].name
            if name not in by_name:
                by_name[name] = []
            by_name[name].append(entry)

        for original_name, entries in by_name.items():
            # Find if this tool belongs to a configured category
            category_cfg = self._get_category_for_tool(original_name)
            
            if len(entries) == 1 and not category_cfg:
                # No conflict and no specific rule: Use default namespacing for safety
                entry = entries[0]
                tool = entry["tool"]
                tool.name = f"{entry['server_id']}_{tool.name}"
                resolved_tools.append(tool)
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
                            break
                
                elif strategy == "namespace":
                    prefix = category_cfg.get("namespace_prefix", f"{entries[0]['server_id']}_")
                    for entry in entries:
                        tool = entry["tool"]
                        if entry["server_id"] == preferred:
                            # Preferred server might get no prefix or a specific one
                            resolved_tools.append(tool)
                        else:
                            tool.name = f"{prefix}{tool.name}"
                            resolved_tools.append(tool)

                elif strategy == "unify":
                    # For now, just alias the preferred one to the unified name
                    unified_name = category_cfg.get("unified_name", original_name)
                    for entry in entries:
                        if entry["server_id"] == preferred:
                            tool = entry["tool"]
                            tool.name = unified_name
                            resolved_tools.append(tool)
                            break
            else:
                # Conflict detected but no rule: Default to server-id namespacing
                for entry in entries:
                    tool = entry["tool"]
                    tool.name = f"{entry['server_id']}_{tool.name}"
                    resolved_tools.append(tool)

        return resolved_tools

    def _get_category_for_tool(self, tool_name: str) -> Optional[Dict[str, Any]]:
        """Determine if a tool falls into a prioritized category."""
        # Simple mapping for the PoC. In production, this would use a 
        # tool-to-category registry or semantic analysis.
        mapping = {
            "read_file": "file_io",
            "write_file": "file_io",
            "list_directory": "file_io",
            "search": "web_search",
            "brave_search": "web_search"
        }
        
        cat_id = mapping.get(tool_name)
        return self.categories.get(cat_id) if cat_id else None
