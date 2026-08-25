from unittest.mock import MagicMock, patch


@patch("gearcore_hub.update.update_all_mcp_servers", return_value=[{"id": "x", "message": "ok"}])
@patch("gearcore_hub.update.update_all_skills", return_value=[{"name": "y", "message": "ok"}])
@patch("gearcore_hub.vendor.update_superpowers", return_value={"changed": False, "upstream": "abc123"})
@patch("gearcore_hub.sync.sync", return_value={"opencode": "linked"})
def test_update_all(mock_sync, mock_superpowers, mock_skills, mock_mcp):
    from gearcore_hub.update import cmd_update

    config = MagicMock()
    args = MagicMock()
    args.resource = None
    args.dry_run = False
    args.source_path = None
    cmd_update(config, args)
    mock_mcp.assert_called_once()
    mock_skills.assert_called_once()
    mock_superpowers.assert_called_once_with(dry_run=False)
    mock_sync.assert_called_once()


@patch("gearcore_hub.update.update_mcp_server", return_value={"id": "x", "message": "ok"})
def test_update_mcp_single(mock_update):
    from gearcore_hub.update import cmd_update

    config = MagicMock()
    args = MagicMock()
    args.resource = "mcp"
    args.name = "x"
    args.dry_run = False
    args.source_path = None
    cmd_update(config, args)
    mock_update.assert_called_once_with(config, "x", dry_run=False, source_path=None)


@patch("gearcore_hub.vendor.update_superpowers", return_value={"changed": True, "upstream": "def4567890123"})
def test_update_superpowers_dry_run(mock_superpowers):
    from gearcore_hub.update import cmd_update

    config = MagicMock()
    args = MagicMock()
    args.resource = "superpowers"
    args.name = None
    args.dry_run = True
    args.source_path = None
    cmd_update(config, args)
    mock_superpowers.assert_called_once_with(dry_run=True)
