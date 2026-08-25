import shutil
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from gearcore_hub.config import McpServerConfig
from gearcore_hub.update import (
    get_git_revision,
    get_manifest_version,
    infer_mcp_source_path,
    update_mcp_server,
)


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_get_git_revision(tmp_path: Path):
    # Initialize a git repo to get a real short SHA
    import subprocess
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "file.txt").write_text("x")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init", "--no-gpg-sign"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    sha = get_git_revision(tmp_path)
    assert sha is not None and len(sha) >= 4


def test_get_git_revision_non_repo(tmp_path: Path):
    assert get_git_revision(tmp_path) is None


def test_get_manifest_version(tmp_path: Path):
    (tmp_path / "manifest.json").write_text('{"version": "1.2.3"}')
    assert get_manifest_version(tmp_path) == "1.2.3"


def test_get_manifest_version_missing(tmp_path: Path):
    assert get_manifest_version(tmp_path) is None


def test_infer_mcp_source_path_from_directory_flag():
    server = McpServerConfig(
        id="sample-prompts",
        command="uv",
        args=["run", "--directory", "/home/foo/SamplePrompts", "python", "-m", "sample-prompts.main"],
    )
    assert infer_mcp_source_path(server) == Path("/home/foo/SamplePrompts").resolve()


def test_infer_mcp_source_path_from_absolute_command():
    server = McpServerConfig(
        id="sample-devtools",
        command="/home/foo/SampleDevtools/.venv/bin/sample-devtools-mcp",
    )
    assert infer_mcp_source_path(server) == Path("/home/foo/SampleDevtools/.venv/bin").resolve()


def test_infer_mcp_source_path_unknown():
    server = McpServerConfig(id="cloudflare", command="npx", args=["-y", "@playwright/mcp"])
    assert infer_mcp_source_path(server) is None


def test_update_mcp_server_no_change(tmp_path: Path):
    server = McpServerConfig(
        id="demo",
        command="uv",
        args=["run", "--directory", str(tmp_path), "python", "-m", "demo"],
        update_metadata={"revision": "abc1234"},
    )
    config = MagicMock()
    config.mcp_servers = [server]
    config.project_cfg = None
    config.project_root = None

    with (
        patch("gearcore_hub.update.get_git_revision", return_value="abc1234"),
        patch("gearcore_hub.update.remove_mcp") as mock_remove,
        patch("gearcore_hub.update.add_mcp") as mock_add,
    ):
        result = update_mcp_server(config, "demo")
        assert result["changed"] is False
        assert "up to date" in result["message"]
        mock_remove.assert_not_called()
        mock_add.assert_not_called()


def test_update_mcp_server_changes(tmp_path: Path):
    server = McpServerConfig(
        id="demo",
        command="uv",
        args=["run", "--directory", str(tmp_path), "python", "-m", "demo"],
        update_metadata={"revision": "oldrev"},
    )
    config = MagicMock()
    config.mcp_servers = [server]
    config.project_cfg = None
    config.project_root = None

    with (
        patch("gearcore_hub.update.get_git_revision", return_value="newrev"),
        patch("gearcore_hub.update.remove_mcp") as mock_remove,
        patch("gearcore_hub.update.add_mcp") as mock_add,
        patch(
            "gearcore_hub.update._read_yaml",
            return_value={"registry": {"mcp_servers": [{"id": "demo"}]}},
        ),
        patch("gearcore_hub.update._write_yaml") as mock_write,
    ):
        result = update_mcp_server(config, "demo")
        assert result["changed"] is True
        assert result["current_revision"] == "newrev"
        mock_remove.assert_called_once()
        mock_add.assert_called_once()
        mock_write.assert_called_once()


def test_update_mcp_server_dry_run(tmp_path: Path):
    server = McpServerConfig(
        id="demo",
        command="uv",
        args=["run", "--directory", str(tmp_path), "python", "-m", "demo"],
        update_metadata={"revision": "oldrev"},
    )
    config = MagicMock()
    config.mcp_servers = [server]

    with (
        patch("gearcore_hub.update.get_git_revision", return_value="newrev"),
        patch("gearcore_hub.update.remove_mcp") as mock_remove,
        patch("gearcore_hub.update.add_mcp") as mock_add,
    ):
        result = update_mcp_server(config, "demo", dry_run=True)
        assert result["changed"] is True
        assert "Would update" in result["message"]
        mock_remove.assert_not_called()
        mock_add.assert_not_called()
