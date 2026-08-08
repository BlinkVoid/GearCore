import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from gearcore_hub.config import load_config
from gearcore_hub.main import build_parser, cmd_status
from gearcore_hub.main import main as cli_main


def test_update_superpowers_parser():
    parser = build_parser()
    args = parser.parse_args(["update-superpowers"])
    assert args.command == "update-superpowers"
    assert args.dry_run is False

    args = parser.parse_args(["update-superpowers", "--dry-run"])
    assert args.dry_run is True


def test_add_mcp_command_flag_does_not_clobber_subcommand():
    # Regression: --command shared dest="command" with the subparser action,
    # so `add-mcp --command uvx` dispatched to "uvx" (no-op help) instead.
    parser = build_parser()
    args = parser.parse_args(
        [
            "add-mcp",
            "--id",
            "jira",
            "--type",
            "stdio",
            "--command",
            "uvx",
            "--args",
            "mcp-atlassian",
            "--env",
            "A=B",
        ]
    )
    assert args.command == "add-mcp"
    assert args.mcp_command == "uvx"
    assert args.args == ["mcp-atlassian"]


def test_launch_policy_flags_are_available_to_runtime_commands():
    parser = build_parser()

    for command in ("serve", "status", "list", "list-skills", "request-skill", "call"):
        argv = [
            "--config",
            "/safe/config.yaml",
            "--profile",
            "hive-worker",
            "--context-envelope",
            "/safe/envelope.json",
            "--envelope-public-key",
            "/safe/public-key.json",
            command,
        ]
        if command == "request-skill":
            argv.append("worker")
        elif command == "call":
            argv.extend(("hive-gateway", "submit", "{}"))

        args = parser.parse_args(argv)

        assert args.config == "/safe/config.yaml"
        assert args.profile == "hive-worker"
        assert args.context_envelope == "/safe/envelope.json"
        assert args.envelope_public_key == "/safe/public-key.json"


@pytest.mark.parametrize(
    "launch_args",
    [
        ["--context-envelope", "", "--envelope-public-key", ""],
        ["--context-envelope", "   ", "--envelope-public-key", "   "],
        ["--context-envelope", ""],
        ["--envelope-public-key", ""],
        ["--context-envelope", "   "],
        ["--envelope-public-key", "   "],
        ["--context-envelope", "secret-missing-envelope"],
        ["--envelope-public-key", "secret-missing-key"],
    ],
)
def test_explicit_empty_envelope_cli_inputs_never_fall_back_to_operator(
    tmp_path, monkeypatch, capsys, launch_args
):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """\
version: 3
profiles:
  default: operator
  entries:
    operator: {}
    hive-worker:
      constrained: true
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["gearcore", "--config", str(config_path), *launch_args, "status"],
    )

    cli_main()

    output = capsys.readouterr().out
    assert "invalid_launch_envelope" in output
    assert "Profile: operator" not in output
    for value in launch_args[1::2]:
        if value.strip():
            assert value not in output


def test_status_prints_vendor_manifest(capsys):
    manifest = MagicMock()
    manifest.vendored_commit = "abcdef1234567890"
    manifest.vendored_at = "2026-07-05"
    manifest.source = "https://github.com/obra/superpowers.git"
    manifest.source_ref = "main"

    config = load_config(global_config_path=Path("/nonexistent"))

    with patch("gearcore_hub.vendor.load_vendor_manifest", return_value=manifest), \
         patch("gearcore_hub.vendor.get_upstream_commit", return_value="abcdef1234567890"):
        cmd_status(config)

    captured = capsys.readouterr()
    assert "superpowers" in captured.out
    assert "abcdef1234567890"[:12] in captured.out


def test_status_prints_update_hint_when_upstream_different(capsys):
    manifest = MagicMock()
    manifest.vendored_commit = "abcdef1234567890"
    manifest.vendored_at = "2026-07-05"
    manifest.source = "https://github.com/obra/superpowers.git"
    manifest.source_ref = "main"

    config = load_config(global_config_path=Path("/nonexistent"))

    with patch("gearcore_hub.vendor.load_vendor_manifest", return_value=manifest), \
         patch("gearcore_hub.vendor.get_upstream_commit", return_value="fedcba0987654321"):
        cmd_status(config)

    captured = capsys.readouterr()
    assert "update available" in captured.out
    assert "fedcba098765" in captured.out
