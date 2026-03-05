import asyncio
import logging
import sys
from typing import Dict, Optional, List
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from contextlib import AsyncExitStack

logger = logging.getLogger("gearcore.process_manager")

class SharedMCPServer:
    """Manages a single long-running MCP server process and its session."""
    def __init__(self, server_id: str, command: str, args: List[str], env: Optional[Dict[str, str]] = None):
        self.server_id = server_id
        self.params = StdioServerParameters(command=command, args=args, env=env)
        self.exit_stack = AsyncExitStack()
        self.session: Optional[ClientSession] = None
        self._lock = asyncio.Lock()

    async def start(self):
        """Start the process and initialize the session."""
        async with self._lock:
            if self.session:
                return

            logger.info(f"Starting MCP server '{self.server_id}': {self.params.command} {' '.join(self.params.args)}")
            
            # Use the stdio_client context manager via exit_stack
            read_stream, write_stream = await self.exit_stack.enter_async_context(stdio_client(self.params))
            self.session = await self.exit_stack.enter_async_context(ClientSession(read_stream, write_stream))
            
            # Initialize the session
            await self.session.initialize()
            logger.info(f"MCP server '{self.server_id}' initialized.")

    async def stop(self):
        """Stop the process and cleanup."""
        async with self._lock:
            if self.exit_stack:
                await self.exit_stack.aclose()
            self.session = None
            logger.info(f"MCP server '{self.server_id}' stopped.")

class ProcessManager:
    """Registry for all shared MCP backend servers."""
    def __init__(self):
        self.servers: Dict[str, SharedMCPServer] = {}

    async def register_and_start(self, server_config: dict):
        server_id = server_config["id"]
        if server_id in self.servers:
            return

        server = SharedMCPServer(
            server_id=server_id,
            command=server_config["command"],
            args=server_config["args"],
            env=server_config.get("env")
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
