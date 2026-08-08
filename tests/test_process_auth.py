"""Authentication boundary tests for MCP client transports."""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pytest
from mcp.types import CallToolRequest, CallToolRequestParams, ListToolsRequest, Tool
from pydantic import SecretStr

from gearcore_hub.config import McpServerConfig, load_config
from gearcore_hub.credentials import CredentialError
from gearcore_hub.main import GearCoreHub, cmd_call, cmd_status
from gearcore_hub.process_manager import ProcessManager, SharedMCPServer

SECRET = "sentinel-runtime-transport-secret-149"
RUNTIME_SECRET = f"Bearer {SECRET}"


class RecordingCredentialStore:
    def __init__(self, result: SecretStr | Exception | None = None):
        self.result = SecretStr(SECRET) if result is None else result
        self.reads: list[str] = []

    def read(self, credential_id: str) -> SecretStr:
        self.reads.append(credential_id)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class AsyncContext:
    def __init__(
        self,
        yielded: tuple[Any, ...] = ("read", "write"),
        *,
        enter_error: Exception | None = None,
    ):
        self.yielded = yielded
        self.enter_error = enter_error
        self.entered = 0
        self.exited = 0

    async def __aenter__(self) -> tuple[Any, ...]:
        self.entered += 1
        if self.enter_error is not None:
            raise self.enter_error
        return self.yielded

    async def __aexit__(self, *_exc: object) -> None:
        self.exited += 1


class SessionContext:
    instances: list[SessionContext] = []

    def __init__(self, read_stream: Any, write_stream: Any):
        self.streams = (read_stream, write_stream)
        self.initialized = 0
        self.exited = 0
        type(self).instances.append(self)

    async def __aenter__(self) -> SessionContext:
        return self

    async def initialize(self) -> None:
        self.initialized += 1

    async def __aexit__(self, *_exc: object) -> None:
        self.exited += 1


class ControlledSessionContext(SessionContext):
    initialize_started = asyncio.Event()
    initialize_release = asyncio.Event()
    enter_error: BaseException | None = None
    initialize_error: BaseException | None = None

    async def __aenter__(self) -> ControlledSessionContext:
        if self.enter_error is not None:
            raise self.enter_error
        return self

    async def initialize(self) -> None:
        self.initialized += 1
        type(self).initialize_started.set()
        await type(self).initialize_release.wait()
        if self.initialize_error is not None:
            raise self.initialize_error


@pytest.fixture(autouse=True)
def _clear_session_instances() -> None:
    SessionContext.instances.clear()
    ControlledSessionContext.initialize_started = asyncio.Event()
    ControlledSessionContext.initialize_release = asyncio.Event()
    ControlledSessionContext.enter_error = None
    ControlledSessionContext.initialize_error = None


def authenticated_config(transport: str = "stdio") -> McpServerConfig:
    auth: dict[str, str] = {"credential_ref": "operator"}
    values: dict[str, Any] = {
        "id": "dispatcher",
        "type": transport,
        "auth": auth,
    }
    if transport == "stdio":
        values.update(
            command="dispatcher-cli",
            args=["serve"],
            env={"CONFIGURED": "yes"},
        )
        auth["stdio_environment"] = "HIVE_AUTH"
    else:
        values["url"] = "https://dispatcher.invalid/mcp"
        auth["http_scheme"] = "bearer"
    return McpServerConfig(**values)


def authenticated_effective_config(tmp_path: Any) -> Any:
    config_path = tmp_path / "gearcore.yaml"
    config_path.write_text(
        """\
version: 3
registry:
  mcp_servers:
    - id: dispatcher
      type: sse
      url: https://dispatcher.invalid/mcp
      auth:
        credential_ref: operator
        http_scheme: bearer
profiles:
  default: operator
  entries:
    operator:
      scope:
        mcp_servers:
          include: [dispatcher]
""",
        encoding="utf-8",
    )
    return load_config(project=tmp_path, global_config_path=config_path)


def assert_exception_graph_is_sanitized(
    exception: BaseException, sentinel: str = RUNTIME_SECRET
) -> None:
    pending: list[BaseException] = [exception]
    rendered: list[str] = []
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        rendered.extend((str(current), repr(current)))
        if current.__cause__ is not None:
            pending.append(current.__cause__)
        if current.__context__ is not None:
            pending.append(current.__context__)
        if isinstance(current, BaseExceptionGroup):
            pending.extend(current.exceptions)
        traceback = current.__traceback__
        while traceback is not None:
            rendered.append(repr(traceback.tb_frame.f_locals))
            traceback = traceback.tb_next
    assert sentinel not in "\n".join(rendered)


def control_flow_failure(kind: str) -> BaseException:
    if kind == "keyboard":
        return KeyboardInterrupt(RUNTIME_SECRET)
    if kind == "system_exit":
        return SystemExit(RUNTIME_SECRET)
    return BaseExceptionGroup(
        RUNTIME_SECRET,
        [
            KeyboardInterrupt(RUNTIME_SECRET),
            BaseExceptionGroup(
                RUNTIME_SECRET, [SystemExit(RUNTIME_SECRET)]
            ),
        ],
    )


async def start_with_recorders(
    monkeypatch: pytest.MonkeyPatch,
    config: McpServerConfig,
    store: RecordingCredentialStore,
    transport_factory: Callable[..., AsyncContext],
) -> tuple[SharedMCPServer, list[tuple[tuple[Any, ...], dict[str, Any]]]]:
    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def recording_factory(*args: Any, **kwargs: Any) -> AsyncContext:
        recorded_kwargs = {
            key: dict(value) if isinstance(value, dict) else value
            for key, value in kwargs.items()
        }
        calls.append((args, recorded_kwargs))
        return transport_factory(*args, **kwargs)

    target = {
        "stdio": "stdio_client",
        "sse": "sse_client",
        "http": "streamablehttp_client",
    }[config.type]
    monkeypatch.setattr(f"gearcore_hub.process_manager.{target}", recording_factory)
    monkeypatch.setattr("gearcore_hub.process_manager.ClientSession", SessionContext)
    server = SharedMCPServer(config, credential_store=store)
    await server.start()
    return server, calls


