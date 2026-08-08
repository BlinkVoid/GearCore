"""Lifecycle and authenticated transport construction for MCP backends."""

from __future__ import annotations

import asyncio
import logging
import sys
from typing import Any, cast

from mcp import ClientSession, StdioServerParameters
from mcp.client.sse import sse_client
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamablehttp_client

from gearcore_hub.config import EffectiveConfig, McpServerConfig
from gearcore_hub.credentials import CredentialStore

logger = logging.getLogger("gearcore.process_manager")

_SANITIZED_CONTROL_FLOW_MESSAGE = ""


def _contains_control_flow(exception: BaseException) -> bool:
    if isinstance(
        exception, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)
    ):
        return True
    if isinstance(exception, BaseExceptionGroup):
        return any(_contains_control_flow(child) for child in exception.exceptions)
    return False


def _sanitized_exception_leaf(exception: BaseException) -> BaseException:
    if isinstance(exception, asyncio.CancelledError):
        return asyncio.CancelledError()
    if isinstance(exception, KeyboardInterrupt):
        return KeyboardInterrupt()
    if isinstance(exception, SystemExit):
        return SystemExit()
    try:
        return type(exception)()
    except Exception:
        return RuntimeError()


def sanitize_authenticated_control_flow(
    exception: BaseException,
) -> BaseException | None:
    """Rebuild control-flow trees without messages, links, or tracebacks."""

    if not _contains_control_flow(exception):
        return None
    if isinstance(exception, BaseExceptionGroup):
        children = tuple(
            sanitize_authenticated_control_flow(child)
            or _sanitized_exception_tree(child)
            for child in exception.exceptions
        )
        if isinstance(exception, ExceptionGroup):
            return ExceptionGroup(
                _SANITIZED_CONTROL_FLOW_MESSAGE,
                cast(tuple[Exception, ...], children),
            )
        return BaseExceptionGroup(_SANITIZED_CONTROL_FLOW_MESSAGE, children)
    return _sanitized_exception_leaf(exception)


def _sanitized_exception_tree(exception: BaseException) -> BaseException:
    if isinstance(exception, BaseExceptionGroup):
        children = tuple(
            _sanitized_exception_tree(child) for child in exception.exceptions
        )
        if isinstance(exception, ExceptionGroup):
            return ExceptionGroup(
                _SANITIZED_CONTROL_FLOW_MESSAGE,
                cast(tuple[Exception, ...], children),
            )
        return BaseExceptionGroup(_SANITIZED_CONTROL_FLOW_MESSAGE, children)
    return _sanitized_exception_leaf(exception)


class _AsyncContextOwner:
    """Own entered contexts until each individual exit completes."""

    def __init__(self) -> None:
        self._contexts: list[Any] = []

    @property
    def pending(self) -> bool:
        return bool(self._contexts)

    async def enter_async_context(self, context: Any) -> Any:
        value = await context.__aenter__()
        self._contexts.append(context)
        return value

    async def close(
        self,
        exc_info: tuple[object, object, object] = (None, None, None),
    ) -> None:
        pending_error: BaseException | None = None
        while self._contexts:
            context = self._contexts[-1]
            try:
                await context.__aexit__(*exc_info)
            except BaseException as caught:
                if _contains_control_flow(caught):
                    raise
                self._contexts.pop()
                pending_error = caught
            else:
                self._contexts.pop()
        if pending_error is not None:
            raise pending_error


class MCPAuthenticationError(RuntimeError):
    """An MCP credential could not be safely materialized."""


class MCPBackendStartError(RuntimeError):
    """An authenticated MCP transport failed after materialization."""


