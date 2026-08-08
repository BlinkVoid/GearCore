"""Tests for signed, capability-constraining launch envelopes."""

from __future__ import annotations

import base64
import json
import sys
import time
from pathlib import Path

import pytest
import yaml
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from mcp.types import ListToolsRequest

from gearcore_hub.config import load_config
from gearcore_hub.envelope import canonical_envelope_bytes
from gearcore_hub.main import GearCoreHub, cmd_call, cmd_list_skills, cmd_status
from gearcore_hub.main import main as cli_main
from gearcore_hub.skill_manager import SkillManager

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


@pytest.mark.parametrize(
    ("envelope_value", "key_value"),
    [("", ""), ("   ", None), (None, "   ")],
)
def test_empty_config_api_envelope_inputs_are_still_explicit(
    tmp_path: Path,
    envelope_value: str | None,
    key_value: str | None,
):
    result = load_config(
        global_config_path=_global_config(tmp_path),
        context_envelope=envelope_value,
        envelope_public_key=key_value,
        now=NOW,
    )

    assert result.diagnostic_only is True
    assert result.profile_source == "invalid-envelope"
    assert result.enforced_profile_name is None
    assert result.mcp_servers == []
    assert result.diagnostic_codes == ("invalid_launch_envelope",)


@pytest.mark.parametrize("blank_target", ["envelope", "public-key", "both"])
def test_whitespace_only_cli_values_are_rejected_even_when_named_files_are_valid(
    tmp_path: Path, signing_material, monkeypatch, capsys, blank_target: str
):
    private_key, public_key_path = signing_material
    current_time = int(time.time())
    envelope_path, _ = _signed_envelope(
        tmp_path,
        private_key,
        issued_at=current_time - 10,
        expires_at=current_time + 60,
    )
    envelope_argument = str(envelope_path)
    key_argument = str(public_key_path)

    if blank_target in ("envelope", "both"):
        whitespace_envelope = tmp_path / " "
        envelope_path.rename(whitespace_envelope)
        envelope_argument = " "
    if blank_target in ("public-key", "both"):
        whitespace_key = tmp_path / "  "
        public_key_path.rename(whitespace_key)
        key_argument = "  "

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "gearcore",
            "--config",
            str(_global_config(tmp_path)),
            "--context-envelope",
            envelope_argument,
            "--envelope-public-key",
            key_argument,
            "status",
        ],
    )

    cli_main()

    output = capsys.readouterr().out
    assert "invalid_launch_envelope" in output
    assert "Profile: hive-worker" not in output
    assert "launch-safe" not in output
    assert "nonce-safe" not in output


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
        project=tmp_path,
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


