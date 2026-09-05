from pathlib import Path
from unittest.mock import MagicMock, patch

from gearcore_hub.config import load_config
from gearcore_hub.main import build_parser, cmd_status


def test_update_superpowers_parser():
    parser = build_parser()
    args = parser.parse_args(["update-superpowers"])
    assert args.command == "update-superpowers"
    assert args.dry_run is False

    args = parser.parse_args(["update-superpowers", "--dry-run"])
    assert args.dry_run is True


def test_list_skills_compact_flag():
    parser = build_parser()
    args = parser.parse_args(["list-skills"])
    assert args.command == "list-skills"
    assert args.compact is False

    args = parser.parse_args(["list-skills", "--compact"])
    assert args.command == "list-skills"
    assert args.compact is True


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


def test_status_prints_vendor_manifest(capsys):
    manifest = MagicMock()
    manifest.vendored_commit = "abcdef1234567890"
    manifest.vendored_at = "2026-07-05"
    manifest.source = "https://github.com/obra/superpowers.git"
    manifest.source_ref = "main"

    config = load_config(global_config_path=Path("/nonexistent"))

    with (
        patch("gearcore_hub.vendor.load_vendor_manifest", return_value=manifest),
        patch(
            "gearcore_hub.vendor.get_upstream_commit_cached",
            return_value="abcdef1234567890",
        ),
    ):
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

    with (
        patch("gearcore_hub.vendor.load_vendor_manifest", return_value=manifest),
        patch(
            "gearcore_hub.vendor.get_upstream_commit_cached",
            return_value="fedcba0987654321",
        ),
    ):
        cmd_status(config)

    captured = capsys.readouterr()
    assert "update available" in captured.out
    assert "fedcba098765" in captured.out


class TestHelperBehavior:
    def test_server_version_uses_package_metadata(self):
        import importlib.metadata

        from gearcore_hub.main import server_version

        assert server_version() == importlib.metadata.version("gearcore")

    def test_parse_env_args_skips_malformed_with_warning(self, caplog):
        import logging

        from gearcore_hub.main import parse_env_args

        with caplog.at_level(logging.WARNING):
            env = parse_env_args(["A=1", "malformed", "B=two=parts"])

        assert env == {"A": "1", "B": "two=parts"}
        assert any("malformed" in r.message for r in caplog.records)

    def test_parse_env_args_none_for_empty(self):
        from gearcore_hub.main import parse_env_args

        assert parse_env_args(None) is None
        assert parse_env_args([]) is None
