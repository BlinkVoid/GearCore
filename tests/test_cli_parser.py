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
