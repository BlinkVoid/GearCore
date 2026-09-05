"""Tests for `gearcore call` structured output (--json) and failure-aware exits.

Structured mode (`--json`) emits exactly one deterministic versioned JSON
envelope on stdout and classifies outcomes as success, usage_error,
transport_error, mcp_tool_error, or nested_command_failure, with distinct
nonzero exit codes for every failure class.

Legacy text mode keeps its historical stdout shape; the only behavior change
(documented in README/CHANGELOG) is that MCP tool errors and nested DevCore
command failures now exit nonzero (legacy coarse code 1) instead of zero.

The nested DevCore adapter is gated to server id ``devcore`` and the exact
command tools ``devcore_run``/``devcore_poll``. It mirrors DevCore's own
command-result contract (``ok``/``exit_code``/``timed_out``/``elapsed_seconds``
with ``ok == (exit_code == 0 and not timed_out)``).
Generic domain payloads with an ``ok`` field are never interpreted.
"""

from __future__ import annotations

import base64
import hashlib
import json
import sys
from unittest.mock import patch

import pytest
from mcp.types import (
    AudioContent,
    BlobResourceContents,
    CallToolResult,
    EmbeddedResource,
    ImageContent,
    ResourceLink,
    TextContent,
    TextResourceContents,
)
from pydantic import BaseModel

from gearcore_hub.config import EffectiveConfig, GlobalConfig
from gearcore_hub.main import _normalize_content, build_parser, cmd_call, main

CALL_SCHEMA = "gearcore.call/1"

# Documented exit-code mapping for `gearcore call --json`.
EXIT_SUCCESS = 0
EXIT_USAGE_ERROR = 2
EXIT_TRANSPORT_ERROR = 3
EXIT_MCP_TOOL_ERROR = 4
EXIT_NESTED_COMMAND_FAILURE = 5


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeSession:
    """Faithful stand-in for mcp ClientSession.call_tool."""

    def __init__(self, result=None, error=None):
        self._result = result
        self._error = error
        self.calls: list[tuple[str, dict]] = []

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        if self._error is not None:
            raise self._error
        return self._result


def _install_fake_server(monkeypatch, *, result=None, error=None, start_error=None):
    """Patch gearcore_hub.main.SharedMCPServer with a lifecycle-faithful fake."""
    created = []

    class FakeSharedMCPServer:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.session = None
            self.stopped = False
            created.append(self)

        async def start(self):
            if start_error is not None:
                raise start_error
            self.session = FakeSession(result, error)

        async def stop(self):
            self.stopped = True

    monkeypatch.setattr("gearcore_hub.main.SharedMCPServer", FakeSharedMCPServer)
    return created


def _config(*server_ids, disabled=()):
    servers = [
        {
            "id": sid,
            "type": "stdio",
            "command": "noop-backend",
            "args": [],
            "enabled": sid not in disabled,
        }
        for sid in server_ids
    ]
    return EffectiveConfig(GlobalConfig(registry={"mcp_servers": servers}), None, None)


def _text_result(*texts, is_error=False, structured_content=None):
    return CallToolResult(
        content=[TextContent(type="text", text=t) for t in texts],
        isError=is_error,
        structuredContent=structured_content,
    )


def _devcore_run_text(exit_code=1, timed_out=False, elapsed=0.4, **extra):
    """Faithful devcore_run result body (dict) as FastMCP serializes it."""
    body = {
        "ok": exit_code == 0 and not timed_out,
        "exit_code": exit_code,
        "timed_out": timed_out,
        "elapsed_seconds": elapsed,
        "command_fingerprint": "fp-123",
        "cwd": "/tmp",
        "used_shell": False,
        "stdout": "selected output",
    }
    body.update(extra)
    return json.dumps(body)


def _run_structured_document(
    config, capsys, server_id="fake", tool="mytool", args_json="{}"
):
    """Run structured mode and return the parsed and raw single JSON document."""
    code = EXIT_SUCCESS
    try:
        cmd_call(config, server_id, tool, args_json, structured=True)
    except SystemExit as exc:
        code = exc.code
    captured = capsys.readouterr()
    # json.loads raises on empty or trailing data: exactly one JSON document.
    payload = json.loads(captured.out)
    return payload, code, captured.err, captured.out


def _run_structured(config, capsys, server_id="fake", tool="mytool", args_json="{}"):
    payload, code, err, _ = _run_structured_document(
        config, capsys, server_id, tool, args_json
    )
    return payload, code, err


