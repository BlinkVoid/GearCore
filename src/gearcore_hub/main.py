"""
GearCore CLI entry point.

Subcommands:
  list-skills    Enumerate available skills in the current context
  request-skill  Print a skill's instructions (SKILL.md)
  call           Invoke a tool on an MCP backend (stateless, one-shot)
  serve          Run the MCP hub (fallback for clients without skill support)
  status         Show active servers and loaded skills
  list           Alias for status
  add-mcp        Register a new MCP server
  add-skill      Register a skill bundle
  add-cli        Wrap a CLI program into a skill via CLI-Anything
  remove         Remove an MCP server or skill
  sync           Install/update the GearCore self-skill on AI CLI tools

Usage:
  gearcore list-skills [--project <path>]
  gearcore request-skill <name> [--project <path>]
  gearcore call <server_id> <tool> ['<json_args>']
  gearcore [--project <path>] [serve]
  gearcore add-mcp --id <id> --type stdio --command <cmd> [--args ...]
  gearcore add-skill <path> [--scope global|project] [--symlink]
  gearcore add-cli <program> [--scope global|project]
  gearcore remove mcp <id> | skill <name> [--scope global|project]
  gearcore sync [--tool claude|codex|kimi|opencode] [--dry-run] [--remove]
  gearcore status
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import sys
from pathlib import Path

from mcp.server import NotificationOptions, Server
from mcp.server.models import InitializationOptions
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from gearcore_hub.config import EffectiveConfig, load_config
from gearcore_hub.conflict_resolver import ConflictResolver
from gearcore_hub.process_manager import ProcessManager, SharedMCPServer
from gearcore_hub.render import render_skill_instructions
from gearcore_hub.skill_manager import SkillManager

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("gearcore")


# ---------------------------------------------------------------------------
# Serve command — the MCP hub runtime
# ---------------------------------------------------------------------------


class GearCoreHub:
    def __init__(self, config: EffectiveConfig):
        self.config = config
        self.process_manager = ProcessManager()
        self.skill_manager = SkillManager(config)
        self.conflict_resolver = ConflictResolver(config.resolution.model_dump())
        self.resolved_tool_map: dict = {}
        self.server = Server("gearcore-hub")
        self._setup_handlers()

    def _setup_handlers(self):
        @self.server.list_tools()
        async def handle_list_tools() -> list[Tool]:
            all_tools = [
                Tool(
                    name="list_skills",
                    description="List available GearCore skill bundles in the current context.",
                    inputSchema={"type": "object", "properties": {}},
                ),
                Tool(
                    name="request_skill",
                    description="Unlock a skill bundle to inject its instructions and expose its tools.",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "name": {
                                "type": "string",
                                "description": "Skill name to unlock.",
                            }
                        },
                        "required": ["name"],
                    },
                ),
            ]

            aggregated_raw = []
            for server_id, server in self.process_manager.servers.items():
                if server.session:
                    try:
                        resp = await asyncio.wait_for(
                            server.session.list_tools(), timeout=10.0
                        )
                        for tool in resp.tools:
                            if self.skill_manager.is_tool_active(server_id, tool.name):
                                aggregated_raw.append(
                                    {
                                        "server_id": server_id,
                                        "tool": tool,
                                        "original_name": tool.name,
                                    }
                                )
                    except TimeoutError:
                        logger.warning("list_tools timed out for %s", server_id)
                    except Exception as exc:
                        logger.error("list_tools failed for %s: %s", server_id, exc)

            resolved_tools, tool_map = self.conflict_resolver.resolve(aggregated_raw)
            self.resolved_tool_map.clear()
            self.resolved_tool_map.update(tool_map)
            all_tools.extend(resolved_tools)
            return all_tools

        @self.server.call_tool()
        async def handle_call_tool(name: str, arguments: dict | None):

            if name == "list_skills":
                skills = self.skill_manager.list_available_skills()
                ctx = self.config.context_name
                lines = [f"GearCore skills ({ctx} context):\n"]
                for s in skills:
                    tag = "[active]" if s["status"] == "active" else ""
                    scope_tag = "[project]" if s["scope"] == "project" else ""
                    lines.append(f"  {s['name']} {tag}{scope_tag} — {s['description']}")
                return [TextContent(type="text", text="\n".join(lines))]

            if name == "request_skill":
                skill_name = (arguments or {}).get("name", "")
                if not skill_name:
                    return [
                        TextContent(type="text", text="Error: missing 'name' argument.")
                    ]
                skill = self.skill_manager.get_skill(skill_name)
                if not skill:
                    return [
                        TextContent(
                            type="text",
                            text=f"Error: skill '{skill_name}' not found or not visible in this context.",
                        )
                    ]
                self.skill_manager.activate_skill(skill_name)
                text = f"### SKILL LOADED: {skill_name}\n\n{skill.instructions}\n\nTools for '{skill_name}' are now available."
                return [TextContent(type="text", text=text)]

            mapping = self.resolved_tool_map.get(name)
            if not mapping:
                return [
                    TextContent(
                        type="text",
                        text=f"Error: tool '{name}' not found. Use 'request_skill' to unlock tools.",
                    )
                ]

            session = await self.process_manager.get_session(mapping["server_id"])
            if not session:
                return [
                    TextContent(
                        type="text",
                        text=f"Error: backend '{mapping['server_id']}' is offline.",
                    )
                ]

            try:
                result = await asyncio.wait_for(
                    session.call_tool(mapping["original_name"], arguments or {}),
                    timeout=60.0,
                )
                return result.content
            except TimeoutError:
                logger.error("Tool call %s timed out after 60s", name)
                return [
                    TextContent(
                        type="text", text="Error: Tool call timed out after 60 seconds."
                    )
                ]
            except Exception as exc:
                logger.error("Tool call %s failed: %s", name, exc)
                return [TextContent(type="text", text=f"Error: {exc}")]

    async def _start_backends(self):
        for server_cfg in self.config.mcp_servers:
            try:
                await asyncio.wait_for(
                    self.process_manager.register_and_start(server_cfg.model_dump()),
                    timeout=15.0,
                )
            except TimeoutError:
                logger.error("Backend '%s' failed to start within 15s", server_cfg.id)
            except Exception as exc:
                logger.error("Failed to start backend '%s': %s", server_cfg.id, exc)

    async def run(self):
        await self._start_backends()
        logger.info("GearCore ready (context: %s)", self.config.context_name)

        try:
            async with stdio_server() as (read_stream, write_stream):
                await self.server.run(
                    read_stream,
                    write_stream,
                    InitializationOptions(
                        server_name="gearcore-hub",
                        server_version="2.1.0",
                        capabilities=self.server.get_capabilities(
                            notification_options=NotificationOptions(),
                            experimental_capabilities={},
                        ),
                    ),
                )
        except asyncio.CancelledError:
            pass
        finally:
            try:
                await asyncio.shield(self._shutdown())
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                logger.error("Shutdown error: %s", exc)

    async def _shutdown(self):
        logger.info("Shutting down GearCore...")
        await self.process_manager.shutdown_all()


# ---------------------------------------------------------------------------
# Status command
# ---------------------------------------------------------------------------


def cmd_status(config: EffectiveConfig):
    print(f"\nGearCore — context: {config.context_name}")
    if config.project_root:
        print(f"  Project root: {config.project_root}")

    print("\nMCP servers (effective):")
    for s in config.mcp_servers:
        addr = s.command if s.type == "stdio" else s.url
        print(f"  [{s.type}] {s.id} — {addr}")

    print("\nSkills dirs:")
    for d in config.skills_dirs:
        print(f"  {d}")

    print("\nDisclosure:")
    disc = config.disclosure
    print(f"  strategy: {disc.strategy}")
    print(f"  core_skills: {disc.core_skills or '(none)'}")
    print(f"  activation_threshold: {disc.activation_threshold}")
    from gearcore_hub.vendor import get_upstream_commit, load_vendor_manifest

    manifest = load_vendor_manifest()
    if manifest:
        print("\nVendored skills:")
        sha = manifest.vendored_commit
        short_sha = sha[:12] if len(sha) >= 12 else sha
        print(f"  superpowers @ {short_sha} ({manifest.vendored_at})")
        upstream = get_upstream_commit(manifest.source, manifest.source_ref)
        if upstream and upstream != manifest.vendored_commit:
            upstream_short = upstream[:12] if len(upstream) >= 12 else upstream
            print(
                f"  update available: {upstream_short} "
                "(run 'gearcore update-superpowers' to refresh)"
            )

    print()


# ---------------------------------------------------------------------------
# list-skills command
# ---------------------------------------------------------------------------


def cmd_list_skills(config: EffectiveConfig):
    sm = SkillManager(config)
    skills = sm.list_available_skills()
    ctx = config.context_name
    print(f"GearCore skills ({ctx} context):\n")
    if not skills:
        print("  (no skills visible in this context)")
        return
    broken = [s for s in skills if s["status"] == "broken"]
    healthy = [s for s in skills if s["status"] != "broken"]
    for s in healthy:
        tags = []
        if s["status"] == "active":
            tags.append("[active]")
        if s["scope"] == "project":
            tags.append("[project]")
        tag_str = " ".join(tags)
        if tag_str:
            tag_str = " " + tag_str
        print(f"  {s['name']}{tag_str} — {s['description']}")
    if broken:
        print(f"\n  BROKEN SYMLINKS ({len(broken)}):")
        print(
            "  Fix with: gearcore remove <name> && gearcore add-skill --symlink <new-path>"
        )
        for s in broken:
            print(
                f"    {s['name']} → {s['description'].removeprefix('BROKEN SYMLINK → ')}"
            )


# ---------------------------------------------------------------------------
# request-skill command
# ---------------------------------------------------------------------------


def cmd_request_skill(config: EffectiveConfig, skill_name: str):
    sm = SkillManager(config)
    skill = sm.get_skill(skill_name)
    if not skill:
        print(
            f"Error: skill '{skill_name}' not found or not visible in this context.",
            file=sys.stderr,
        )
        sys.exit(1)
    print(render_skill_instructions(skill))


# ---------------------------------------------------------------------------
# call command — stateless MCP tool invocation via CLI
# ---------------------------------------------------------------------------


def cmd_call(config: EffectiveConfig, server_id: str, tool: str, args_json: str):
    import json as _json

    # Find the server config (use effective config to respect project scope)
    server_cfg = None
    for s in config.mcp_servers:
        if s.id == server_id:
            server_cfg = s
            break

    if not server_cfg:
        print(f"error: server '{server_id}' not found in gearcore config")
        sys.exit(1)

    # config.mcp_servers already filters to enabled servers, but double-check
    if not server_cfg.enabled:
        print(f"error: server '{server_id}' is disabled in gearcore config")
        sys.exit(1)

    try:
        tool_args = _json.loads(args_json) if args_json else {}
    except _json.JSONDecodeError as exc:
        print(f"error: invalid JSON arguments: {exc}")
        sys.exit(1)

    async def _run():
        server = SharedMCPServer(
            server_id=server_cfg.id,
            transport=server_cfg.type,
            command=server_cfg.command,
            args=server_cfg.args,
            url=server_cfg.url,
            env=server_cfg.env,
        )
        try:
            await server.start()
            result = await server.session.call_tool(tool, tool_args)
            for content in result.content:
                if hasattr(content, "text"):
                    print(content.text)
                elif hasattr(content, "data"):
                    print(content.data)
        finally:
            await server.stop()

    try:
        asyncio.run(_run())
    except Exception as exc:
        print(f"error: {server_id}/{tool} — {exc}")
        sys.exit(1)


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gearcore",
        description="GearCore — unified skill and MCP hub",
    )
    parser.add_argument(
        "--project",
        "-p",
        metavar="PATH",
        help="Project root containing a .gearcore/ directory. "
        "Auto-detected from CWD if omitted.",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Enable verbose logging (DEBUG level).",
    )

    sub = parser.add_subparsers(dest="command")

    # serve (default)
    sub.add_parser("serve", help="Run the MCP hub (default when no subcommand given)")

    # status / list
    sub.add_parser("status", help="Show current config and context")
    sub.add_parser("list", help="Alias for status")

    # list-skills
    sub.add_parser(
        "list-skills", help="Enumerate available skills in the current context"
    )

    # request-skill
    p_req_skill = sub.add_parser(
        "request-skill", help="Print a skill's instructions (SKILL.md)"
    )
    p_req_skill.add_argument("name", help="Skill name to retrieve")

    # call
    p_call = sub.add_parser("call", help="Invoke a tool on an MCP backend (stateless)")
    p_call.add_argument("server_id", help="MCP server ID (e.g. hive-gateway)")
    p_call.add_argument("tool", help="Tool name to call (e.g. worker_register)")
    p_call.add_argument(
        "args_json", nargs="?", default="", help="JSON-encoded arguments (default: {})"
    )

    # add-mcp
    p_add_mcp = sub.add_parser("add-mcp", help="Register a new MCP server")
    p_add_mcp.add_argument("--id", required=True)
    p_add_mcp.add_argument("--type", default="stdio", choices=["stdio", "sse", "http"])
    p_add_mcp.add_argument("--command", default="")
    p_add_mcp.add_argument("--args", nargs="*", default=[])
    p_add_mcp.add_argument("--url", default="")
    p_add_mcp.add_argument("--env", nargs="*", metavar="KEY=VALUE", default=[])
    p_add_mcp.add_argument("--scope", default="global", choices=["global", "project"])
    p_add_mcp.add_argument("--disabled", action="store_true")

    # add-skill
    p_add_skill = sub.add_parser("add-skill", help="Register a skill bundle directory")
    p_add_skill.add_argument(
        "path", help="Path to the skill directory (must contain SKILL.md)"
    )
    p_add_skill.add_argument("--scope", default="global", choices=["global", "project"])
    p_add_skill.add_argument(
        "--symlink", action="store_true", help="Symlink instead of copy"
    )

    # add-cli
    p_add_cli = sub.add_parser(
        "add-cli", help="Wrap a CLI program into a skill via CLI-Anything"
    )
    p_add_cli.add_argument("program", help="Program name or path (e.g. ffmpeg)")
    p_add_cli.add_argument("--scope", default="global", choices=["global", "project"])

    # remove
    p_remove = sub.add_parser("remove", help="Remove an MCP server or skill")
    p_remove.add_argument("type", choices=["mcp", "skill"])
    p_remove.add_argument("name", help="ID (for mcp) or name (for skill)")
    p_remove.add_argument("--scope", default="global", choices=["global", "project"])

    # sync
    p_sync = sub.add_parser("sync", help="Install GearCore self-skill on AI CLI tools")
    p_sync.add_argument(
        "--tool",
        nargs="*",
        metavar="TOOL",
        help="Specific tools to target (claude, codex, kimi, opencode). Default: auto-detect.",
    )
    p_sync.add_argument("--dry-run", action="store_true")
    p_sync.add_argument("--remove", action="store_true", help="Unlink from all tools")

    # update-superpowers
    p_update_sp = sub.add_parser(
        "update-superpowers",
        help="Update the bundled superpowers skills from upstream",
    )
    p_update_sp.add_argument(
        "--dry-run",
        action="store_true",
        help="Show whether an update is available without writing files",
    )

    return parser


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main():
    parser = build_parser()
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
        logger.debug("Verbose logging enabled")

    project_path = Path(args.project).resolve() if args.project else None
    config = load_config(project=project_path)

    command = args.command or "serve"

    if command in ("status", "list"):
        cmd_status(config)
        return

    if command == "list-skills":
        cmd_list_skills(config)
        return

    if command == "request-skill":
        cmd_request_skill(config, args.name)
        return

    if command == "call":
        cmd_call(config, args.server_id, args.tool, args.args_json)
        return

    if command == "serve":
        hub = GearCoreHub(config)
        with contextlib.suppress(KeyboardInterrupt):
            asyncio.run(hub.run())
        return

    if command == "add-mcp":
        from gearcore_hub.registry import add_mcp

        env = dict(kv.split("=", 1) for kv in (args.env or []) if "=" in kv) or None
        try:
            path = add_mcp(
                id=args.id,
                type=args.type,
                command=args.command,
                args=args.args,
                url=args.url,
                env=env,
                scope=args.scope,
                project_root=project_path,
                enabled=not args.disabled,
            )
            print(f"Registered MCP server '{args.id}' in {path}")
        except (ValueError, KeyError) as exc:
            print(f"Error: {exc}", file=sys.stderr)
            sys.exit(1)
        return

    if command == "add-skill":
        from gearcore_hub.registry import add_skill

        try:
            dest = add_skill(
                source=Path(args.path),
                scope=args.scope,
                project_root=project_path,
                symlink=args.symlink,
            )
            print(f"Skill installed at {dest}")
        except (FileNotFoundError, FileExistsError, ValueError) as exc:
            print(f"Error: {exc}", file=sys.stderr)
            sys.exit(1)
        return

    if command == "add-cli":
        from gearcore_hub.registry import add_cli

        try:
            dest = add_cli(
                program=args.program,
                scope=args.scope,
                project_root=project_path,
            )
            print(f"CLI skill scaffolded at {dest}")
        except (RuntimeError, FileExistsError) as exc:
            print(f"Error: {exc}", file=sys.stderr)
            sys.exit(1)
        return

    if command == "remove":
        from gearcore_hub.registry import remove_mcp, remove_skill

        try:
            if args.type == "mcp":
                remove_mcp(args.name, scope=args.scope, project_root=project_path)
            else:
                remove_skill(args.name, scope=args.scope, project_root=project_path)
            print(f"Removed {args.type} '{args.name}'")
        except (KeyError, FileNotFoundError) as exc:
            print(f"Error: {exc}", file=sys.stderr)
            sys.exit(1)
        return

    if command == "sync":
        from gearcore_hub.sync import sync

        results = sync(
            tools=args.tool,
            dry_run=args.dry_run,
            remove=args.remove,
        )
        for target, result in results.items():
            print(f"  {target:12s} {result}")
        return

    if command == "update-superpowers":
        from gearcore_hub.vendor import update_superpowers

        try:
            result = update_superpowers(dry_run=args.dry_run)
        except RuntimeError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            sys.exit(1)

        if result.get("changed"):
            upstream = result["upstream"]
            upstream_short = upstream[:12] if len(upstream) >= 12 else upstream
            if result.get("dry_run"):
                print(
                    f"Update available: superpowers {upstream_short} "
                    "(run without --dry-run to apply)"
                )
            else:
                print(f"Updated superpowers to {upstream_short}")
        else:
            upstream = result["upstream"]
            upstream_short = upstream[:12] if len(upstream) >= 12 else upstream
            print(f"superpowers is up to date ({upstream_short})")
        return

    parser.print_help()


if __name__ == "__main__":
    main()
