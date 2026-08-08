"""Tests for signed, capability-constraining launch envelopes."""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest
import yaml
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from mcp.types import ListToolsRequest

from gearcore_hub.config import load_config
from gearcore_hub.envelope import canonical_envelope_bytes
from gearcore_hub.main import GearCoreHub, cmd_call, cmd_status

NOW = 1_800_000_000
ISSUER = "test-worker-spawner"


def _global_config(tmp_path: Path) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "version": 3,
                "registry": {
                    "mcp_servers": [
                        {
                            "id": "hive-dispatcher",
                            "type": "stdio",
                            "command": "/trusted/dispatcher",
                        },
                        {
                            "id": "hive-gateway",
                            "type": "stdio",
                            "command": "/trusted/gateway",
                        },
                    ]
                },
                "profiles": {
                    "default": "operator",
                    "entries": {
                        "operator": {
                            "scope": {
                                "mcp_servers": {
                                    "include": ["hive-dispatcher", "hive-gateway"]
                                },
                                "skills": {
                                    "include": ["operator", "common"]
                                },
                            }
                        },
                        "hive-worker": {
                            "constrained": True,
                            "scope": {
                                "mcp_servers": {"include": ["hive-gateway"]},
                                "skills": {"include": ["worker", "common"]},
                            },
                        },
                        "observer": {
                            "scope": {
                                "mcp_servers": {"include": []},
                                "skills": {"include": ["common"]},
                            },
                        },
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    return path


@pytest.fixture
def signing_material(tmp_path: Path):
    private_key = Ed25519PrivateKey.generate()
    raw_public_key = private_key.public_key().public_bytes(
        Encoding.Raw, PublicFormat.Raw
    )
    public_key_path = tmp_path / "public-key.json"
    public_key_path.write_text(
        json.dumps(
            {
                "version": 1,
                "issuer": ISSUER,
                "public_key": base64.urlsafe_b64encode(raw_public_key)
                .decode("ascii")
                .rstrip("="),
            }
        ),
        encoding="utf-8",
    )
    return private_key, public_key_path


def _signed_envelope(
    tmp_path: Path,
    private_key: Ed25519PrivateKey,
    *,
    profile: str = "hive-worker",
    issuer: str = ISSUER,
    issued_at: int = NOW - 10,
    expires_at: int = NOW + 60,
) -> tuple[Path, dict[str, object]]:
    payload: dict[str, object] = {
        "version": 1,
        "profile": profile,
        "issuer": issuer,
        "launch_id": "launch-safe",
        "execution_id": "execution-safe",
        "task_id": "task-safe",
        "issued_at": issued_at,
        "expires_at": expires_at,
        "nonce": "nonce-safe",
    }
    signature = private_key.sign(canonical_envelope_bytes(payload))
    envelope = {
        **payload,
        "signature": base64.urlsafe_b64encode(signature)
        .decode("ascii")
        .rstrip("="),
    }
    path = tmp_path / "envelope.json"
    # Deliberately non-canonical on disk: verification must canonicalize the payload.
    path.write_text(json.dumps(envelope, indent=3), encoding="utf-8")
    return path, envelope


def _load(
    config_path: Path,
    envelope_path: Path | None,
    public_key_path: Path | None,
    *,
    profile: str | None = None,
    project: Path | None = None,
):
    return load_config(
        project=project,
        global_config_path=config_path,
        profile_name=profile,
        context_envelope=envelope_path,
        envelope_public_key=public_key_path,
        now=NOW,
    )


def test_canonical_signature_selects_valid_worker_profile(
    tmp_path: Path, signing_material
):
    private_key, public_key_path = signing_material
    envelope_path, _ = _signed_envelope(tmp_path, private_key)

    result = _load(_global_config(tmp_path), envelope_path, public_key_path)

    assert result.diagnostic_only is False
    assert result.profile_name == "hive-worker"
    assert result.enforced_profile_name == "hive-worker"
    assert result.profile_source == "envelope"
    assert [server.id for server in result.mcp_servers] == ["hive-gateway"]


@pytest.mark.parametrize(
    ("mutation", "unsafe_fragment"),
    [
        (lambda data: data.update({"task_id": "secret-tampered-task"}), "secret-tampered-task"),
        (lambda data: data.update({"expires_at": NOW - 1}), "nonce-safe"),
        (lambda data: data.update({"issuer": "unknown-secret-issuer"}), "unknown-secret-issuer"),
        (lambda data: data.update({"profile": "unknown-secret-profile"}), "unknown-secret-profile"),
        (lambda data: data.update({"signature": "not+base64/secret"}), "not+base64/secret"),
        (lambda data: data.update({"signature": "abc=="}), "abc=="),
    ],
)
def test_invalid_envelope_fails_closed_without_leaking_definition(
    tmp_path: Path, signing_material, mutation, unsafe_fragment: str
):
    private_key, public_key_path = signing_material
    envelope_path, envelope = _signed_envelope(tmp_path, private_key)
    mutation(envelope)
    envelope_path.write_text(json.dumps(envelope), encoding="utf-8")

    result = _load(_global_config(tmp_path), envelope_path, public_key_path)

    assert result.diagnostic_only is True
    assert result.profile_name == "unavailable"
    assert result.profile_source == "invalid-envelope"
    assert result.mcp_servers == []
    assert result.diagnostic_codes == ("invalid_launch_envelope",)
    assert unsafe_fragment not in repr(result.diagnostic_codes)


def test_unknown_issuer_is_rejected_even_with_a_valid_signature(
    tmp_path: Path, signing_material
):
    private_key, public_key_path = signing_material
    envelope_path, _ = _signed_envelope(
        tmp_path, private_key, issuer="different-signed-issuer"
    )

    result = _load(_global_config(tmp_path), envelope_path, public_key_path)

    assert result.diagnostic_only is True
    assert result.diagnostic_codes == ("invalid_launch_envelope",)


@pytest.mark.parametrize("missing", ["envelope", "public-key"])
def test_explicit_missing_envelope_input_never_falls_back_to_operator(
    tmp_path: Path, signing_material, missing: str
):
    private_key, public_key_path = signing_material
    envelope_path, _ = _signed_envelope(tmp_path, private_key)
    if missing == "envelope":
        envelope_path = tmp_path / "secret-missing-envelope.json"
    else:
        public_key_path = tmp_path / "secret-missing-key.json"

    result = _load(_global_config(tmp_path), envelope_path, public_key_path)

    assert result.diagnostic_only is True
    assert result.profile_source == "invalid-envelope"
    assert result.mcp_servers == []
    assert "secret-missing" not in repr(result.diagnostic_codes)


@pytest.mark.parametrize("supplied", ["envelope", "public-key"])
def test_one_sided_explicit_envelope_input_fails_closed(
    tmp_path: Path, signing_material, supplied: str
):
    private_key, public_key_path = signing_material
    envelope_path, _ = _signed_envelope(tmp_path, private_key)

    result = _load(
        _global_config(tmp_path),
        envelope_path if supplied == "envelope" else None,
        public_key_path if supplied == "public-key" else None,
    )

    assert result.diagnostic_only is True
    assert result.profile_source == "invalid-envelope"
    assert result.enforced_profile_name is None
    assert result.mcp_servers == []


def test_valid_envelope_rejects_requested_authority_expansion(
    tmp_path: Path, signing_material
):
    private_key, public_key_path = signing_material
    envelope_path, _ = _signed_envelope(tmp_path, private_key)

    result = _load(
        _global_config(tmp_path),
        envelope_path,
        public_key_path,
        profile="operator",
    )

    assert result.diagnostic_only is True
    assert result.profile_source == "envelope"
    assert result.enforced_profile_name == "hive-worker"
    assert result.mcp_servers == []
    assert result.diagnostic_codes == ("envelope_authority_expansion",)


def test_valid_envelope_allows_requested_profile_only_when_it_is_narrower(
    tmp_path: Path, signing_material
):
    private_key, public_key_path = signing_material
    envelope_path, _ = _signed_envelope(tmp_path, private_key)

    result = _load(
        _global_config(tmp_path),
        envelope_path,
        public_key_path,
        profile="observer",
    )

    assert result.diagnostic_only is False
    assert result.profile_name == "observer"
    assert result.enforced_profile_name == "hive-worker"
    assert result.profile_source == "envelope"
    assert result.mcp_servers == []


def test_envelope_still_blocks_project_expansion_for_narrower_unconstrained_profile(
    tmp_path: Path, signing_material
):
    private_key, public_key_path = signing_material
    envelope_path, _ = _signed_envelope(tmp_path, private_key)
    project = tmp_path / "project"
    project_config = project / ".gearcore" / "config.yaml"
    project_config.parent.mkdir(parents=True)
    project_config.write_text(
        yaml.safe_dump(
            {
                "version": 2,
                "registry": {
                    "mcp_servers": [
                        {
                            "id": "shell-root",
                            "type": "stdio",
                            "command": "/unsafe/shell-root",
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )

    result = _load(
        _global_config(tmp_path),
        envelope_path,
        public_key_path,
        profile="observer",
        project=project,
    )

    assert result.diagnostic_only is False
    assert result.profile.constrained is False
    assert result.enforced_profile_name == "hive-worker"
    assert result.mcp_servers == []


@pytest.mark.asyncio
async def test_diagnostic_only_status_call_and_serve_are_fail_closed(
    tmp_path: Path, signing_material, capsys, monkeypatch
):
    _, public_key_path = signing_material
    missing_path = tmp_path / "secret-envelope-path"
    config = _load(_global_config(tmp_path), missing_path, public_key_path)

    cmd_status(config)
    status = capsys.readouterr().out
    assert "invalid_launch_envelope" in status
    assert "secret-envelope-path" not in status

    with pytest.raises(SystemExit) as exit_info:
        cmd_call(config, "hive-dispatcher", "swarm_execute", "{}")
    assert exit_info.value.code == 1
    call_output = capsys.readouterr().out
    assert "invalid_launch_envelope" in call_output

    hub = GearCoreHub(config)
    started = False

    async def unexpected_start(_definition):
        nonlocal started
        started = True

    monkeypatch.setattr(hub.process_manager, "register_and_start", unexpected_start)
    await hub._start_backends()
    assert started is False

    response = await hub.server.request_handlers[ListToolsRequest](None)
    assert {tool.name for tool in response.root.tools} == {
        "list_skills",
        "request_skill",
        "capability_diagnostic",
    }
