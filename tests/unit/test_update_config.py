from gearcore_hub.config import McpServerConfig


def test_mcp_server_config_accepts_update_metadata():
    cfg = McpServerConfig(
        id="sample-prompts",
        command="uv",
        update_metadata={"source_path": "/tmp/sample-prompts", "revision": "abc123"},
    )
    assert cfg.update_metadata["revision"] == "abc123"


def test_mcp_server_config_without_update_metadata_loads():
    cfg = McpServerConfig(id="sample-prompts", command="uv")
    assert cfg.update_metadata is None