@pytest.mark.asyncio
async def test_stdio_materializes_one_secret_at_start_into_fresh_child_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = authenticated_config("stdio")
    configured_environment = config.env
    parent_before = dict(os.environ)
    captured_environment: dict[str, str] = {}

    def factory(params: Any) -> AsyncContext:
        captured_environment.update(params.env)
        return AsyncContext()

    store = RecordingCredentialStore()
    server = SharedMCPServer(config, credential_store=store)
    assert store.reads == []
    monkeypatch.setattr("gearcore_hub.process_manager.stdio_client", factory)
    monkeypatch.setattr("gearcore_hub.process_manager.ClientSession", SessionContext)

    await asyncio.gather(server.start(), server.start())

    assert store.reads == ["operator"]
    assert captured_environment == {
        "CONFIGURED": "yes",
        "HIVE_AUTH": SECRET,
    }
    assert config.env == configured_environment == {"CONFIGURED": "yes"}
    assert dict(os.environ) == parent_before
    await server.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["sse", "http"])
async def test_network_transports_pass_ephemeral_bearer_header(
    monkeypatch: pytest.MonkeyPatch, transport: str
) -> None:
    store = RecordingCredentialStore()
    server, calls = await start_with_recorders(
        monkeypatch,
        authenticated_config(transport),
        store,
        lambda *_args, **_kwargs: AsyncContext(
            ("read", "write", lambda: "optional-session-id")
            if transport == "http"
            else ("read", "write")
        ),
    )

    assert store.reads == ["operator"]
    assert calls[0][0] == ("https://dispatcher.invalid/mcp",)
    assert calls[0][1]["headers"] == {"Authorization": f"Bearer {SECRET}"}
    assert SessionContext.instances[0].streams == ("read", "write")
    await server.stop()


@pytest.mark.asyncio
async def test_http_uses_streamable_client_and_never_sse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    http_calls = 0

    def http_client(*_args: Any, **_kwargs: Any) -> AsyncContext:
        nonlocal http_calls
        http_calls += 1
        return AsyncContext(("read", "write", lambda: None))

    def forbidden_sse(*_args: Any, **_kwargs: Any) -> AsyncContext:
        raise AssertionError("HTTP transport was incorrectly routed through SSE")

    monkeypatch.setattr("gearcore_hub.process_manager.streamablehttp_client", http_client)
    monkeypatch.setattr("gearcore_hub.process_manager.sse_client", forbidden_sse)
    monkeypatch.setattr("gearcore_hub.process_manager.ClientSession", SessionContext)
    server = SharedMCPServer(
        authenticated_config("http"),
        credential_store=RecordingCredentialStore(),
    )

    await server.start()

    assert http_calls == 1
    assert SessionContext.instances[0].streams == ("read", "write")
    await server.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["stdio", "sse", "http"])
async def test_unauthenticated_transports_preserve_existing_construction(
    monkeypatch: pytest.MonkeyPatch, transport: str
) -> None:
    values: dict[str, Any] = {"id": "legacy", "type": transport}
    if transport == "stdio":
        values.update(command="legacy-cli", args=["serve"], env={"MODE": "safe"})
    else:
        values["url"] = "https://legacy.invalid/mcp"
    config = McpServerConfig(**values)
    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def factory(*args: Any, **kwargs: Any) -> AsyncContext:
        calls.append((args, kwargs))
        return AsyncContext(
            ("read", "write", lambda: None)
            if transport == "http"
            else ("read", "write")
        )

    target = {
        "stdio": "stdio_client",
        "sse": "sse_client",
        "http": "streamablehttp_client",
    }[transport]
    monkeypatch.setattr(f"gearcore_hub.process_manager.{target}", factory)
    monkeypatch.setattr("gearcore_hub.process_manager.ClientSession", SessionContext)
    store = RecordingCredentialStore()
    server = SharedMCPServer(config, credential_store=store)

    await server.start()

    assert store.reads == []
    assert calls[0][1] == {}
    if transport == "stdio":
        assert calls[0][0][0].env == {"MODE": "safe"}
    else:
        assert calls[0][0] == ("https://legacy.invalid/mcp",)
    await server.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        CredentialError("credential unavailable"),
        CredentialError("unsafe credential file"),
        CredentialError("empty credential"),
    ],
)
async def test_credential_failure_prevents_transport_connection(
    monkeypatch: pytest.MonkeyPatch, failure: Exception
) -> None:
    connection_attempts = 0

    def forbidden_transport(*_args: Any, **_kwargs: Any) -> AsyncContext:
        nonlocal connection_attempts
        connection_attempts += 1
        return AsyncContext()

    monkeypatch.setattr("gearcore_hub.process_manager.sse_client", forbidden_transport)
    server = SharedMCPServer(
        authenticated_config("sse"),
        credential_store=RecordingCredentialStore(failure),
    )

    with pytest.raises(RuntimeError, match="MCP backend authentication failed") as exc:
        await server.start()

    assert connection_attempts == 0
    assert SECRET not in str(exc.value)
    assert exc.value.__cause__ is None
    assert exc.value.__context__ is None