def _run_legacy(config, capsys, server_id="fake", tool="mytool", args_json="{}"):
    code = EXIT_SUCCESS
    try:
        cmd_call(config, server_id, tool, args_json)
    except SystemExit as exc:
        code = exc.code
    captured = capsys.readouterr()
    return code, captured.out, captured.err


# ---------------------------------------------------------------------------
# Parser and dispatch
# ---------------------------------------------------------------------------


class TestParserAndDispatch:
    def test_call_json_flag_defaults_off_and_opts_in(self):
        parser = build_parser()
        args = parser.parse_args(["call", "srv", "tool"])
        assert args.command == "call"
        assert args.json is False

        args = parser.parse_args(["call", "srv", "tool", "{}", "--json"])
        assert args.json is True

    def test_dispatch_passes_structured_flag(self, monkeypatch):
        config = _config("srv")
        monkeypatch.setattr(sys, "argv", ["gearcore", "call", "srv", "tool", "--json"])
        with (
            patch("gearcore_hub.main.load_config", return_value=config),
            patch("gearcore_hub.main.cmd_call") as fake_call,
        ):
            main()
        fake_call.assert_called_once_with(config, "srv", "tool", "", structured=True)

    def test_dispatch_defaults_to_legacy_text_mode(self, monkeypatch):
        config = _config("srv")
        monkeypatch.setattr(sys, "argv", ["gearcore", "call", "srv", "tool"])
        with (
            patch("gearcore_hub.main.load_config", return_value=config),
            patch("gearcore_hub.main.cmd_call") as fake_call,
        ):
            main()
        fake_call.assert_called_once_with(config, "srv", "tool", "", structured=False)


# ---------------------------------------------------------------------------
# Structured envelope — outcome classification
# ---------------------------------------------------------------------------


