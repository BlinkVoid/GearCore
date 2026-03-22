import asyncio
import logging
import sys
from typing import Dict, Optional, List
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.sse import sse_client
from contextlib import AsyncExitStack

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
        args: Optional[List[str]] = None,
        url: str = "",
        env: Optional[Dict[str, str]] = None,
    ):
        self.server_id = server_id
        self.transport = transport
        self.session: Optional[ClientSession] = None
        self.exit_stack = AsyncExitStack()
        self._lock = asyncio.Lock()

        if transport == "stdio":
            self.params = StdioServerParameters(
                command=command, args=args or [], env=env
            )
            self.url = ""
        elif transport in ("sse", "http"):
            self.params = None
            self.url = url
        else:
            raise ValueError(
                f"Unknown transport '{transport}' for server '{server_id}'. "
                "Supported: stdio, sse"
            )

    async def start(self):
        """Start the process/connection and initialize the MCP session."""
        async with self._lock:
            if self.session:
                return

            if self.transport == "stdio":
                logger.info(
                    "Starting MCP server '%s' (stdio): %s %s",
                    self.server_id,
                    self.params.command,
                    " ".join(self.params.args),
                )
                read_stream, write_stream = await self.exit_stack.enter_async_context(
                    stdio_client(self.params)
                )
            else:
                logger.info(
                    "Connecting to MCP server '%s' (sse): %s",
                    self.server_id,
                    self.url,
                )
                read_stream, write_stream = await self.exit_stack.enter_async_context(
                    sse_client(self.url)
                )

            self.session = await self.exit_stack.enter_async_context(
                ClientSession(read_stream, write_stream)
            )
            await self.session.initialize()
            logger.info("MCP server '%s' initialized.", self.server_id)

    async def stop(self):
        """Stop the process/connection and cleanup."""
        async with self._lock:
            if self.exit_stack:
                await self.exit_stack.aclose()
            self.session = None
            logger.info("MCP server '%s' stopped.", self.server_id)


class ProcessManager:
    """Registry for all shared MCP backend servers."""
    def __init__(self):
        self.servers: Dict[str, SharedMCPServer] = {}

    async def register_and_start(self, server_config: dict):
        server_id = server_config["id"]
        if server_id in self.servers:
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
        await server.start()
        self.servers[server_id] = server

    async def get_session(self, server_id: str) -> Optional[ClientSession]:
        server = self.servers.get(server_id)
        return server.session if server else None

    async def shutdown_all(self):
        logger.info("Shutting down all shared MCP processes...")
        tasks = [server.stop() for server in self.servers.values()]
        await asyncio.gather(*tasks)
        self.servers.clear()