class SharedMCPServer:
    """Manage one long-running MCP backend and its client session.

    The retained configuration contains only a credential reference. Credential
    material is resolved for each start and passed directly to the selected MCP
    client transport.
    """

    def __init__(
        self,
        config: McpServerConfig,
        *,
        credential_store: CredentialStore | None = None,
    ) -> None:
        # Keep a defensive, validated snapshot so callers cannot alter transport
        # policy after the server has been built.
        self.config = config.model_copy(deep=True)
        self.credential_store = (
            credential_store if credential_store is not None else CredentialStore()
        )
        self.session: ClientSession | None = None
        self._lock = asyncio.Lock()

        # Context managers retained solely for cleanup. Raw credentials are not
        # attached to this object.
        self._client_ctx: Any = None
        self._session_ctx: Any = None
        self._streams: tuple[Any, ...] | None = None
        self._exit_stack: _AsyncContextOwner | None = None

    @property
    def server_id(self) -> str:
        return self.config.id

    @property
    def transport(self) -> str:
        return self.config.type

    @property
    def authenticated(self) -> bool:
        return self.config.auth is not None

    async def start(self) -> None:
        """Start and initialize the MCP session, resolving auth at the boundary."""

        async with self._lock:
            if self.session:
                return
            if self._exit_stack is not None:
                raise MCPBackendStartError(
                    "MCP backend cleanup is incomplete"
                )

            provisional_stack = _AsyncContextOwner()
            client_cm: Any = None
            session_cm: Any = None
            provisional_session: Any = None
            streams: tuple[Any, ...] | None = None
            authenticated = self.config.auth is not None
            failure: RuntimeError | None = None
            stage = "authentication" if authenticated else "transport"
            raw_secret = ""
            secret: Any = None
            propagated: BaseException | None = None

            try:
                if self.config.auth is not None:
                    secret = self.credential_store.read(
                        self.config.auth.credential_id()
                    )
                    raw_secret = secret.get_secret_value()
                    if not raw_secret:
                        raise ValueError("empty materialized credential")

                stage = "transport"
                if self.transport == "stdio":
                    logger.info(
                        "Starting MCP server '%s' (stdio): %s %s",
                        self.server_id,
                        self.config.command,
                        " ".join(self.config.args),
                    )
                    configured_env = self.config.env
                    child_env = (
                        None if configured_env is None else dict(configured_env)
                    )
                    if self.config.auth is not None:
                        child_env = child_env or {}
                        child_env[self.config.auth.stdio_environment] = raw_secret
                    params = StdioServerParameters(
                        command=self.config.command,
                        args=self.config.args,
                        env=child_env,
                    )
                    try:
                        client_cm = stdio_client(params)
                        streams = await provisional_stack.enter_async_context(
                            client_cm
                        )
                    finally:
                        # The SDK has already created the child at this point. Do
                        # not retain credential material in its parameters or our
                        # temporary mapping while the session remains active.
                        if authenticated:
                            params.env = None
                            if child_env is not None:
                                child_env.clear()
                elif self.transport == "sse":
                    logger.info(
                        "Connecting to MCP server '%s' (sse): %s",
                        self.server_id,
                        self.config.url,
                    )
                    headers = (
                        {"Authorization": f"Bearer {raw_secret}"}
                        if self.config.auth is not None
                        else None
                    )
                    try:
                        client_cm = (
                            sse_client(self.config.url, headers=headers)
                            if headers is not None
                            else sse_client(self.config.url)
                        )
                        streams = await provisional_stack.enter_async_context(
                            client_cm
                        )
                    finally:
                        if headers is not None:
                            headers.clear()
                elif self.transport == "http":
                    logger.info(
                        "Connecting to MCP server '%s' (http): %s",
                        self.server_id,
                        self.config.url,
                    )
                    headers = (
                        {"Authorization": f"Bearer {raw_secret}"}
                        if self.config.auth is not None
                        else None
                    )
                    try:
                        client_cm = (
                            streamablehttp_client(
                                self.config.url, headers=headers
                            )
                            if headers is not None
                            else streamablehttp_client(self.config.url)
                        )
                        streams = await provisional_stack.enter_async_context(
                            client_cm
                        )
                    finally:
                        if headers is not None:
                            headers.clear()
                else:
                    raise ValueError(
                        f"Unknown transport '{self.transport}' for server "
                        f"'{self.server_id}'. Supported: stdio, sse, http"
                    )

                # Authentication material is no longer needed once the MCP
                # transport has established its own connection state.
                raw_secret = ""
                secret = None

                # Streamable HTTP additionally yields a session-id accessor.
                # ClientSession consumes only the first two streams for every
                # supported transport.
                if streams is None:  # pragma: no cover - transport contract guard
                    raise RuntimeError("MCP transport did not provide streams")
                read_stream, write_stream = streams[0], streams[1]
                session_cm = ClientSession(read_stream, write_stream)
                provisional_session = await provisional_stack.enter_async_context(
                    session_cm
                )
                await provisional_session.initialize()

                # Publish only a fully initialized session and its complete exit
                # stack. Cancellation before this point leaves no shared state.
                self.session = provisional_session
                self._client_ctx = client_cm
                self._session_ctx = session_cm
                self._streams = streams
                self._exit_stack = provisional_stack
                logger.info("MCP server '%s' initialized.", self.server_id)
            except BaseException as caught:
                # Clean up partial initialization while the original exception is
                # active, then raise a new stable error outside this except suite
                # so traceback chaining cannot retain a secret-bearing failure.
                exc_info = sys.exc_info()
                cleanup_error: BaseException | None = None
                try:
                    await provisional_stack.close(exc_info)
                except BaseException as close_error:
                    cleanup_error = close_error
                self.session = None
                self._client_ctx = None
                self._session_ctx = None
                self._streams = None
                self._exit_stack = (
                    provisional_stack if provisional_stack.pending else None
                )
                sanitized_cancellation = None
                if authenticated:
                    if cleanup_error is not None:
                        sanitized_cancellation = sanitize_authenticated_control_flow(
                            cleanup_error
                        )
                    if sanitized_cancellation is None:
                        sanitized_cancellation = sanitize_authenticated_control_flow(
                            caught
                        )
                if sanitized_cancellation is not None:
                    propagated = sanitized_cancellation
                elif authenticated and isinstance(caught, Exception):
                    if stage == "authentication":
                        failure = MCPAuthenticationError(
                            "MCP backend authentication failed"
                        )
                    else:
                        failure = MCPBackendStartError(
                            "MCP backend startup failed"
                        )
                else:
                    propagated = cleanup_error or caught

                # Discard every direct or indirect reference to credential
                # material before constructing the sanitized traceback.
                raw_secret = ""
                secret = None
                client_cm = None
                session_cm = None
                provisional_session = None
                streams = None
                exc_info = (None, None, None)
                cleanup_error = None

            if propagated is not None:
                raise propagated from None
            if failure is not None:
                raise failure

    async def stop(self) -> None:
        """Stop the connection and release all retained contexts."""

        async with self._lock:
            exit_stack = self._exit_stack
            self.session = None
            self._session_ctx = None
            self._streams = None
            propagated: BaseException | None = None
            try:
                if exit_stack is not None:
                    await exit_stack.close()
            except BaseException as caught:
                if self.authenticated:
                    propagated = sanitize_authenticated_control_flow(caught)
                if propagated is None and isinstance(caught, Exception):
                    logger.warning("Client cleanup error for '%s'", self.server_id)
                elif propagated is None:
                    propagated = caught
            finally:
                if exit_stack is None or not exit_stack.pending:
                    self._exit_stack = None
                    self._client_ctx = None
            logger.info("MCP server '%s' stopped.", self.server_id)
            if propagated is not None:
                raise propagated from None