@pytest.mark.asyncio
async def test_transport_failure_cannot_echo_secret_and_partial_context_closes(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    context = AsyncContext(
        enter_error=RuntimeError(f"monkeypatch received bearer {SECRET}")
    )
    monkeypatch.setattr(
        "gearcore_hub.process_manager.sse_client", lambda *_a, **_k: context
    )
    server = SharedMCPServer(
        authenticated_config("sse"),
        credential_store=RecordingCredentialStore(),
    )

    with (
        caplog.at_level(logging.DEBUG),
        pytest.raises(RuntimeError, match="MCP backend startup failed") as exc,
    ):
        await server.start()

    assert SECRET not in str(exc.value)
    assert exc.value.__cause__ is None
    assert exc.value.__context__ is None
    assert SECRET not in caplog.text
    traceback_surfaces: list[str] = []
    traceback = exc.value.__traceback__
    while traceback is not None:
        traceback_surfaces.append(repr(traceback.tb_frame.f_locals))
        traceback = traceback.tb_next
    assert SECRET not in "\n".join(traceback_surfaces)
    assert server.session is None
    assert server._client_ctx is None
    assert server._session_ctx is None
    assert SECRET not in repr(vars(server))


@pytest.mark.asyncio
async def test_secret_not_retained_by_manager_server_or_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_headers: dict[str, str] = {}

    def factory(_url: str, *, headers: dict[str, str]) -> AsyncContext:
        captured_headers.update(headers)
        return AsyncContext()

    monkeypatch.setattr("gearcore_hub.process_manager.sse_client", factory)
    monkeypatch.setattr("gearcore_hub.process_manager.ClientSession", SessionContext)
    config = authenticated_config("sse")
    manager = ProcessManager(credential_store=RecordingCredentialStore())

    await manager.register_and_start(config)
    server = manager.servers["dispatcher"]

    assert captured_headers == {"Authorization": f"Bearer {SECRET}"}
    retained_surfaces = [
        repr(config),
        config.model_dump_json(),
        repr(server),
        repr(manager.servers),
        repr(server.config),
    ]
    assert all(SECRET not in surface for surface in retained_surfaces)
    assert not hasattr(server, "credential")
    assert not hasattr(server, "headers")
    await server.stop()


@pytest.mark.asyncio
async def test_stop_then_restart_reresolves_secret_and_closes_each_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contexts: list[AsyncContext] = []

    def factory(*_args: Any, **_kwargs: Any) -> AsyncContext:
        context = AsyncContext()
        contexts.append(context)
        return context

    store = RecordingCredentialStore()
    monkeypatch.setattr("gearcore_hub.process_manager.sse_client", factory)
    monkeypatch.setattr("gearcore_hub.process_manager.ClientSession", SessionContext)
    server = SharedMCPServer(
        authenticated_config("sse"), credential_store=store
    )

    await server.start()
    await server.stop()
    await server.start()
    await server.stop()

    assert store.reads == ["operator", "operator"]
    assert [context.exited for context in contexts] == [1, 1]
    assert [session.exited for session in SessionContext.instances] == [1, 1]


def test_process_manager_build_server_is_single_configuration_factory() -> None:
    config = authenticated_config("stdio")
    store = RecordingCredentialStore()
    manager = ProcessManager(credential_store=store)

    server = manager.build_server(config)

    assert server.config is not config
    assert server.config == config
    assert server.credential_store is store


def test_one_shot_call_uses_process_manager_server_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = authenticated_config("stdio")
    built: list[McpServerConfig] = []

    class CallSession:
        async def call_tool(self, _tool: str, _arguments: dict[str, Any]) -> Any:
            return SimpleNamespace(content=[])

    class CallServer:
        session = CallSession()

        async def start(self) -> None:
            return None

        async def stop(self) -> None:
            return None

    def build_server(
        _manager: ProcessManager, server_config: McpServerConfig
    ) -> CallServer:
        built.append(server_config)
        return CallServer()

    monkeypatch.setattr(ProcessManager, "build_server", build_server)
    effective = SimpleNamespace(diagnostic_only=False, mcp_servers=[config])

    cmd_call(effective, "dispatcher", "health", "{}")  # type: ignore[arg-type]

    assert built == [config]


def test_one_shot_authenticated_startup_failure_is_sanitized_and_nonzero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    context = AsyncContext(
        enter_error=RuntimeError(f"transport echoed {SECRET}")
    )
    monkeypatch.setattr(
        "gearcore_hub.process_manager.sse_client", lambda *_a, **_k: context
    )
    config = authenticated_config("sse")
    effective = SimpleNamespace(diagnostic_only=False, mcp_servers=[config])

    with pytest.raises(SystemExit) as exc:
        cmd_call(
            effective,  # type: ignore[arg-type]
            "dispatcher",
            "health",
            "{}",
            credential_store=RecordingCredentialStore(),
        )

    assert exc.value.code == 1
    output = capsys.readouterr().out
    assert "MCP backend startup failed" in output
    assert SECRET not in output


@pytest.mark.asyncio
async def test_cancellation_during_initialize_closes_provisional_state_and_restarts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contexts: list[AsyncContext] = []

    def factory(*_args: Any, **_kwargs: Any) -> AsyncContext:
        context = AsyncContext()
        contexts.append(context)
        return context

    store = RecordingCredentialStore()
    monkeypatch.setattr("gearcore_hub.process_manager.sse_client", factory)
    monkeypatch.setattr(
        "gearcore_hub.process_manager.ClientSession", ControlledSessionContext
    )
    server = SharedMCPServer(
        authenticated_config("sse"), credential_store=store
    )

    start_task = asyncio.create_task(server.start())
    await ControlledSessionContext.initialize_started.wait()
    start_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await start_task

    assert contexts[0].exited == 1
    assert SessionContext.instances[0].exited == 1
    assert server.session is None
    assert server._client_ctx is None
    assert server._session_ctx is None
    assert getattr(server, "_exit_stack", None) is None
    assert SECRET not in repr(vars(server))

    ControlledSessionContext.initialize_started = asyncio.Event()
    ControlledSessionContext.initialize_release.set()
    await server.start()
    assert len(contexts) == 2
    assert store.reads == ["operator", "operator"]
    assert server.session is SessionContext.instances[1]
    await server.stop()
    await server.stop()
    assert contexts[1].exited == 1
    assert SessionContext.instances[1].exited == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_point", ["client_enter", "session_enter", "initialize"])
async def test_partial_start_failure_closes_only_entered_contexts_once(
    monkeypatch: pytest.MonkeyPatch, failure_point: str
) -> None:
    client_context = AsyncContext(
        enter_error=(
            RuntimeError("client entry failed")
            if failure_point == "client_enter"
            else None
        )
    )
    ControlledSessionContext.initialize_release.set()
    if failure_point == "session_enter":
        ControlledSessionContext.enter_error = RuntimeError("session entry failed")
    if failure_point == "initialize":
        ControlledSessionContext.initialize_error = RuntimeError(
            "initialize failed"
        )
    monkeypatch.setattr(
        "gearcore_hub.process_manager.sse_client", lambda *_a, **_k: client_context
    )
    monkeypatch.setattr(
        "gearcore_hub.process_manager.ClientSession", ControlledSessionContext
    )
    server = SharedMCPServer(
        authenticated_config("sse"),
        credential_store=RecordingCredentialStore(),
    )

    with pytest.raises(RuntimeError, match="MCP backend startup failed"):
        await server.start()
    await server.stop()

    expected_client_exits = 0 if failure_point == "client_enter" else 1
    expected_session_exits = 1 if failure_point == "initialize" else 0
    assert client_context.exited == expected_client_exits
    if failure_point == "client_enter":
        assert SessionContext.instances == []
    else:
        assert SessionContext.instances[0].exited == expected_session_exits
    assert server.session is None
    assert getattr(server, "_exit_stack", None) is None


@pytest.mark.asyncio
async def test_concurrent_registration_builds_starts_and_publishes_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    start_entered = asyncio.Event()
    start_release = asyncio.Event()
    candidates: list[Any] = []

    class Candidate:
        def __init__(self) -> None:
            self.starts = 0
            self.stops = 0

        async def start(self) -> None:
            self.starts += 1
            start_entered.set()
            await start_release.wait()

        async def stop(self) -> None:
            self.stops += 1

    def build(_config: McpServerConfig) -> Any:
        candidate = Candidate()
        candidates.append(candidate)
        return candidate

    manager = ProcessManager(credential_store=RecordingCredentialStore())
    monkeypatch.setattr(manager, "build_server", build)
    config = authenticated_config("sse")

    registrations = [
        asyncio.create_task(manager.register_and_start(config)) for _ in range(4)
    ]
    await start_entered.wait()
    await asyncio.sleep(0)
    start_release.set()
    await asyncio.gather(*registrations)

    assert len(candidates) == 1
    assert candidates[0].starts == 1
    assert manager.servers == {"dispatcher": candidates[0]}
    await manager.shutdown_all()
    assert candidates[0].stops == 1


@pytest.mark.asyncio
async def test_failed_or_cancelled_registration_releases_reservation_for_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidates: list[Any] = []
    first_started = asyncio.Event()

    class Candidate:
        def __init__(self, block: bool) -> None:
            self.block = block
            self.stops = 0

        async def start(self) -> None:
            if self.block:
                first_started.set()
                await asyncio.Event().wait()

        async def stop(self) -> None:
            self.stops += 1

    def build(_config: McpServerConfig) -> Any:
        candidate = Candidate(block=not candidates)
        candidates.append(candidate)
        return candidate

    manager = ProcessManager(credential_store=RecordingCredentialStore())
    monkeypatch.setattr(manager, "build_server", build)
    config = authenticated_config("sse")
    first = asyncio.create_task(manager.register_and_start(config))
    await first_started.wait()
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first

    await manager.register_and_start(config)

    assert len(candidates) == 2
    assert candidates[0].stops == 1
    assert manager.servers == {"dispatcher": candidates[1]}
    await manager.shutdown_all()
    assert candidates[1].stops == 1


@pytest.mark.asyncio
async def test_failed_registration_releases_reservation_for_explicit_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidates: list[Any] = []

    class Candidate:
        def __init__(self, fail: bool) -> None:
            self.fail = fail
            self.stops = 0

        async def start(self) -> None:
            if self.fail:
                raise RuntimeError("candidate failed")

        async def stop(self) -> None:
            self.stops += 1

    def build(_config: McpServerConfig) -> Any:
        candidate = Candidate(fail=not candidates)
        candidates.append(candidate)
        return candidate

    manager = ProcessManager(credential_store=RecordingCredentialStore())
    monkeypatch.setattr(manager, "build_server", build)
    config = authenticated_config("sse")

    with pytest.raises(RuntimeError, match="candidate failed"):
        await manager.register_and_start(config)
    await manager.register_and_start(config)

    assert len(candidates) == 2
    assert candidates[0].stops == 1
    assert manager.servers == {"dispatcher": candidates[1]}
    await manager.shutdown_all()


@pytest.mark.asyncio
async def test_shutdown_racing_registration_cannot_orphan_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    start_entered = asyncio.Event()
    start_release = asyncio.Event()
    stop_entered = asyncio.Event()
    stop_release = asyncio.Event()

    class Candidate:
        def __init__(self) -> None:
            self.stops = 0

        async def start(self) -> None:
            start_entered.set()
            await start_release.wait()

        async def stop(self) -> None:
            self.stops += 1
            stop_entered.set()
            await stop_release.wait()

    candidate = Candidate()
    manager = ProcessManager(credential_store=RecordingCredentialStore())
    monkeypatch.setattr(manager, "build_server", lambda _config: candidate)
    registration = asyncio.create_task(
        manager.register_and_start(authenticated_config("sse"))
    )
    await start_entered.wait()
    shutdown = asyncio.create_task(manager.shutdown_all())
    await asyncio.sleep(0)
    start_release.set()
    await stop_entered.wait()
    assert shutdown.done() is False
    stop_release.set()

    with pytest.raises(RuntimeError, match="shutting down"):
        await registration
    await shutdown

    assert candidate.stops == 1
    assert manager.servers == {}


@pytest.mark.asyncio
async def test_authenticated_hub_runtime_errors_are_redacted_without_status_mutation(
    tmp_path: Any,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class FailingSession:
        async def list_tools(self) -> Any:
            raise RuntimeError(f"backend list failure {RUNTIME_SECRET}")

        async def call_tool(self, _name: str, _arguments: dict[str, Any]) -> Any:
            raise RuntimeError(f"backend call failure {RUNTIME_SECRET}")

    config = authenticated_effective_config(tmp_path)
    diagnostic_codes_before = tuple(config.diagnostic_codes)
    hub = GearCoreHub(config, credential_store=RecordingCredentialStore())
    hub.process_manager.servers["dispatcher"] = SimpleNamespace(
        session=FailingSession(), authenticated=True
    )

    with caplog.at_level(logging.ERROR):
        list_response = await hub.server.request_handlers[ListToolsRequest](None)
        hub.resolved_tool_map["danger"] = {
            "server_id": "dispatcher",
            "original_name": "danger",
        }
        hub.server._tool_cache["danger"] = Tool(
            name="danger", inputSchema={"type": "object"}
        )
        call_response = await hub.server.request_handlers[CallToolRequest](
            CallToolRequest(
                params=CallToolRequestParams(name="danger", arguments={})
            )
        )

    cmd_status(config)
    status = capsys.readouterr().out
    surfaces = [
        caplog.text,
        repr(list_response),
        repr(call_response),
        status,
        repr(config.diagnostic_codes),
    ]
    assert RUNTIME_SECRET not in "\n".join(surfaces)
    assert tuple(config.diagnostic_codes) == diagnostic_codes_before
    assert "authenticated backend request failed" in repr(call_response)
    assert "authenticated_backend_failure" not in status
    assert "Capability diagnostics:" not in status


def test_one_shot_authenticated_runtime_error_has_no_secret_exception_graph(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class FailingSession:
        async def call_tool(self, _name: str, _arguments: dict[str, Any]) -> Any:
            raise RuntimeError(f"backend runtime failure {RUNTIME_SECRET}")

    class RunningServer:
        session = FailingSession()

        async def start(self) -> None:
            return None

        async def stop(self) -> None:
            return None

    monkeypatch.setattr(
        ProcessManager,
        "build_server",
        lambda _manager, _config: RunningServer(),
    )
    config = authenticated_effective_config(tmp_path)
    diagnostic_codes_before = tuple(config.diagnostic_codes)

    with pytest.raises(SystemExit) as exc:
        cmd_call(config, "dispatcher", "danger", "{}")

    output = capsys.readouterr()
    exception_graph = [repr(exc.value), repr(exc.value.__context__)]
    traceback = exc.value.__traceback__
    while traceback is not None:
        exception_graph.append(repr(traceback.tb_frame.f_locals))
        traceback = traceback.tb_next
    surfaces = [output.out, output.err, *exception_graph]
    assert RUNTIME_SECRET not in "\n".join(surfaces)
    assert "authenticated backend request failed" in output.out
    assert tuple(config.diagnostic_codes) == diagnostic_codes_before
    assert exc.value.__context__ is None


@pytest.mark.asyncio
@pytest.mark.parametrize("grouped", [False, True])
async def test_stop_cancellation_retains_only_unfinished_cleanup_for_retry(
    monkeypatch: pytest.MonkeyPatch, grouped: bool
) -> None:
    class CancellingClientContext(AsyncContext):
        async def __aexit__(self, *_exc: object) -> None:
            self.exited += 1
            if self.exited == 1:
                if grouped:
                    raise BaseExceptionGroup(
                        RUNTIME_SECRET,
                        [
                            asyncio.CancelledError(RUNTIME_SECRET),
                            ExceptionGroup(
                                RUNTIME_SECRET,
                                [RuntimeError(RUNTIME_SECRET)],
                            ),
                        ],
                    )
                raise asyncio.CancelledError(RUNTIME_SECRET)

    client_context = CancellingClientContext()
    monkeypatch.setattr(
        "gearcore_hub.process_manager.sse_client",
        lambda *_args, **_kwargs: client_context,
    )
    monkeypatch.setattr("gearcore_hub.process_manager.ClientSession", SessionContext)
    server = SharedMCPServer(
        authenticated_config("sse"),
        credential_store=RecordingCredentialStore(),
    )
    await server.start()

    expected = BaseExceptionGroup if grouped else asyncio.CancelledError
    with pytest.raises(expected) as exc:
        await server.stop()

    assert_exception_graph_is_sanitized(exc.value)
    assert server.session is None
    assert server._exit_stack is not None
    assert SessionContext.instances[0].exited == 1
    assert client_context.exited == 1
    with pytest.raises(RuntimeError, match="cleanup is incomplete"):
        await server.start()

    await server.stop()
    await server.stop()

    assert server._exit_stack is None
    assert SessionContext.instances[0].exited == 1
    assert client_context.exited == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("grouped", [False, True])
async def test_authenticated_start_sanitizes_cancellation_and_nested_group(
    monkeypatch: pytest.MonkeyPatch, grouped: bool
) -> None:
    ControlledSessionContext.initialize_release.set()
    if grouped:
        ControlledSessionContext.initialize_error = BaseExceptionGroup(
            RUNTIME_SECRET,
            [
                asyncio.CancelledError(RUNTIME_SECRET),
                ExceptionGroup(
                    RUNTIME_SECRET, [RuntimeError(RUNTIME_SECRET)]
                ),
                KeyboardInterrupt(RUNTIME_SECRET),
                SystemExit(RUNTIME_SECRET),
            ],
        )
    else:
        ControlledSessionContext.initialize_error = asyncio.CancelledError(
            RUNTIME_SECRET
        )
    monkeypatch.setattr(
        "gearcore_hub.process_manager.sse_client",
        lambda *_args, **_kwargs: AsyncContext(),
    )
    monkeypatch.setattr(
        "gearcore_hub.process_manager.ClientSession", ControlledSessionContext
    )
    server = SharedMCPServer(
        authenticated_config("sse"),
        credential_store=RecordingCredentialStore(),
    )
    expected = BaseExceptionGroup if grouped else asyncio.CancelledError

    with pytest.raises(expected) as exc:
        await server.start()

    assert_exception_graph_is_sanitized(exc.value)
    if grouped:
        assert isinstance(exc.value.exceptions[0], asyncio.CancelledError)
        assert isinstance(exc.value.exceptions[1], ExceptionGroup)
        assert isinstance(exc.value.exceptions[2], KeyboardInterrupt)
        assert isinstance(exc.value.exceptions[3], SystemExit)
    assert server.session is None
    assert server._exit_stack is None


@pytest.mark.asyncio
async def test_external_start_cancellation_preserves_task_state_without_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "gearcore_hub.process_manager.sse_client",
        lambda *_args, **_kwargs: AsyncContext(),
    )
    monkeypatch.setattr(
        "gearcore_hub.process_manager.ClientSession", ControlledSessionContext
    )
    server = SharedMCPServer(
        authenticated_config("sse"),
        credential_store=RecordingCredentialStore(),
    )
    task = asyncio.create_task(server.start())
    await ControlledSessionContext.initialize_started.wait()

    task.cancel(RUNTIME_SECRET)
    with pytest.raises(asyncio.CancelledError) as exc:
        await task

    assert task.cancelled()
    assert task.cancelling() == 1
    assert_exception_graph_is_sanitized(exc.value)


@pytest.mark.asyncio
async def test_authenticated_hub_sanitizes_list_cancel_and_call_group(
    tmp_path: Any,
) -> None:
    class CancellingSession:
        async def list_tools(self) -> Any:
            raise asyncio.CancelledError(RUNTIME_SECRET)

        async def call_tool(self, _name: str, _arguments: dict[str, Any]) -> Any:
            raise BaseExceptionGroup(
                RUNTIME_SECRET,
                [
                    asyncio.CancelledError(RUNTIME_SECRET),
                    ExceptionGroup(
                        RUNTIME_SECRET, [RuntimeError(RUNTIME_SECRET)]
                    ),
                ],
            )

    config = authenticated_effective_config(tmp_path)
    hub = GearCoreHub(config, credential_store=RecordingCredentialStore())
    hub.process_manager.servers["dispatcher"] = SimpleNamespace(
        session=CancellingSession(), authenticated=True
    )

    with pytest.raises(asyncio.CancelledError) as list_exc:
        await hub.server.request_handlers[ListToolsRequest](None)
    assert_exception_graph_is_sanitized(list_exc.value)

    hub.resolved_tool_map["danger"] = {
        "server_id": "dispatcher",
        "original_name": "danger",
    }
    hub.server._tool_cache["danger"] = Tool(
        name="danger", inputSchema={"type": "object"}
    )
    with pytest.raises(BaseExceptionGroup) as call_exc:
        await hub.server.request_handlers[CallToolRequest](
            CallToolRequest(
                params=CallToolRequestParams(name="danger", arguments={})
            )
        )
    assert_exception_graph_is_sanitized(call_exc.value)
    assert isinstance(call_exc.value.exceptions[0], asyncio.CancelledError)
    assert isinstance(call_exc.value.exceptions[1], ExceptionGroup)


@pytest.mark.parametrize("grouped", [False, True])
def test_one_shot_preserves_and_sanitizes_authenticated_cancellation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
    capsys: pytest.CaptureFixture[str],
    grouped: bool,
) -> None:
    class CancellingSession:
        async def call_tool(self, _name: str, _arguments: dict[str, Any]) -> Any:
            if grouped:
                raise BaseExceptionGroup(
                    RUNTIME_SECRET,
                    [
                        asyncio.CancelledError(RUNTIME_SECRET),
                        ExceptionGroup(
                            RUNTIME_SECRET, [RuntimeError(RUNTIME_SECRET)]
                        ),
                    ],
                )
            raise asyncio.CancelledError(RUNTIME_SECRET)

    class RunningServer:
        session = CancellingSession()

        async def start(self) -> None:
            return None

        async def stop(self) -> None:
            return None

    monkeypatch.setattr(
        ProcessManager,
        "build_server",
        lambda _manager, _config: RunningServer(),
    )
    config = authenticated_effective_config(tmp_path)
    expected = BaseExceptionGroup if grouped else asyncio.CancelledError

    with pytest.raises(expected) as exc:
        cmd_call(config, "dispatcher", "danger", "{}")

    assert_exception_graph_is_sanitized(exc.value)
    output = capsys.readouterr()
    assert RUNTIME_SECRET not in output.out
    assert RUNTIME_SECRET not in output.err


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["keyboard", "system_exit", "group"])
async def test_authenticated_start_sanitizes_control_flow_base_exceptions(
    monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    ControlledSessionContext.initialize_release.set()
    ControlledSessionContext.initialize_error = control_flow_failure(kind)
    monkeypatch.setattr(
        "gearcore_hub.process_manager.sse_client",
        lambda *_args, **_kwargs: AsyncContext(),
    )
    monkeypatch.setattr(
        "gearcore_hub.process_manager.ClientSession", ControlledSessionContext
    )
    server = SharedMCPServer(
        authenticated_config("sse"), credential_store=RecordingCredentialStore()
    )
    expected = {
        "keyboard": KeyboardInterrupt,
        "system_exit": SystemExit,
        "group": BaseExceptionGroup,
    }[kind]

    with pytest.raises(expected) as exc:
        await server.start()

    assert_exception_graph_is_sanitized(exc.value)
    assert exc.value.args == (() if kind != "group" else ("", exc.value.exceptions))
    if kind == "group":
        assert isinstance(exc.value.exceptions[0], KeyboardInterrupt)
        assert isinstance(exc.value.exceptions[1], BaseExceptionGroup)
        assert isinstance(exc.value.exceptions[1].exceptions[0], SystemExit)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["keyboard", "system_exit", "group"])
