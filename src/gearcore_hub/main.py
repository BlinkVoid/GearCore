import asyncio
import logging
import yaml
import json
import signal
import sys
from typing import List, Any, Optional, Dict
from pathlib import Path
from mcp.server import Server, NotificationOptions
from mcp.server.models import InitializationOptions
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent, ImageContent, EmbeddedResource
from gearcore_hub.process_manager import ProcessManager
from gearcore_hub.skill_manager import SkillManager
from gearcore_hub.conflict_resolver import ConflictResolver

# Formalized Logging to stderr (mandatory for stdio transport)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    stream=sys.stderr
)
logger = logging.getLogger("gearcore.hub")

class GearCoreHub:
    def __init__(self, config_path: str):
        self.config_path = config_path
        self.config = {}
        self.process_manager = ProcessManager()
        self.skill_manager: Optional[SkillManager] = None
        self.conflict_resolver: Optional[ConflictResolver] = None
        self.resolved_tool_map: Dict[str, Dict[str, str]] = {}
        
        self.server = Server("gearcore-hub")
        self.reload_config()
        self._setup_handlers()

    def reload_config(self):
        """Load or reload the gearcore.yaml configuration."""
        try:
            with open(self.config_path, "r") as f:
                self.config = yaml.safe_load(f)
            
            # Re-initialize managers
            skills_cfg = self.config.get("skills", {})
            skills_dir = skills_cfg.get("directory", "./skills")
            core_skills = skills_cfg.get("core_skills", [])
            self.skill_manager = SkillManager(skills_dir, core_skills=core_skills)
            self.conflict_resolver = ConflictResolver(self.config.get("resolution", {}))
            
            logger.info(f"Configuration reloaded from {self.config_path}")
        except Exception as e:
            logger.error(f"Failed to load config: {e}")
            if not self.config:
                sys.exit(1)

    async def _start_backends(self):
        """Pre-load and start all enabled MCP servers."""
        for server_cfg in self.config.get("mcp_servers", []):
            if server_cfg.get("enabled", True):
                try:
                    await self.process_manager.register_and_start(server_cfg)
                except Exception as e:
                    logger.error(f"Failed to start backend {server_cfg.get('id')}: {e}")

    def _setup_handlers(self):
        """Set up standard MCP handlers with Skill and Conflict integration."""
        
        @self.server.list_tools()
        async def handle_list_tools() -> List[Tool]:
            """Aggregate tools with Progressive Disclosure and Conflict Resolution."""
            # 1. Core Hub Tools
            all_tools = [
                Tool(
                    name="list_skills",
                    description="List available GearCore skill bundles.",
                    inputSchema={"type": "object", "properties": {}}
                ),
                Tool(
                    name="request_skill",
                    description="Unlock a skill bundle (injects instructions and tools).",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "name": {"type": "string", "description": "The name of the skill bundle."}
                        },
                        "required": ["name"]
                    }
                )
            ]
            
            # 2. Aggregation & Disclosure
            aggregated_raw = []
            for server_id, server in self.process_manager.servers.items():
                if server.session:
                    try:
                        resp = await server.session.list_tools()
                        for tool in resp.tools:
                            # Only disclose if the tool is active in a skill
                            if self.skill_manager and self.skill_manager.is_tool_active(server_id, tool.name):
                                aggregated_raw.append({
                                    "server_id": server_id, 
                                    "tool": tool, 
                                    "original_name": tool.name
                                })
                    except Exception as e:
                        logger.error(f"Failed to list tools for {server_id}: {e}")
            
            # 3. Resolve Conflicts & Update Routing Map
            if self.conflict_resolver:
                resolved = self.conflict_resolver.resolve(aggregated_raw)
                self.resolved_tool_map.clear()
                for entry in aggregated_raw:
                    resolved_name = entry["tool"].name
                    self.resolved_tool_map[resolved_name] = {
                        "server_id": entry["server_id"],
                        "original_name": entry["original_name"]
                    }
                all_tools.extend(resolved)
            
            return all_tools

        @self.server.call_tool()
        async def handle_call_tool(name: str, arguments: dict | None) -> List[TextContent | ImageContent | EmbeddedResource]:
            """Route tool calls with error handling."""
            
            if name == "list_skills":
                skills = self.skill_manager.list_available_skills()
                formatted = "Available GearCore Skills:\n" + "\n".join(
                    [f"- {s['name']}: {s['description']} ({s['status']})" for s in skills]
                )
                return [TextContent(type="text", text=formatted)]

            elif name == "request_skill":
                skill_name = (arguments or {}).get("name")
                if not skill_name:
                    return [TextContent(type="text", text="Error: Missing 'name' argument.")]
                
                skill = self.skill_manager.get_skill(skill_name)
                if not skill:
                    return [TextContent(type="text", text=f"Error: Skill '{skill_name}' not found.")]
                
                self.skill_manager.activate_skill(skill_name)
                logger.info(f"Skill activated: {skill_name}")
                
                instructions = f"### SKILL LOADED: {skill_name}\n\n{skill.instructions}\n\n"
                instructions += f"Tools for '{skill_name}' are now available."
                return [TextContent(type="text", text=instructions)]

            # Backend Tool Routing
            mapping = self.resolved_tool_map.get(name)
            if not mapping:
                return [TextContent(type="text", text=f"Error: Tool '{name}' not found. Use 'request_skill' to unlock tools.")]
            
            server_id = mapping["server_id"]
            original_name = mapping["original_name"]
            
            session = await self.process_manager.get_session(server_id)
            if not session:
                return [TextContent(type="text", text=f"Error: Backend '{server_id}' is offline.")]

            try:
                result = await session.call_tool(original_name, arguments or {})
                return result.content
            except Exception as e:
                logger.error(f"Error calling {name} on {server_id}: {e}")
                return [TextContent(type="text", text=f"Error during execution: {str(e)}")]

    async def run(self):
        """Run the hub with graceful shutdown support."""
        await self._start_backends()
        logger.info("GearCore Hub initialized and ready.")
        
        async with stdio_server() as (read_stream, write_stream):
            await self.server.run(
                read_stream,
                write_stream,
                InitializationOptions(
                    server_name="gearcore-hub",
                    server_version="1.0.0",
                    capabilities=self.server.get_capabilities(
                        notification_options=NotificationOptions(),
                        experimental_capabilities={},
                    ),
                ),
            )

async def shutdown(hub: GearCoreHub):
    """Gracefully shut down the hub and its backends."""
    logger.info("Shutting down GearCore Hub...")
    await hub.process_manager.shutdown_all()
    logger.info("Shutdown complete.")

async def main():
    config_file = "config/gearcore.yaml"
    hub = GearCoreHub(config_file)
    
    # Handle OS signals for graceful termination
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            # Note: Windows doesn't support add_signal_handler fully
            # but SIGINT works via KeyboardInterrupt in the main loop
            if sys.platform != "win32":
                loop.add_signal_handler(sig, lambda: asyncio.create_task(shutdown(hub)))
        except NotImplementedError:
            pass

    try:
        await hub.run()
    except (asyncio.CancelledError, KeyboardInterrupt):
        pass
    finally:
        await shutdown(hub)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