class ProcessManager:
    """Registry and single construction path for shared MCP backends."""

    def __init__(
        self,
        config: EffectiveConfig,
        credential_store: CredentialStore | None = None,
    ) -> None:
        # Force the defensive effective-sequence validation now, before any
        # lifecycle state or backend can be constructed.
        _ = config.mcp_servers
        self.config = config
        self.credential_store = (
            credential_store if credential_store is not None else CredentialStore()
        )
        self.servers: dict[str, SharedMCPServer] = {}
        self._state_lock = asyncio.Lock()
        self._shutdown_lock = asyncio.Lock()
        self._reservations: dict[str, asyncio.Future[bool]] = {}
        self._registration_tasks: dict[str, asyncio.Task[Any]] = {}
        self._pending_cleanup: dict[str, SharedMCPServer] = {}
        self._shutting_down = False

    def _server_config(self, server_id: str) -> McpServerConfig:
        server = self.config.mcp_server(server_id)
        if server is not None:
            return server
        # This is a policy boundary, not a registry discovery surface. Do not
        # reveal a denied raw definition or any of its transport data.
        raise KeyError("capability_denied") from None

    def build_server(self, server_id: str) -> SharedMCPServer:
        """Build one backend selected from the bound effective configuration."""

        return SharedMCPServer(
            self._server_config(server_id),
            credential_store=self.credential_store,
        )

    async def register_and_start(self, server_id: str) -> None:
        server_config = self._server_config(server_id)
        reservation: asyncio.Future[bool]
        while True:
            async with self._state_lock:
                if self._shutting_down:
                    raise RuntimeError("MCP process manager is shutting down")
                if server_id in self._pending_cleanup:
                    raise MCPBackendStartError(
                        "MCP backend cleanup is incomplete"
                    )
                if server_id in self.servers:
                    logger.debug(
                        "MCP server '%s' already registered, skipping.",
                        server_id,
                    )
                    return
                existing = self._reservations.get(server_id)
                if existing is None:
                    reservation = asyncio.get_running_loop().create_future()
                    self._reservations[server_id] = reservation
                    registration_task = asyncio.current_task()
                    if registration_task is not None:
                        self._registration_tasks[server_id] = registration_task
                    owner = True
                else:
                    reservation = existing
                    owner = False
            if owner:
                break
            wait_result: bool | None = None
            wait_error: BaseException | None = None
            try:
                wait_result = await asyncio.shield(reservation)
            except BaseException as caught:
                if server_config.auth is not None:
                    wait_error = sanitize_authenticated_control_flow(caught)
                if wait_error is None:
                    wait_error = caught
            if wait_error is not None:
                raise wait_error from None
            if wait_result:
                return

        server: SharedMCPServer | None = None
        propagated: BaseException | None = None
        try:
            server = self.build_server(server_id)
            await server.start()
        except BaseException as caught:
            if server_config.auth is not None:
                propagated = sanitize_authenticated_control_flow(caught)
            if propagated is None:
                propagated = caught
        else:
            async with self._state_lock:
                if not self._shutting_down:
                    self.servers[server_id] = server
                    self._reservations.pop(server_id, None)
                    self._registration_tasks.pop(server_id, None)
                    if not reservation.done():
                        reservation.set_result(True)
                    return
            propagated = RuntimeError("MCP process manager is shutting down")

        if server is not None:
            try:
                await self._stop_candidate(server_id, server)
            except BaseException as cleanup_error:
                propagated = cleanup_error
        async with self._state_lock:
            if self._reservations.get(server_id) is reservation:
                self._reservations.pop(server_id, None)
            self._registration_tasks.pop(server_id, None)
            if not reservation.done():
                reservation.set_result(False)
        if propagated is not None:
            raise propagated from None

    async def _stop_candidate(
        self, server_id: str, server: SharedMCPServer
    ) -> None:
        """Retain an unpublished candidate until its cleanup completes."""

        async with self._state_lock:
            self._pending_cleanup[server_id] = server
        propagated: BaseException | None = None
        try:
            await server.stop()
        except BaseException as caught:
            if getattr(server, "authenticated", False):
                propagated = sanitize_authenticated_control_flow(caught)
                if propagated is None and isinstance(caught, Exception):
                    propagated = MCPBackendStartError(
                        "MCP backend cleanup failed"
                    )
            if propagated is None:
                propagated = caught
        else:
            async with self._state_lock:
                if self._pending_cleanup.get(server_id) is server:
                    self._pending_cleanup.pop(server_id, None)
        if propagated is not None:
            raise propagated from None

    async def get_session(self, server_id: str) -> ClientSession | None:
        server = self.servers.get(server_id)
        return server.session if server else None

    async def shutdown_all(self) -> None:
        async with self._shutdown_lock:
            logger.info("Shutting down all shared MCP processes...")
            propagated: BaseException | None = None
            async with self._state_lock:
                self._shutting_down = True
                pending = tuple(self._reservations.values())
                registration_tasks = tuple(self._registration_tasks.values())

            def capture_control_flow(caught: BaseException) -> None:
                nonlocal propagated
                sanitized = sanitize_authenticated_control_flow(caught)
                if propagated is None:
                    propagated = sanitized if sanitized is not None else caught

            try:
                while pending:
                    try:
                        await asyncio.gather(
                            *(
                                asyncio.shield(reservation)
                                for reservation in pending
                            ),
                            return_exceptions=True,
                        )
                    except BaseException as caught:
                        capture_control_flow(caught)
                        for registration_task in registration_tasks:
                            registration_task.cancel()
                        continue
                    break

                transferred = False
                while not transferred:
                    try:
                        await self._state_lock.acquire()
                    except BaseException as caught:
                        capture_control_flow(caught)
                        continue
                    try:
                        for server_id, server in self.servers.items():
                            self._pending_cleanup.setdefault(server_id, server)
                        self.servers.clear()
                        servers = tuple(self._pending_cleanup.items())
                        transferred = True
                    finally:
                        self._state_lock.release()

                # Keep control-flow BaseExceptions in this task. Scheduling a
                # KeyboardInterrupt/SystemExit in child tasks can terminate the
                # event-loop runner before ownership bookkeeping completes.
                for server_id, server in servers:
                    try:
                        await self._stop_candidate(server_id, server)
                    except BaseException as result:
                        if isinstance(result, Exception):
                            logger.error("Error stopping '%s'", server_id)
                        else:
                            capture_control_flow(result)
            finally:
                gate_cleared = False
                while not gate_cleared:
                    try:
                        await self._state_lock.acquire()
                    except BaseException as caught:
                        capture_control_flow(caught)
                        continue
                    try:
                        self._shutting_down = False
                        gate_cleared = True
                    finally:
                        self._state_lock.release()
            if propagated is not None:
                raise propagated from None
