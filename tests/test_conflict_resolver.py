"""Tests for the conflict resolver."""

from mcp.types import Tool

from gearcore_hub.conflict_resolver import ConflictResolver


def _make_tool(name: str, description: str = "") -> Tool:
    return Tool(name=name, description=description, inputSchema={"type": "object"})


class TestNoConflict:
    def test_single_tool_gets_namespaced(self):
        resolver = ConflictResolver({"auto_deduplicate": True, "categories": {}})
        aggregated = [
            {
                "server_id": "fs",
                "tool": _make_tool("read_file"),
                "original_name": "read_file",
            }
        ]
        tools, mapping = resolver.resolve(aggregated)
        assert len(tools) == 1
        assert tools[0].name == "fs_read_file"
        assert mapping["fs_read_file"]["server_id"] == "fs"
        assert mapping["fs_read_file"]["original_name"] == "read_file"

    def test_original_tool_not_mutated(self):
        resolver = ConflictResolver({"auto_deduplicate": True, "categories": {}})
        original = _make_tool("read_file")
        aggregated = [
            {"server_id": "fs", "tool": original, "original_name": "read_file"}
        ]
        resolver.resolve(aggregated)
        assert original.name == "read_file"  # unchanged


class TestSuppressOthers:
    def test_only_preferred_included(self):
        resolver = ConflictResolver(
            {
                "categories": {
                    "file_io": {
                        "preferred": "fs",
                        "strategy": "suppress_others",
                    }
                }
            }
        )
        aggregated = [
            {
                "server_id": "fs",
                "tool": _make_tool("read_file"),
                "original_name": "read_file",
            },
            {
                "server_id": "backup",
                "tool": _make_tool("read_file"),
                "original_name": "read_file",
            },
        ]
        tools, mapping = resolver.resolve(aggregated)
        assert len(tools) == 1
        assert tools[0].name == "read_file"
        assert mapping["read_file"]["server_id"] == "fs"


class TestNamespace:
    def test_preferred_keeps_name_others_get_prefix(self):
        resolver = ConflictResolver(
            {
                "categories": {
                    "file_io": {
                        "preferred": "fs",
                        "strategy": "namespace",
                        "namespace_prefix": "backup_",
                    }
                }
            }
        )
        aggregated = [
            {
                "server_id": "fs",
                "tool": _make_tool("read_file"),
                "original_name": "read_file",
            },
            {
                "server_id": "backup",
                "tool": _make_tool("read_file"),
                "original_name": "read_file",
            },
        ]
        tools, mapping = resolver.resolve(aggregated)
        names = [t.name for t in tools]
        assert "read_file" in names
        assert "backup_read_file" in names
        assert mapping["read_file"]["server_id"] == "fs"
        assert mapping["backup_read_file"]["server_id"] == "backup"


class TestUnify:
    def test_preferred_renamed_to_unified(self):
        resolver = ConflictResolver(
            {
                "categories": {
                    "file_io": {
                        "preferred": "fs",
                        "strategy": "unify",
                        "unified_name": "read",
                    }
                }
            }
        )
        aggregated = [
            {
                "server_id": "fs",
                "tool": _make_tool("read_file"),
                "original_name": "read_file",
            },
            {
                "server_id": "backup",
                "tool": _make_tool("read_file"),
                "original_name": "read_file",
            },
        ]
        tools, mapping = resolver.resolve(aggregated)
        assert len(tools) == 1
        assert tools[0].name == "read"
        assert mapping["read"]["server_id"] == "fs"
        assert mapping["read"]["original_name"] == "read_file"


class TestDefaultConflict:
    def test_no_category_rule_uses_server_prefix(self):
        resolver = ConflictResolver({"auto_deduplicate": True, "categories": {}})
        aggregated = [
            {
                "server_id": "fs",
                "tool": _make_tool("search"),
                "original_name": "search",
            },
            {
                "server_id": "web",
                "tool": _make_tool("search"),
                "original_name": "search",
            },
        ]
        tools, mapping = resolver.resolve(aggregated)
        names = [t.name for t in tools]
        assert "fs_search" in names
        assert "web_search" in names
