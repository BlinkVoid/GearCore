import asyncio
import contextlib
import logging
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.sse import sse_client
from mcp.client.stdio import stdio_client

logger = logging.getLogger("gearcore.process_manager")


class SharedMCPServer:
    """Manages a single long-running MCP server process and its session.

    Supports two transport modes:
    - stdio: spawns a subprocess (command + args)
    - sse: connects to an HTTP/SSE endpoint (url)
    """

    def __init__(
        self,
        server_id: str,
        transport: str = "stdio",
        command: str = "",
        args: list[str] | None = None,
        url: str = "",
        env: dict[str, str] | None = None,
    ):
        self.server_id = server_id
        self.transport = transport
        self.session: ClientSession | None = None
        self._lock = asyncio.Lock()
        self.params: StdioServerParameters | None = None

        # Context managers for cleanup
        self._client_ctx = None
        self._session_ctx = None
        self._streams = None

        if transport == "stdio":
            self.params = StdioServerParameters(
                command=command, args=args or [], env=env
            )
            self.url = ""
        elif transport in ("sse", "http"):
            self.url = url
        else:
            raise ValueError(
                f"Unknown transport '{transport}' for server '{server_id}'. "
                "Supported: stdio, sse, http"
            )

    async def start(self):
        """Start the process/connection and initialize the MCP session."""
        async with self._lock:
            if self.session:
                return

            client_cm = None
            session_cm = None
            streams = None

            try:
                if self.transport == "stdio":
                    logger.info(
                        "Starting MCP server '%s' (stdio): %s %s",
                        self.server_id,
                        self.params.command,
                        " ".join(self.params.args),
                    )
                    client_cm = stdio_client(self.params)
                    streams = await client_cm.__aenter__()
                else:
                    logger.info(
                        "Connecting to MCP server '%s' (sse): %s",
                        self.server_id,
                        self.url,
                    )
                    client_cm = sse_client(self.url)
                    streams = await client_cm.__aenter__()

                read_stream, write_stream = streams
                session_cm = ClientSession(read_stream, write_stream)
                self.session = await session_cm.__aenter__()
                await self.session.initialize()

                # Success — store references for later cleanup
                self._client_ctx = client_cm
                self._session_ctx = session_cm
                self._streams = streams
                logger.info("MCP server '%s' initialized.", self.server_id)
            except BaseException:
                # Cleanup partial initialization on failure.
                # BaseException (not Exception) so CancelledError from a
                # backend-start timeout can't leak the client/session CMs
                # and orphan the spawned subprocess.
                if session_cm is not None:
                    with contextlib.suppress(BaseException):
                        await session_cm.__aexit__(*sys.exc_info())
                if client_cm is not None:
                    with contextlib.suppress(BaseException):
                        await client_cm.__aexit__(*sys.exc_info())
                self.session = None
                self._client_ctx = None
                self._session_ctx = None
                self._streams = None
                raise

    async def stop(self):
        """Stop the process/connection and cleanup."""
        async with self._lock:
            if self._session_ctx is not None:
                try:
                    await self._session_ctx.__aexit__(None, None, None)
                except Exception as exc:
                    logger.debug(
                        "Session cleanup error for '%s': %s", self.server_id, exc
                    )
                self._session_ctx = None

            if self._client_ctx is not None:
                try:
                    await self._client_ctx.__aexit__(None, None, None)
                except RuntimeError as exc:
                    # Suppress known anyio task-boundary noise during event-loop teardown
                    msg = str(exc)
                    if "different task" in msg or "cancel scope" in msg:
                        logger.debug(
                            "Suppressing anyio cleanup noise for '%s': %s",
                            self.server_id,
                            exc,
                        )
                    else:
                        logger.warning(
                            "Client cleanup error for '%s': %s", self.server_id, exc
                        )
                except Exception as exc:
                    logger.warning(
                        "Client cleanup error for '%s': %s", self.server_id, exc
                    )
                self._client_ctx = None

            self.session = None
            self._streams = None
            logger.info("MCP server '%s' stopped.", self.server_id)


class ProcessManager:
    """Registry for all shared MCP backend servers."""

    def __init__(self):
        self.servers: dict[str, SharedMCPServer] = {}

    async def register_and_start(self, server_config: dict):
        server_id = server_config["id"]
        if server_id in self.servers:
            logger.debug("MCP server '%s' already registered, skipping.", server_id)
            return

        transport = server_config.get("type", "stdio")

        server = SharedMCPServer(
            server_id=server_id,
            transport=transport,
            command=server_config.get("command", ""),
            args=server_config.get("args", []),
            url=server_config.get("url", ""),
            env=server_config.get("env"),
        )
        try:
            await server.start()
            self.servers[server_id] = server
        except Exception as exc:
            logger.error("Failed to start MCP server '%s': %s", server_id, exc)
            raise

    async def get_session(self, server_id: str) -> ClientSession | None:
        server = self.servers.get(server_id)
        return server.session if server else None

    async def shutdown_all(self):
        logger.info("Shutting down all shared MCP processes...")
        if not self.servers:
            return
        tasks = [server.stop() for server in self.servers.values()]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for server_id, result in zip(self.servers.keys(), results, strict=True):
            if isinstance(result, Exception):
                logger.error("Error stopping '%s': %s", server_id, result)
        self.servers.clear()
