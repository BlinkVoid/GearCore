from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path


def test_fresh_packaged_install_starts_with_resolved_mcp(tmp_path: Path) -> None:
    """A fresh packaged install must import its entrypoint with MCP 2.x present."""

    uv = shutil.which("uv")
    assert uv is not None, "uv is required for the packaged-install compatibility check"
    repo_root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [
            uv,
            "run",
            "--isolated",
            "--no-project",
            "--no-editable",
            "--python",
            sys.executable,
            "--cache-dir",
            str(tmp_path / "uv-cache"),
            "--with",
            str(repo_root),
            "--with",
            "mcp==2.0.0",
            "python",
            "-c",
            (
                "import importlib.metadata as md; "
                "assert md.version('mcp').startswith('2.'); "
                "from gearcore_hub.main import main; "
                "print(main.__module__)"
            ),
        ],
        cwd=repo_root,
        env={
            **os.environ,
            "UV_PYTHON_DOWNLOADS": "never",
        },
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert result.returncode == 0, (
        "fresh packaged GearCore install failed to import with MCP 2.x\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )
    assert result.stdout.strip() == "gearcore_hub.main"