class TestStructuredEnvelope:
    def test_success_envelope_identity_and_content(self, monkeypatch, capsys):
        _install_fake_server(monkeypatch, result=_text_result("hello"))
        payload, code, err = _run_structured(_config("fake"), capsys)

        assert code == EXIT_SUCCESS
        assert err == ""
        assert payload["schema"] == CALL_SCHEMA
        assert payload["server"] == "fake"
        assert payload["tool"] == "mytool"
        assert payload["ok"] is True
        assert payload["status"] == "success"
        assert payload["mcp_is_error"] is False
        assert payload["content"] == [{"type": "text", "text": "hello"}]

    def test_mcp_is_error_classified_and_exit_nonzero(self, monkeypatch, capsys):
        _install_fake_server(monkeypatch, result=_text_result("boom", is_error=True))
        payload, code, err = _run_structured(_config("fake"), capsys)

        assert code == EXIT_MCP_TOOL_ERROR
        assert payload["ok"] is False
        assert payload["status"] == "mcp_tool_error"
        assert payload["mcp_is_error"] is True
        assert payload["content"] == [{"type": "text", "text": "boom"}]
        assert "mcp_tool_error" in err

    def test_start_transport_exception_classified(self, monkeypatch, capsys):
        _install_fake_server(monkeypatch, start_error=RuntimeError("spawn failed"))
        payload, code, err = _run_structured(_config("fake"), capsys)

        assert code == EXIT_TRANSPORT_ERROR
        assert payload["ok"] is False
        assert payload["status"] == "transport_error"
        assert payload["content"] == []
        assert "spawn failed" in payload["error"]
        assert "spawn failed" in err

    def test_call_transport_exception_classified(self, monkeypatch, capsys):
        _install_fake_server(monkeypatch, error=RuntimeError("connection refused"))
        payload, code, _ = _run_structured(_config("fake"), capsys)

        assert code == EXIT_TRANSPORT_ERROR
        assert payload["status"] == "transport_error"
        assert "connection refused" in payload["error"]

    @pytest.mark.parametrize("tool", ["devcore_run", "devcore_poll"])
    def test_devcore_nested_command_failure_classified(self, monkeypatch, capsys, tool):
        _install_fake_server(
            monkeypatch, result=_text_result(_devcore_run_text(exit_code=1))
        )
        payload, code, _ = _run_structured(
            _config("devcore"), capsys, server_id="devcore", tool=tool
        )

        assert code == EXIT_NESTED_COMMAND_FAILURE
        assert payload["ok"] is False
        assert payload["status"] == "nested_command_failure"
        assert payload["mcp_is_error"] is False
        assert payload["server"] == "devcore"
        assert payload["content"][0]["type"] == "text"

    def test_devcore_non_command_tool_run_shaped_payload_stays_success(
        self, monkeypatch, capsys
    ):
        _install_fake_server(
            monkeypatch, result=_text_result(_devcore_run_text(exit_code=1))
        )
        payload, code, _ = _run_structured(
            _config("devcore"), capsys, server_id="devcore", tool="devcore_status"
        )

        assert code == EXIT_SUCCESS
        assert payload["ok"] is True
        assert payload["status"] == "success"

    def test_devcore_successful_command_stays_success(self, monkeypatch, capsys):
        _install_fake_server(
            monkeypatch, result=_text_result(_devcore_run_text(exit_code=0))
        )
        payload, code, _ = _run_structured(
            _config("devcore"), capsys, server_id="devcore", tool="devcore_run"
        )

        assert code == EXIT_SUCCESS
        assert payload["status"] == "success"

    def test_non_devcore_domain_ok_false_remains_success(self, monkeypatch, capsys):
        # A generic domain tool's `ok` field is never interpreted, regardless
        # of how command-like the payload looks.
        domain_payload = json.dumps(
            {"ok": False, "exit_code": 1, "timed_out": False, "elapsed_seconds": 1.0}
        )
        _install_fake_server(monkeypatch, result=_text_result(domain_payload))
        payload, code, _ = _run_structured(
            _config("domain"), capsys, server_id="domain"
        )

        assert code == EXIT_SUCCESS
        assert payload["status"] == "success"
        assert payload["mcp_is_error"] is False

    def test_devcore_payload_without_run_contract_stays_success(
        self, monkeypatch, capsys
    ):
        # Conservative shape validation: {"ok": false} alone does not satisfy
        # the DevCore command-result contract, so it is not a command failure.
        _install_fake_server(monkeypatch, result=_text_result('{"ok": false}'))
        payload, code, _ = _run_structured(
            _config("devcore"), capsys, server_id="devcore"
        )

        assert code == EXIT_SUCCESS
        assert payload["status"] == "success"

    def test_devcore_non_json_text_stays_success(self, monkeypatch, capsys):
        _install_fake_server(monkeypatch, result=_text_result("plain prose reply"))
        payload, code, _ = _run_structured(
            _config("devcore"), capsys, server_id="devcore"
        )

        assert code == EXIT_SUCCESS
        assert payload["status"] == "success"

    def test_structured_content_metadata_preserved(self, monkeypatch, capsys):
        _install_fake_server(
            monkeypatch,
            result=_text_result("done", structured_content={"count": 3}),
        )
        payload, code, _ = _run_structured(_config("fake"), capsys)

        assert code == EXIT_SUCCESS
        assert payload["structured_content"] == {"count": 3}

    def test_output_is_deterministic_across_runs(self, monkeypatch, capsys):
        _install_fake_server(monkeypatch, result=_text_result("stable"))
        config = _config("fake")

        first_payload, first_code, first_err, first_document = _run_structured_document(
            config, capsys
        )
        second_payload, second_code, second_err, second_document = (
            _run_structured_document(config, capsys)
        )

        assert first_code == second_code == EXIT_SUCCESS
        assert first_err == second_err == ""
        assert first_document == second_document
        assert json.loads(first_document) == first_payload
        assert json.loads(second_document) == second_payload

    def test_success_stderr_is_quiet(self, monkeypatch, capsys):
        _install_fake_server(monkeypatch, result=_text_result("quiet"))
        _, _, err = _run_structured(_config("fake"), capsys)

        assert err == ""


# ---------------------------------------------------------------------------
# Structured envelope — pre-flight usage errors
# ---------------------------------------------------------------------------


class TestStructuredUsageErrors:
    def test_unknown_server_envelope_and_exit(self, monkeypatch, capsys):
        _install_fake_server(monkeypatch)
        payload, code, _ = _run_structured(_config("fake"), capsys, server_id="ghost")

        assert code == EXIT_USAGE_ERROR
        assert payload["status"] == "usage_error"
        assert payload["ok"] is False
        assert "ghost" in payload["error"]

    def test_disabled_server_envelope_and_exit(self, monkeypatch, capsys):
        _install_fake_server(monkeypatch)
        payload, code, _ = _run_structured(_config("fake", disabled=("fake",)), capsys)

        assert code == EXIT_USAGE_ERROR
        assert payload["status"] == "usage_error"

    def test_invalid_args_json_envelope_and_exit(self, monkeypatch, capsys):
        _install_fake_server(monkeypatch)
        payload, code, _ = _run_structured(
            _config("fake"), capsys, args_json="{not json"
        )

        assert code == EXIT_USAGE_ERROR
        assert payload["status"] == "usage_error"
        assert "invalid JSON arguments" in payload["error"]