def test_alternate_profile_cannot_drop_envelope_protected_binding(
    tmp_path: Path, signing_material
):
    private_key, public_key_path = signing_material
    config_path = tmp_path / "protected-config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "version": 3,
                "registry": {
                    "mcp_servers": [
                        {
                            "id": "gateway",
                            "type": "stdio",
                            "command": "/trusted/gateway",
                        }
                    ]
                },
                "profiles": {
                    "default": "operator",
                    "entries": {
                        "operator": {},
                        "protected-worker": {
                            "constrained": True,
                            "scope": {
                                "mcp_servers": {"protected": ["gateway"]}
                            },
                        },
                        "unprotected-alternate": {"constrained": True},
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    envelope_path, _ = _signed_envelope(
        tmp_path, private_key, profile="protected-worker"
    )
    project = tmp_path / "hostile-project"
    project_config = project / ".gearcore" / "config.yaml"
    project_config.parent.mkdir(parents=True)
    project_config.write_text(
        yaml.safe_dump(
            {
                "version": 2,
                "registry": {
                    "mcp_servers": [
                        {
                            "id": "gateway",
                            "type": "stdio",
                            "command": "/unsafe/replacement",
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )

    result = _load(
        config_path,
        envelope_path,
        public_key_path,
        profile="unprotected-alternate",
        project=project,
    )

    assert result.diagnostic_only is True
    assert result.profile_source == "envelope"
    assert result.enforced_profile_name == "protected-worker"
    assert result.mcp_servers == []
    assert result.diagnostic_codes == ("envelope_authority_expansion",)


@pytest.mark.parametrize("overlay_rule", ["include", "deny"])
def test_alternate_profile_cannot_regain_capabilities_removed_by_enforced_overlay(
    tmp_path: Path, signing_material, overlay_rule: str
):
    private_key, public_key_path = signing_material
    config_path = tmp_path / "overlay-config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "version": 3,
                "registry": {
                    "mcp_servers": [
                        {"id": "gateway", "command": "/trusted/gateway"},
                        {"id": "common", "command": "/trusted/common"},
                    ]
                },
                "profiles": {
                    "default": "operator",
                    "entries": {
                        "operator": {},
                        "worker": {
                            "constrained": True,
                            "scope": {
                                "mcp_servers": {
                                    "include": ["gateway", "common"]
                                },
                                "skills": {
                                    "include": ["worker-skill", "common-skill"]
                                },
                            },
                        },
                        "alternate": {
                            "constrained": True,
                            "scope": {
                                "mcp_servers": {"include": ["common"]},
                                "skills": {"include": ["common-skill"]},
                            },
                        },
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    worker_rules = (
        {
            "mcp_servers": {"include": ["gateway"]},
            "skills": {"include": ["worker-skill"]},
        }
        if overlay_rule == "include"
        else {
            "mcp_servers": {"deny": ["common"]},
            "skills": {"deny": ["common-skill"]},
        }
    )
    project = tmp_path / "overlay-project"
    project_config = project / ".gearcore" / "config.yaml"
    project_config.parent.mkdir(parents=True)
    project_config.write_text(
        yaml.safe_dump(
            {
                "version": 3,
                "profiles": {
                    "entries": {
                        "worker": {"scope": worker_rules},
                        "alternate": {
                            "scope": {
                                "mcp_servers": {"include": ["common"]},
                                "skills": {"include": ["common-skill"]},
                            }
                        },
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    envelope_path, _ = _signed_envelope(tmp_path, private_key, profile="worker")

    result = _load(
        config_path,
        envelope_path,
        public_key_path,
        profile="alternate",
        project=project,
    )

    assert result.diagnostic_only is True
    assert result.profile_source == "envelope"
    assert result.enforced_profile_name == "worker"
    assert result.mcp_servers == []
    assert result.diagnostic_codes == ("envelope_authority_expansion",)


@pytest.mark.parametrize(
    "identity_field",
    ["profile", "issuer", "launch_id", "execution_id", "task_id", "nonce"],
)
def test_correctly_signed_whitespace_identity_fields_fail_closed(
    tmp_path: Path, signing_material, identity_field: str
):
    private_key, public_key_path = signing_material
    config_path = _global_config(tmp_path)
    if identity_field == "profile":
        config_data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        config_data["profiles"]["entries"]["   "] = {"constrained": True}
        config_path.write_text(yaml.safe_dump(config_data), encoding="utf-8")

    envelope_path, envelope = _signed_envelope(tmp_path, private_key)
    envelope[identity_field] = "   "
    if identity_field == "issuer":
        key_document = json.loads(public_key_path.read_text(encoding="utf-8"))
        key_document["issuer"] = "   "
        public_key_path.write_text(json.dumps(key_document), encoding="utf-8")
    payload = {key: value for key, value in envelope.items() if key != "signature"}
    envelope["signature"] = base64.urlsafe_b64encode(
        private_key.sign(canonical_envelope_bytes(payload))
    ).decode("ascii").rstrip("=")
    envelope_path.write_text(json.dumps(envelope), encoding="utf-8")

    result = _load(config_path, envelope_path, public_key_path)

    assert result.diagnostic_only is True
    assert result.profile_source == "invalid-envelope"
    assert result.mcp_servers == []
    assert result.diagnostic_codes == ("invalid_launch_envelope",)


def test_diagnostic_only_never_scans_or_discloses_skill_roots(
    tmp_path: Path, signing_material, caplog, capsys
):
    _, public_key_path = signing_material
    private_target = tmp_path / "private" / "secret-skill-target"
    skills_dir = tmp_path / "private-skills"
    skills_dir.mkdir()
    (skills_dir / "broken-secret").symlink_to(private_target)
    config_path = _global_config(tmp_path)
    config_data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config_data["registry"]["skills_dirs"] = [str(skills_dir)]
    config_path.write_text(yaml.safe_dump(config_data), encoding="utf-8")
    project = tmp_path / "private-project"
    project_config = project / ".gearcore" / "config.yaml"
    project_config.parent.mkdir(parents=True)
    project_config.write_text(
        yaml.safe_dump(
            {"version": 2, "context": {"name": "secret-private-context"}}
        ),
        encoding="utf-8",
    )
    config = _load(
        config_path,
        tmp_path / "missing-envelope",
        public_key_path,
        project=project,
    )

    with caplog.at_level("DEBUG"):
        manager = SkillManager(config)
    cmd_list_skills(config)
    cmd_status(config)
    listing = capsys.readouterr().out
    logs = caplog.text

    assert config.diagnostic_only is True
    assert config.skills_dirs == []
    assert manager.skills == {}
    assert manager.broken_skills == {}
    assert manager.list_available_skills() == []
    assert str(skills_dir) not in logs
    assert str(private_target) not in logs
    assert str(skills_dir) not in listing
    assert str(private_target) not in listing
    assert "broken-secret" not in logs
    assert "broken-secret" not in listing
    assert "secret-private-context" not in listing


def test_status_reports_effective_and_enforced_profiles_and_server_ids(
    tmp_path: Path, signing_material, capsys
):
    private_key, public_key_path = signing_material
    config_path = _global_config(tmp_path)
    config_data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    for server in config_data["registry"]["mcp_servers"]:
        if server["id"] == "hive-gateway":
            server["command"] = "secret-backend-command"
    config_data["profiles"]["entries"]["observer"]["scope"]["mcp_servers"] = {
        "include": ["hive-gateway"],
        "deny": ["hive-dispatcher"],
    }
    config_path.write_text(yaml.safe_dump(config_data), encoding="utf-8")
    envelope_path, _ = _signed_envelope(tmp_path, private_key)
    config = _load(
        config_path,
        envelope_path,
        public_key_path,
        profile="observer",
        project=tmp_path,
    )

    cmd_status(config)
    output = capsys.readouterr().out

    assert "Profile: observer" in output
    assert "Enforced profile: hive-worker" in output
    assert "Active server IDs: hive-gateway" in output
    assert "Denied server IDs: hive-dispatcher" in output
    assert "secret-backend-command" not in output
    assert "launch-safe" not in output
    assert "nonce-safe" not in output


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