async def test_authenticated_stop_sanitizes_control_flow_and_retains_retry(
    monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    class ControlFlowClientContext(AsyncContext):
        async def __aexit__(self, *_exc: object) -> None:
            self.exited += 1
            if self.exited == 1:
                raise control_flow_failure(kind)

    client_context = ControlFlowClientContext()
    monkeypatch.setattr(
        "gearcore_hub.process_manager.sse_client",
        lambda *_args, **_kwargs: client_context,
    )
    monkeypatch.setattr("gearcore_hub.process_manager.ClientSession", SessionContext)
    server = SharedMCPServer(
        authenticated_config("sse"), credential_store=RecordingCredentialStore()
    )
    await server.start()
    expected = {
        "keyboard": KeyboardInterrupt,
        "system_exit": SystemExit,
        "group": BaseExceptionGroup,
    }[kind]

    with pytest.raises(expected) as exc:
        await server.stop()

    assert_exception_graph_is_sanitized(exc.value)
    assert server._exit_stack is not None
    await server.stop()
    assert server._exit_stack is None
    assert client_context.exited == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["keyboard", "system_exit", "group"])
async def test_authenticated_hub_sanitizes_control_flow_list_and_call(
    tmp_path: Any,
    kind: str,
) -> None:
    class ControlFlowSession:
        async def list_tools(self) -> Any:
            raise control_flow_failure(kind)

        async def call_tool(self, _name: str, _arguments: dict[str, Any]) -> Any:
            raise control_flow_failure(kind)

    config = authenticated_effective_config(tmp_path)
    hub = GearCoreHub(config, credential_store=RecordingCredentialStore())
    hub.process_manager.servers["dispatcher"] = SimpleNamespace(
        session=ControlFlowSession(), authenticated=True
    )

    expected = {
        "keyboard": KeyboardInterrupt,
        "system_exit": SystemExit,
        "group": BaseExceptionGroup,
    }[kind]
    with pytest.raises(expected) as list_exc:
        await hub.server.request_handlers[ListToolsRequest](None)
    assert_exception_graph_is_sanitized(list_exc.value)

    hub.resolved_tool_map["danger"] = {
        "server_id": "dispatcher",
        "original_name": "danger",
    }
    hub.server._tool_cache["danger"] = Tool(
        name="danger", inputSchema={"type": "object"}
    )
    with pytest.raises(expected) as call_exc:
        await hub.server.request_handlers[CallToolRequest](
            CallToolRequest(
                params=CallToolRequestParams(name="danger", arguments={})
            )
        )
    assert_exception_graph_is_sanitized(call_exc.value)


