"""Cross-surface conformance tests for constrained capability profiles."""

from __future__ import annotations

import base64
import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import anyio
import pytest
import yaml
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from mcp import ClientSession
from mcp.server import NotificationOptions
from mcp.server.models import InitializationOptions
from mcp.types import Tool

from gearcore_hub import main as main_module
from gearcore_hub.config import EffectiveConfig, load_config
from gearcore_hub.envelope import canonical_envelope_bytes
from gearcore_hub.main import GearCoreHub
from gearcore_hub.process_manager import ProcessManager
from gearcore_hub.skill_manager import SkillManager

ISSUER = "profile-conformance-launcher"
HOSTILE_SENTINELS = (
    "hostile-global-command-sentinel",
    "hostile-global-arg-sentinel",
    "hostile-project-command-sentinel",
    "hostile-global-sse-sentinel",
    "hostile-project-sse-sentinel",
    "hostile-global-http-sentinel",
    "hostile-project-http-sentinel",
    "hostile-global-auth-ref-sentinel",
    "hostile-project-auth-ref-sentinel",
    "HOSTILE_GLOBAL_AUTH_SENTINEL",
    "HOSTILE_PROJECT_AUTH_SENTINEL",
    "hostile-global-skill-sentinel",
    "hostile-project-skill-sentinel",
)


@dataclass(frozen=True)
class WorkerLaunch:
    config: EffectiveConfig
    config_path: Path
    project: Path
    envelope_path: Path
    public_key_path: Path

    @property
    def cli_prefix(self) -> list[str]:
        return [
            "gearcore",
            "--config",
            str(self.config_path),
            "--project",
            str(self.project),
            "--context-envelope",
            str(self.envelope_path),
            "--envelope-public-key",
            str(self.public_key_path),
        ]


def _write_skill(root: Path, name: str, server_id: str, marker: str) -> None:
    skill_dir = root / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(f"# {name}\n\n{marker}\n", encoding="utf-8")
    (skill_dir / "manifest.json").write_text(
        json.dumps(
            {
                "name": name,
                "description": marker,
                "mcp_servers": [
                    {"server_id": server_id, "tools": [f"{name}-tool"]}
                ],
            }
        ),
        encoding="utf-8",
    )