# ---------------------------------------------------------------------------
# Content normalization
# ---------------------------------------------------------------------------


def _mixed_result():
    png_raw = b"\x89PNG\r\n\x1a\nfakepng"
    png_b64 = base64.b64encode(png_raw).decode("ascii")
    blob_raw = b"\x00\x01\x02binaryblob"
    blob_b64 = base64.b64encode(blob_raw).decode("ascii")
    return CallToolResult(
        content=[
            TextContent(type="text", text="before"),
            ImageContent(type="image", data=png_b64, mimeType="image/png"),
            EmbeddedResource(
                type="resource",
                resource=TextResourceContents(
                    uri="file:///notes.txt",
                    mimeType="text/plain",
                    text="resource body",
                ),
            ),
            EmbeddedResource(
                type="resource",
                resource=BlobResourceContents(
                    uri="file:///data.bin",
                    mimeType="application/octet-stream",
                    blob=blob_b64,
                ),
            ),
            ResourceLink(
                type="resource_link",
                name="data.csv",
                uri="file:///data.csv",
                mimeType="text/csv",
                size=128,
            ),
        ],
    )


def _metadata_result():
    binary = base64.b64encode(b"metadata-binary").decode("ascii")
    return CallToolResult(
        content=[
            TextContent(
                type="text",
                text="text",
                annotations={"audience": ["assistant"]},
                meta={"text_meta": True},
            ),
            ImageContent(
                type="image",
                data=binary,
                mimeType="image/png",
                annotations={"priority": 0.5},
                meta={"image_meta": 1},
            ),
            AudioContent(
                type="audio",
                data=binary,
                mimeType="audio/wav",
                annotations={"audience": ["user"]},
                meta={"audio_meta": 2},
            ),
            EmbeddedResource(
                type="resource",
                annotations={"outer_annotation": "yes"},
                meta={"outer_meta": True},
                resource=TextResourceContents(
                    uri="file:///notes.txt",
                    mimeType="text/plain",
                    text="resource text",
                    meta={"nested_meta": "text"},
                ),
            ),
            EmbeddedResource(
                type="resource",
                annotations={"outer_blob_annotation": "yes"},
                meta={"outer_blob_meta": True},
                resource=BlobResourceContents(
                    uri="file:///data.bin",
                    mimeType="application/octet-stream",
                    blob=binary,
                    meta={"nested_meta": "blob"},
                ),
            ),
            ResourceLink(
                type="resource_link",
                name="data.csv",
                title="Data",
                uri="file:///data.csv",
                description="A data link",
                mimeType="text/csv",
                size=128,
                icons=[
                    {
                        "src": "https://example.test/data-icon.png",
                        "mimeType": "image/png",
                        "sizes": ["16x16"],
                    }
                ],
                annotations={"link_annotation": "yes"},
                meta={"link_meta": True},
            ),
        ]
    )