@pytest.mark.parametrize("kind", ["keyboard", "system_exit", "group"])
def test_one_shot_sanitizes_authenticated_control_flow(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
    capsys: pytest.CaptureFixture[str],
    kind: str,
) -> None:
    class ControlFlowSession:
        async def call_tool(self, _name: str, _arguments: dict[str, Any]) -> Any:
            raise control_flow_failure(kind)

    class RunningServer:
        session = ControlFlowSession()

        async def start(self) -> None:
            return None

        async def stop(self) -> None:
            return None

    monkeypatch.setattr(
        ProcessManager,
        "build_server",
        lambda _manager, _config: RunningServer(),
    )
    config = authenticated_effective_config(tmp_path)
    expected = {
        "keyboard": KeyboardInterrupt,
        "system_exit": SystemExit,
        "group": BaseExceptionGroup,
    }[kind]

    with pytest.raises(expected) as exc:
        cmd_call(config, "dispatcher", "danger", "{}")

    assert_exception_graph_is_sanitized(exc.value)
    output = capsys.readouterr()
    assert RUNTIME_SECRET not in output.out
    assert RUNTIME_SECRET not in output.err


@pytest.mark.asyncio
async def test_manager_retains_failed_start_candidate_until_cleanup_completes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidates: list[Any] = []

    class Candidate:
        authenticated = True

        def __init__(self) -> None:
            self.stops = 0

        async def start(self) -> None:
            raise RuntimeError("start failed")

        async def stop(self) -> None:
            self.stops += 1
            if self.stops == 1:
                raise asyncio.CancelledError(RUNTIME_SECRET)

    def build(_config: McpServerConfig) -> Any:
        candidate = Candidate()
        candidates.append(candidate)
        return candidate

    manager = ProcessManager(credential_store=RecordingCredentialStore())
    monkeypatch.setattr(manager, "build_server", build)
    config = authenticated_config("sse")

    with pytest.raises(asyncio.CancelledError) as exc:
        await manager.register_and_start(config)
    assert_exception_graph_is_sanitized(exc.value)
    assert manager._pending_cleanup == {"dispatcher": candidates[0]}

    with pytest.raises(RuntimeError, match="cleanup is incomplete"):
        await manager.register_and_start(config)
    assert len(candidates) == 1

    await manager.shutdown_all()
    assert candidates[0].stops == 2
    assert manager._pending_cleanup == {}