def _signed_worker_config(tmp_path: Path, transport: str) -> WorkerLaunch:
    global_skills = tmp_path / "global-skills"
    project = tmp_path / "project"
    project_skills = project / ".gearcore" / "skills"
    _write_skill(global_skills, "gateway", "hive-gateway", "gateway-available")
    _write_skill(
        global_skills,
        "hive-dispatcher",
        "hive-dispatcher",
        "hostile-global-skill-sentinel",
    )
    _write_skill(
        project_skills,
        "hive-dispatcher",
        "hive-dispatcher",
        "hostile-project-skill-sentinel",
    )

    hostile_global: dict[str, object] = {
        "id": "hive-dispatcher",
        "type": transport,
    }
    hostile_project = dict(hostile_global)
    if transport == "stdio":
        hostile_global.update(
            command="hostile-global-command-sentinel",
            args=["hostile-global-arg-sentinel"],
            auth={
                "credential_ref": "hostile-global-auth-ref-sentinel",
                "stdio_environment": "HOSTILE_GLOBAL_AUTH_SENTINEL",
            },
        )
        hostile_project.update(
            command="hostile-project-command-sentinel",
            auth={
                "credential_ref": "hostile-project-auth-ref-sentinel",
                "stdio_environment": "HOSTILE_PROJECT_AUTH_SENTINEL",
            },
        )
    else:
        hostile_global["url"] = f"https://hostile-global-{transport}-sentinel.invalid/mcp"
        hostile_project["url"] = f"https://hostile-project-{transport}-sentinel.invalid/mcp"
        hostile_global["auth"] = {
            "credential_ref": "hostile-global-auth-ref-sentinel",
            "http_scheme": "bearer",
        }
        hostile_project["auth"] = {
            "credential_ref": "hostile-project-auth-ref-sentinel",
            "http_scheme": "bearer",
        }

    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "version": 3,
                "registry": {
                    "skills_dirs": [str(global_skills)],
                    "mcp_servers": [
                        {
                            "id": "hive-gateway",
                            "type": "stdio",
                            "command": "gateway-command",
                        },
                        hostile_global,
                    ],
                },
                "profiles": {
                    "default": "operator",
                    "entries": {
                        "operator": {
                            "scope": {
                                "mcp_servers": {
                                    "include": ["hive-gateway", "hive-dispatcher"]
                                },
                                "skills": {
                                    "include": ["gateway", "hive-dispatcher"]
                                },
                            }
                        },
                        "hive-worker": {
                            "constrained": True,
                            "scope": {
                                "mcp_servers": {
                                    "include": ["hive-gateway"],
                                    "deny": ["hive-dispatcher"],
                                },
                                "skills": {
                                    "include": ["gateway"],
                                    "deny": ["hive-dispatcher"],
                                },
                            },
                        },
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    project_config = project / ".gearcore" / "config.yaml"
    project_config.parent.mkdir(parents=True, exist_ok=True)
    project_config.write_text(
        yaml.safe_dump(
            {
                "version": 3,
                "registry": {"mcp_servers": [hostile_project]},
                "profiles": {
                    "entries": {
                        "hive-worker": {
                            "scope": {
                                "mcp_servers": {
                                    "include": ["hive-gateway", "hive-dispatcher"],
                                    "deny": ["hive-dispatcher"],
                                },
                                "skills": {
                                    "include": ["gateway", "hive-dispatcher"],
                                    "deny": ["hive-dispatcher"],
                                },
                            }
                        }
                    }
                },
            }
        ),
        encoding="utf-8",
    )

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
    now = int(time.time())
    payload: dict[str, object] = {
        "version": 1,
        "profile": "hive-worker",
        "issuer": ISSUER,
        "launch_id": "conformance-launch",
        "execution_id": "conformance-execution",
        "task_id": "conformance-task",
        "issued_at": now - 10,
        "expires_at": now + 300,
        "nonce": "conformance-nonce",
    }
    signature = private_key.sign(canonical_envelope_bytes(payload))
    envelope_path = tmp_path / "envelope.json"
    envelope_path.write_text(
        json.dumps(
            {
                **payload,
                "signature": base64.urlsafe_b64encode(signature)
                .decode("ascii")
                .rstrip("="),
            }
        ),
        encoding="utf-8",
    )
    config = load_config(
        project=project,
        global_config_path=config_path,
        context_envelope=envelope_path,
        envelope_public_key=public_key_path,
        now=now,
    )
    assert config.profile_name == "hive-worker"
    assert config.enforced_profile_name == "hive-worker"
    assert [server.id for server in config.mcp_servers] == ["hive-gateway"]
    return WorkerLaunch(
        config=config,
        config_path=config_path,
        project=project,
        envelope_path=envelope_path,
        public_key_path=public_key_path,
    )


@pytest.mark.parametrize("transport", ["stdio", "sse", "http"])
def test_process_manager_cannot_construct_denied_raw_definition(
    tmp_path: Path, transport: str
) -> None:
    """A manager must be bound to one effective config, not accept raw registry data."""

    launch = _signed_worker_config(tmp_path, transport)
    manager = ProcessManager(launch.config)

    with pytest.raises(KeyError, match="capability_denied"):
        manager.build_server("hive-dispatcher")


class _GatewaySession:
    async def list_tools(self) -> Any:
        return SimpleNamespace(
            tools=[
                Tool(
                    name="gateway-tool",
                    description="safe gateway",
                    inputSchema={"type": "object", "properties": {}},
                )
            ]
        )

    async def call_tool(self, _name: str, _arguments: dict[str, Any]) -> Any:
        return SimpleNamespace(content=[])


class _GatewayServer:
    authenticated = False

    def __init__(self) -> None:
        self.session: _GatewaySession | None = None
        self.starts = 0
        self.stops = 0

    async def start(self) -> None:
        self.starts += 1
        self.session = _GatewaySession()

    async def stop(self) -> None:
        self.stops += 1
        self.session = None


async def _drive_real_hub_session(hub: GearCoreHub) -> dict[str, object]:
    """Exercise the registered MCP handlers through a real SDK client session."""

    client_send, server_receive = anyio.create_memory_object_stream(10)
    server_send, client_receive = anyio.create_memory_object_stream(10)
    options = InitializationOptions(
        server_name="gearcore-hub",
        server_version="test",
        capabilities=hub.server.get_capabilities(
            notification_options=NotificationOptions(),
            experimental_capabilities={},
        ),
    )
    await hub._start_backends()
    result: dict[str, object] = {}
    async with (
        client_send,
        server_receive,
        server_send,
        client_receive,
        anyio.create_task_group() as task_group,
    ):
        task_group.start_soon(
            hub.server.run,
            server_receive,
            server_send,
            options,
        )
        async with ClientSession(client_receive, client_send) as session:
            await session.initialize()
            result["before"] = {
                tool.name for tool in (await session.list_tools()).tools
            }
            result["denied"] = await session.call_tool(
                "request_skill", {"name": "hive-dispatcher"}
            )
            result["gateway"] = await session.call_tool(
                "request_skill", {"name": "gateway"}
            )
            result["after"] = {
                tool.name for tool in (await session.list_tools()).tools
            }
            hub.skill_manager.refresh()
            result["after_refresh"] = {
                tool.name for tool in (await session.list_tools()).tools
            }
        task_group.cancel_scope.cancel()
    await hub._shutdown()
    return result


@pytest.mark.parametrize("transport", ["stdio", "sse", "http"])
@pytest.mark.parametrize(
    "surface", ["status", "list-skills", "request-skill", "call", "serve"]
)
def test_constrained_profile_denial_is_identical_everywhere(
    tmp_path: Path,
    transport: str,
    surface: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    launch = _signed_worker_config(tmp_path, transport)
    load_calls: list[EffectiveConfig] = []
    component_config_ids: list[tuple[str, int]] = []
    built_ids: list[str] = []
    transport_connects: list[str] = []
    serve_result: dict[str, object] = {}
    gateway_servers: list[_GatewayServer] = []

    real_load_config = main_module.load_config
    real_process_init = ProcessManager.__init__
    real_skill_init = SkillManager.__init__

    def recording_load_config(*args: Any, **kwargs: Any) -> EffectiveConfig:
        config = real_load_config(*args, **kwargs)
        load_calls.append(config)
        return config

    def recording_process_init(
        manager: ProcessManager,
        config: EffectiveConfig,
        credential_store: Any = None,
    ) -> None:
        component_config_ids.append(("process", id(config)))
        real_process_init(manager, config, credential_store)

    def recording_skill_init(
        manager: SkillManager, config: EffectiveConfig
    ) -> None:
        component_config_ids.append(("skill", id(config)))
        real_skill_init(manager, config)

    def safe_build(manager: ProcessManager, server_id: str) -> _GatewayServer:
        built_ids.append(server_id)
        assert manager.config is load_calls[0]
        assert server_id == "hive-gateway"
        server = _GatewayServer()
        gateway_servers.append(server)
        return server

    def forbidden_transport(*_args: Any, **_kwargs: Any) -> None:
        transport_connects.append(transport)
        raise AssertionError("denied transport was constructed")

    async def run_hub(hub: GearCoreHub) -> None:
        serve_result.update(await _drive_real_hub_session(hub))

    monkeypatch.setattr(main_module, "load_config", recording_load_config)
    monkeypatch.setattr(ProcessManager, "__init__", recording_process_init)
    monkeypatch.setattr(SkillManager, "__init__", recording_skill_init)
    monkeypatch.setattr(ProcessManager, "build_server", safe_build)
    monkeypatch.setattr("gearcore_hub.process_manager.stdio_client", forbidden_transport)
    monkeypatch.setattr("gearcore_hub.process_manager.sse_client", forbidden_transport)
    monkeypatch.setattr(
        "gearcore_hub.process_manager.streamablehttp_client", forbidden_transport
    )
    monkeypatch.setattr(GearCoreHub, "run", run_hub)

    argv = [*launch.cli_prefix, surface]
    if surface == "request-skill":
        argv.append("hive-dispatcher")
    elif surface == "call":
        argv.extend(["hive-dispatcher", "swarm_execute", "{}"])
    monkeypatch.setattr(sys, "argv", argv)

    exit_code = 0
    try:
        main_module.main()
    except SystemExit as exc:
        exit_code = int(exc.code or 0)

    captured = capsys.readouterr()
    rendered = f"{captured.out}\n{captured.err}\n{caplog.text}"
    assert len(load_calls) == 1
    assert load_calls[0].profile_name == "hive-worker"
    assert load_calls[0].active_mcp_server_ids == ("hive-gateway",)
    assert load_calls[0].denied_mcp_server_ids == ("hive-dispatcher",)
    assert all(config_id == id(load_calls[0]) for _, config_id in component_config_ids)
    assert transport_connects == []
    assert "hive-dispatcher" not in built_ids
    assert all(sentinel not in rendered for sentinel in HOSTILE_SENTINELS)
    assert all(
        str(path) not in rendered
        for path in (
            launch.config_path,
            launch.project,
            launch.envelope_path,
            launch.public_key_path,
        )
    )

    if surface == "status":
        assert exit_code == 0
        assert "active_mcp: hive-gateway" in captured.out
        assert "denied_mcp: hive-dispatcher" in captured.out
        assert "active_skills: gateway" in captured.out
    elif surface == "list-skills":
        assert exit_code == 0
        assert "gateway" in captured.out
        assert "hostile" not in captured.out
    elif surface == "request-skill":
        assert exit_code == 1
        assert "not found or not visible" in captured.err
    elif surface == "call":
        assert exit_code == 1
        assert captured.out.strip() == "error: capability_denied"
    else:
        assert exit_code == 0
        assert built_ids == ["hive-gateway"]
        assert gateway_servers[0].starts == 1
        for phase in ("before", "after", "after_refresh"):
            tools = serve_result[phase]
            assert isinstance(tools, set)
            assert all("dispatcher" not in tool for tool in tools)
        assert "gateway-tool" not in serve_result["before"]
        assert any(
            tool.endswith("gateway-tool") for tool in serve_result["after"]
        )
        assert any(
            tool.endswith("gateway-tool")
            for tool in serve_result["after_refresh"]
        )
        denied = serve_result["denied"]
        assert "not found or not visible" in denied.content[0].text  # type: ignore[union-attr]


def test_one_shot_call_uses_same_effective_config_and_only_effective_gateway(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launch = _signed_worker_config(tmp_path, "http")
    loaded: list[EffectiveConfig] = []
    seen: list[tuple[EffectiveConfig, str]] = []
    real_load_config = main_module.load_config

    class CallSession:
        async def call_tool(self, _tool: str, _arguments: dict[str, object]) -> Any:
            return SimpleNamespace(content=[])

    class CallServer:
        session = CallSession()

        async def start(self) -> None:
            return None

        async def stop(self) -> None:
            return None

    def build(manager: ProcessManager, server_id: str) -> CallServer:
        seen.append((manager.config, server_id))
        return CallServer()

    def recording_load_config(*args: Any, **kwargs: Any) -> EffectiveConfig:
        config = real_load_config(*args, **kwargs)
        loaded.append(config)
        return config

    monkeypatch.setattr(main_module, "load_config", recording_load_config)
    monkeypatch.setattr(ProcessManager, "build_server", build)
    monkeypatch.setattr(
        sys,
        "argv",
        [*launch.cli_prefix, "call", "hive-gateway", "health", "{}"],
    )
    main_module.main()

    assert len(loaded) == 1
    assert seen == [(loaded[0], "hive-gateway")]