class TestContentNormalization:
    def test_mixed_blocks_preserved_in_order_without_raw_binary(
        self, monkeypatch, capsys
    ):
        png_raw = b"\x89PNG\r\n\x1a\nfakepng"
        blob_raw = b"\x00\x01\x02binaryblob"
        _install_fake_server(monkeypatch, result=_mixed_result())
        payload, code, _, out = _run_structured_document(_config("fake"), capsys)

        assert code == EXIT_SUCCESS
        blocks = payload["content"]
        assert [b["type"] for b in blocks] == [
            "text",
            "image",
            "resource",
            "resource",
            "resource_link",
        ]

        assert blocks[0]["text"] == "before"

        image = blocks[1]
        assert image["mime_type"] == "image/png"
        assert image["byte_length"] == len(png_raw)
        assert image["sha256"] == hashlib.sha256(png_raw).hexdigest()

        assert blocks[2]["uri"] == "file:///notes.txt"
        assert blocks[2]["text"] == "resource body"

        blob = blocks[3]
        assert blob["uri"] == "file:///data.bin"
        assert blob["byte_length"] == len(blob_raw)
        assert blob["sha256"] == hashlib.sha256(blob_raw).hexdigest()

        link = blocks[4]
        assert link["name"] == "data.csv"
        assert link["uri"] == "file:///data.csv"
        assert link["mime_type"] == "text/csv"
        assert link["size"] == 128

        # No raw binary (or its base64 encoding) ever reaches stdout.
        assert base64.b64encode(png_raw).decode() not in out
        assert base64.b64encode(blob_raw).decode() not in out
        assert json.loads(out) == payload

    def test_known_content_preserves_metadata_and_nested_resource_fields(
        self, monkeypatch, capsys
    ):
        _install_fake_server(monkeypatch, result=_metadata_result())
        payload, code, _, out = _run_structured_document(_config("fake"), capsys)

        assert code == EXIT_SUCCESS
        blocks = payload["content"]
        assert blocks[0]["annotations"] == {"audience": ["assistant"]}
        assert blocks[0]["meta"] == {"text_meta": True}
        assert blocks[1]["annotations"] == {"priority": 0.5}
        assert blocks[1]["meta"] == {"image_meta": 1}
        assert blocks[2]["annotations"] == {"audience": ["user"]}
        assert blocks[2]["meta"] == {"audio_meta": 2}

        text_resource = blocks[3]
        assert text_resource["annotations"] == {"outer_annotation": "yes"}
        assert text_resource["meta"] == {"outer_meta": True}
        assert text_resource["resource"]["meta"] == {"nested_meta": "text"}

        blob_resource = blocks[4]
        assert blob_resource["meta"] == {"outer_blob_meta": True}
        assert blob_resource["resource"]["meta"] == {"nested_meta": "blob"}
        assert blob_resource["byte_length"] == len(b"metadata-binary")
        assert "blob" not in blob_resource["resource"]

        link = blocks[5]
        assert link["title"] == "Data"
        assert link["description"] == "A data link"
        assert link["icons"][0]["src"] == "https://example.test/data-icon.png"
        assert link["annotations"] == {"link_annotation": "yes"}
        assert link["meta"] == {"link_meta": True}
        assert json.loads(out) == payload

    @pytest.mark.parametrize("field", ["image", "blob"])
    def test_malformed_base64_is_strict_and_bounded(self, monkeypatch, capsys, field):
        malformed = "QU J D-not-a-payload"
        if field == "image":
            content = [ImageContent(type="image", data=malformed, mimeType="image/png")]
        else:
            content = [
                EmbeddedResource(
                    type="resource",
                    resource=BlobResourceContents(
                        uri="file:///data.bin",
                        mimeType="application/octet-stream",
                        blob=malformed,
                    ),
                )
            ]
        _install_fake_server(monkeypatch, result=CallToolResult(content=content))
        payload, code, _, out = _run_structured_document(_config("fake"), capsys)

        assert code == EXIT_SUCCESS
        binary = payload["content"][0]
        location = binary if field == "image" else binary["resource"]
        assert location["data_encoding"] == "base64"
        assert location["encoded_length"] == len(malformed)
        assert "byte_length" not in location
        assert malformed not in out


class FutureContent(BaseModel):
    type: str = "future"
    nested: dict


def test_unknown_future_content_sanitizes_nested_binary_fields():
    raw = b"future-binary"
    encoded = base64.b64encode(raw).decode("ascii")
    blocks = _normalize_content(
        [
            FutureContent(
                nested={
                    "first": {"data": encoded},
                    "second": [{"blob": encoded}],
                    "safe": "metadata",
                }
            )
        ]
    )

    serialized = json.dumps(blocks, sort_keys=True)
    assert encoded not in serialized
    assert blocks[0]["nested"]["first"]["data"] == {
        "byte_length": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }
    assert blocks[0]["nested"]["second"][0]["blob"]["byte_length"] == len(raw)
    assert blocks[0]["nested"]["safe"] == "metadata"


def test_mcp_is_error_none_normalizes_to_json_false(monkeypatch, capsys):
    result = CallToolResult(
        content=[TextContent(type="text", text="ok")], isError=False
    )
    # MCP's Pydantic model normally rejects None at construction time, but a
    # transport adapter can still supply it on a result-like object.
    result.isError = None
    _install_fake_server(
        monkeypatch,
        result=result,
    )
    payload, code, _, out = _run_structured_document(_config("fake"), capsys)

    assert code == EXIT_SUCCESS
    assert payload["mcp_is_error"] is False
    assert json.loads(out)["mcp_is_error"] is False


# ---------------------------------------------------------------------------
# Lifecycle: stop/shutdown runs after every outcome
# ---------------------------------------------------------------------------


