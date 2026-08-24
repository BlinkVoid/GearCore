import shutil

import pytest
from pathlib import Path
from unittest.mock import patch

from gearcore_hub.config import McpServerConfig
from gearcore_hub.update import (
    get_git_revision,
    get_manifest_version,
    infer_mcp_source_path,
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