@pytest.mark.asyncio
async def test_shutdown_retains_cancelled_cleanup_and_allows_unrelated_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Candidate:
        authenticated = True

        def __init__(self, cancel_once: bool) -> None:
            self.cancel_once = cancel_once
            self.stops = 0

        async def start(self) -> None:
            return None

        async def stop(self) -> None:
            self.stops += 1
            if self.cancel_once and self.stops == 1:
                raise KeyboardInterrupt(RUNTIME_SECRET)

    dispatcher = Candidate(cancel_once=True)
    unrelated = Candidate(cancel_once=False)
    later = Candidate(cancel_once=False)
    manager = ProcessManager(credential_store=RecordingCredentialStore())
    configs = {
        "dispatcher": authenticated_config("sse"),
        "other": McpServerConfig(id="other", type="sse", url="https://other.invalid"),
        "later": McpServerConfig(id="later", type="sse", url="https://later.invalid"),
    }
    candidates = {
        "dispatcher": dispatcher,
        "other": unrelated,
        "later": later,
    }
    monkeypatch.setattr(
        manager, "build_server", lambda config: candidates[config.id]
    )
    await manager.register_and_start(configs["dispatcher"])
    await manager.register_and_start(configs["other"])

    with pytest.raises(KeyboardInterrupt) as exc:
        await manager.shutdown_all()
    assert_exception_graph_is_sanitized(exc.value)
    assert manager._pending_cleanup == {"dispatcher": dispatcher}
    assert unrelated.stops == 1

    await manager.register_and_start(configs["later"])
    assert manager.servers == {"later": later}
    await manager.shutdown_all()
    await manager.shutdown_all()
    assert dispatcher.stops == 2
    assert unrelated.stops == 1
    assert later.stops == 1
    assert manager._pending_cleanup == {}