class TestLifecycleStopsServer:
    @pytest.mark.parametrize(
        ("result", "error", "start_error", "server_id", "tool", "exits"),
        [
            (_text_result("ok"), None, None, "fake", "mytool", False),
            (_text_result("boom", is_error=True), None, None, "fake", "mytool", True),
            (
                _text_result(_devcore_run_text(exit_code=1)),
                None,
                None,
                "devcore",
                "devcore_run",
                True,
            ),
            (None, RuntimeError("mid-call"), None, "fake", "mytool", True),
            (None, None, RuntimeError("no spawn"), "fake", "mytool", True),
        ],
        ids=[
            "success",
            "mcp-error",
            "nested-failure",
            "call-exception",
            "start-exception",
        ],
    )
    def test_stop_runs_after_every_outcome(
        self, monkeypatch, capsys, result, error, start_error, server_id, tool, exits
    ):
        created = _install_fake_server(
            monkeypatch, result=result, error=error, start_error=start_error
        )
        config = _config("fake", "devcore")

        if exits:
            with pytest.raises(SystemExit):
                cmd_call(config, server_id, tool, "{}", structured=True)
        else:
            cmd_call(config, server_id, tool, "{}", structured=True)

        assert len(created) == 1
        assert created[0].stopped is True

    def test_stop_runs_after_structured_success(self, monkeypatch, capsys):
        created = _install_fake_server(monkeypatch, result=_text_result("ok"))
        _run_structured(_config("fake"), capsys)

        assert created[0].stopped is True


# ---------------------------------------------------------------------------
# Legacy text mode compatibility
# ---------------------------------------------------------------------------


class TestLegacyTextMode:
    def test_success_output_shape_unchanged(self, monkeypatch, capsys):
        _install_fake_server(monkeypatch, result=_text_result("hello"))
        code, out, _ = _run_legacy(_config("fake"), capsys)

        assert code == EXIT_SUCCESS
        assert out == "hello\n"

    def test_multiple_text_blocks_all_printed_in_order(self, monkeypatch, capsys):
        _install_fake_server(monkeypatch, result=_text_result("one", "two"))
        code, out, _ = _run_legacy(_config("fake"), capsys)

        assert code == EXIT_SUCCESS
        assert out == "one\ntwo\n"

    def test_binary_content_legacy_shape_unchanged(self, monkeypatch, capsys):
        # Documented legacy limitation: non-text content prints its raw
        # payload (today: the base64 string). Compatibility keeps this.
        png_b64 = base64.b64encode(b"\x89PNG").decode("ascii")
        result = CallToolResult(
            content=[ImageContent(type="image", data=png_b64, mimeType="image/png")]
        )
        _install_fake_server(monkeypatch, result=result)
        code, out, _ = _run_legacy(_config("fake"), capsys)

        assert code == EXIT_SUCCESS
        assert out == f"{png_b64}\n"

    def test_exception_output_shape_and_exit_unchanged(self, monkeypatch, capsys):
        _install_fake_server(monkeypatch, error=RuntimeError("kaboom"))
        code, out, _ = _run_legacy(_config("fake"), capsys)

        assert code == 1
        assert out == "error: fake/mytool — kaboom\n"

    def test_mcp_error_now_exits_nonzero_with_shape_unchanged(
        self, monkeypatch, capsys
    ):
        # Behavior change (documented): content still prints exactly as
        # before, but isError no longer exits zero.
        _install_fake_server(monkeypatch, result=_text_result("boom", is_error=True))
        code, out, err = _run_legacy(_config("fake"), capsys)

        assert code == 1
        assert out == "boom\n"
        assert err != ""

    def test_nested_devcore_failure_now_exits_nonzero(self, monkeypatch, capsys):
        _install_fake_server(
            monkeypatch, result=_text_result(_devcore_run_text(exit_code=1))
        )
        code, out, _ = _run_legacy(
            _config("devcore"), capsys, server_id="devcore", tool="devcore_run"
        )

        assert code == 1
        assert '"ok": false' in out or '"ok":false' in out

    def test_domain_ok_false_still_exits_zero_in_legacy(self, monkeypatch, capsys):
        domain_payload = json.dumps({"ok": False, "reason": "domain-level"})
        _install_fake_server(monkeypatch, result=_text_result(domain_payload))
        code, out, _ = _run_legacy(_config("domain"), capsys, server_id="domain")

        assert code == EXIT_SUCCESS
        assert "domain-level" in out

    def test_legacy_usage_error_exit_unchanged(self, monkeypatch, capsys):
        _install_fake_server(monkeypatch)
        code, out, _ = _run_legacy(_config("fake"), capsys, server_id="ghost")

        assert code == 1
        assert "not found" in out
