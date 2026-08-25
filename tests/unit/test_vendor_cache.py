"""Tests for the TTL-cached git ls-remote wrapper used by `gearcore status`."""

import json
from unittest.mock import patch


def test_cached_returns_value_and_calls_underlying_once(tmp_path):
    from gearcore_hub.vendor import get_upstream_commit_cached

    cache_file = tmp_path / "ls-remote.json"
    with (
        patch("gearcore_hub.vendor._cache_path", return_value=cache_file),
        patch(
            "gearcore_hub.vendor.get_upstream_commit", return_value="abc123"
        ) as mock_remote,
    ):
        assert get_upstream_commit_cached("src", "main") == "abc123"
        assert get_upstream_commit_cached("src", "main") == "abc123"
        assert mock_remote.call_count == 1
    assert json.loads(cache_file.read_text())["src#main"]["sha"] == "abc123"


def test_cache_expiry_triggers_refresh(tmp_path):
    from gearcore_hub.vendor import get_upstream_commit_cached

    cache_file = tmp_path / "ls-remote.json"
    with patch("gearcore_hub.vendor._cache_path", return_value=cache_file):
        with patch(
            "gearcore_hub.vendor.get_upstream_commit", return_value="aaa"
        ) as mock_remote:
            get_upstream_commit_cached("src", "main", ttl=100)
        with patch(
            "gearcore_hub.vendor.get_upstream_commit", return_value="bbb"
        ) as mock_remote2:
            result = get_upstream_commit_cached("src", "main", ttl=-1)

    assert result == "bbb"
    assert mock_remote.call_count == 1
    assert mock_remote2.call_count == 1


def test_failed_lookup_is_not_cached(tmp_path):
    from gearcore_hub.vendor import get_upstream_commit_cached

    cache_file = tmp_path / "ls-remote.json"
    with (
        patch("gearcore_hub.vendor._cache_path", return_value=cache_file),
        patch("gearcore_hub.vendor.get_upstream_commit", return_value=None) as mock_remote,
    ):
        assert get_upstream_commit_cached("src", "main") is None
        # A failed lookup must not poison the cache: next call retries.
        assert mock_remote.call_count == 1
        assert not cache_file.exists()