@pytest.mark.asyncio
async def test_cancelled_shutdown_keeps_gate_and_drains_inflight_registration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    start_entered = asyncio.Event()
    start_cancelled = asyncio.Event()
    allow_start_cancel = asyncio.Event()
    stop_entered = asyncio.Event()
    allow_stop = asyncio.Event()

    class Candidate:
        authenticated = True

        def __init__(self) -> None:
            self.stops = 0

        async def start(self) -> None:
            start_entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                start_cancelled.set()
                await allow_start_cancel.wait()
                raise

        async def stop(self) -> None:
            self.stops += 1
            stop_entered.set()
            await allow_stop.wait()

    class Owned:
        authenticated = False

        def __init__(self) -> None:
            self.stops = 0

        async def stop(self) -> None:
            self.stops += 1

    candidate = Candidate()
    owned = Owned()
    manager = ProcessManager(credential_store=RecordingCredentialStore())
    monkeypatch.setattr(manager, "build_server", lambda _config: candidate)
    manager.servers["owned"] = owned  # type: ignore[assignment]
    config = authenticated_config("sse")
    registration = asyncio.create_task(manager.register_and_start(config))
    await start_entered.wait()
    shutdown = asyncio.create_task(manager.shutdown_all())
    while not manager._shutting_down:
        await asyncio.sleep(0)

    shutdown.cancel(RUNTIME_SECRET)
    await asyncio.wait_for(start_cancelled.wait(), timeout=0.5)

    with pytest.raises(RuntimeError, match="shutting down"):
        await manager.register_and_start(config)
    with pytest.raises(RuntimeError, match="shutting down"):
        await manager.register_and_start(
            McpServerConfig(id="other", type="sse", url="https://other.invalid")
        )
    assert manager._shutting_down is True
    assert "dispatcher" not in manager.servers

    allow_start_cancel.set()
    await stop_entered.wait()
    shutdown.cancel(f"{RUNTIME_SECRET}-again")
    await asyncio.sleep(0)
    assert manager._pending_cleanup == {"dispatcher": candidate}
    assert manager._shutting_down is True
    allow_stop.set()

    with pytest.raises(asyncio.CancelledError) as shutdown_exc:
        await shutdown
    with pytest.raises(asyncio.CancelledError):
        await registration

    assert shutdown.cancelled()
    assert shutdown.cancelling() == 2
    assert_exception_graph_is_sanitized(shutdown_exc.value)
    assert manager._shutting_down is False
    assert manager.servers == {}
    assert manager._registration_tasks == {}
    assert manager._pending_cleanup == {}
    assert candidate.stops == 2
    assert owned.stops == 1


@pytest.mark.asyncio
async def test_normal_shutdown_without_reservations_is_idempotent() -> None:
    manager = ProcessManager(credential_store=RecordingCredentialStore())

    await manager.shutdown_all()
    await manager.shutdown_all()

    assert manager._shutting_down is False
    assert manager._reservations == {}
    assert manager._registration_tasks == {}
    assert manager._pending_cleanup == {}
    assert manager.servers == {}
