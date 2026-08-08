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
  profile-set    Create or replace a global capability profile
  remove         Remove an MCP server or skill
  sync           Install/update the GearCore self-skill on AI CLI tools

Usage:
  gearcore [--project <path>] list-skills
  gearcore [--project <path>] request-skill <name>
  gearcore call <server_id> <tool> ['<json_args>']
  gearcore [--project <path>] [serve]
  gearcore add-mcp --id <id> --type stdio --command <cmd> [--args ...]
  gearcore add-skill <path> [--scope global|project] [--symlink]
  gearcore add-cli <program> [--scope global|project]
  gearcore profile-set <name> [capability options]
  gearcore remove mcp <id> | skill <name> [--scope global|project]
  gearcore sync [--tool claude|codex|kimi|opencode] [--dry-run] [--remove]
  gearcore status
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import re
import sys
from collections.abc import Iterable
from pathlib import Path

from mcp.server import NotificationOptions, Server
from mcp.server.models import InitializationOptions
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from gearcore_hub.config import EffectiveConfig, load_config
from gearcore_hub.conflict_resolver import ConflictResolver
from gearcore_hub.credentials import CredentialError, CredentialStore
from gearcore_hub.logging_utils import silence_logger
from gearcore_hub.process_manager import (
    MCPAuthenticationError,
    MCPBackendStartError,
    ProcessManager,
    sanitize_authenticated_control_flow,
)
from gearcore_hub.render import render_skill_instructions
from gearcore_hub.skill_manager import SkillManager

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("gearcore")

AUTHENTICATED_BACKEND_FAILURE_MESSAGE = "authenticated backend request failed"


_SAFE_STATUS_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


_silence_logger = silence_logger


def _status_token(value: object) -> str:
    text = str(value)
    if _SAFE_STATUS_TOKEN.fullmatch(text) is not None:
        return text
    return json.dumps(text, ensure_ascii=True, separators=(",", ":"))


# ---------------------------------------------------------------------------
# Serve command — the MCP hub runtime
# ---------------------------------------------------------------------------


class GearCoreHub:
    def __init__(
        self,
        config: EffectiveConfig,
        *,
        credential_store: CredentialStore | None = None,
    ):
        self.config = config
        self.process_manager = ProcessManager(
            config, credential_store=credential_store
        )
        self.skill_manager = SkillManager(config)
        self.conflict_resolver = ConflictResolver(config.resolution.model_dump())
        self.resolved_tool_map: dict = {}
        self.server = Server("gearcore-hub")
        self._setup_handlers()

    def _log_authenticated_backend_failure(self, server_id: str) -> None:
        logger.error(
            "Authenticated backend request failed for '%s'", server_id
        )

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
            if self.config.diagnostic_only:
                all_tools.append(
                    Tool(
                        name="capability_diagnostic",
                        description="Report why GearCore started without capabilities.",
                        inputSchema={"type": "object", "properties": {}},
                    )
                )

            aggregated_raw = []
            for server_id, server in self.process_manager.servers.items():
                if server.session:
                    propagated: BaseException | None = None
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
                    except BaseException as caught:
                        if server.authenticated:
                            propagated = sanitize_authenticated_control_flow(
                                caught
                            )
                        if propagated is None:
                            if isinstance(caught, Exception) and server.authenticated:
                                self._log_authenticated_backend_failure(server_id)
                            elif isinstance(caught, Exception):
                                logger.error(
                                    "list_tools failed for %s: %s",
                                    server_id,
                                    caught,
                                )
                            else:
                                propagated = caught
                    if propagated is not None:
                        raise propagated from None

            resolved_tools, tool_map = self.conflict_resolver.resolve(aggregated_raw)
            self.resolved_tool_map.clear()
            self.resolved_tool_map.update(tool_map)
            all_tools.extend(resolved_tools)
            return all_tools

        @self.server.call_tool()
        async def handle_call_tool(name: str, arguments: dict | None):

            if name == "list_skills":
                skills = self.skill_manager.list_available_skills()
                ctx = (
                    "diagnostic-only"
                    if self.config.diagnostic_only
                    else self.config.context_name
                )
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

            if name == "capability_diagnostic" and self.config.diagnostic_only:
                return [
                    TextContent(
                        type="text", text=", ".join(self.config.diagnostic_codes)
                    )
                ]

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
            except BaseException as caught:
                backend = self.process_manager.servers.get(mapping["server_id"])
                if backend is not None and backend.authenticated:
                    propagated = sanitize_authenticated_control_flow(caught)
                    if propagated is None:
                        if isinstance(caught, Exception):
                            self._log_authenticated_backend_failure(
                                mapping["server_id"]
                            )
                            return [
                                TextContent(
                                    type="text",
                                    text=f"Error: {AUTHENTICATED_BACKEND_FAILURE_MESSAGE}.",
                                )
                            ]
                        propagated = caught
                elif isinstance(caught, Exception):
                    logger.error("Tool call %s failed: %s", name, caught)
                    return [TextContent(type="text", text=f"Error: {caught}")]
                else:
                    propagated = caught
            if propagated is not None:
                raise propagated from None

    async def _start_backends(self):
        if self.config.diagnostic_only:
            return
        for server_cfg in self.config.mcp_servers:
            try:
                await asyncio.wait_for(
                    self.process_manager.register_and_start(server_cfg.id),
                    timeout=15.0,
                )
            except TimeoutError:
                logger.error("Backend '%s' failed to start within 15s", server_cfg.id)
            except Exception as exc:
                logger.error("Failed to start backend '%s': %s", server_cfg.id, exc)

    async def run(self):
        await self._start_backends()
        context_name = (
            "diagnostic-only"
            if self.config.diagnostic_only
            else self.config.context_name
        )
        logger.info("GearCore ready (context: %s)", context_name)

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


def cmd_status(
    config: EffectiveConfig,
    *,
    credential_store: CredentialStore | None = None,
):
    def ids(values: Iterable[str]) -> str:
        ordered = sorted({_status_token(value) for value in values})
        return ",".join(ordered) if ordered else "none"

    if config.diagnostic_only:
        # Only launch/configuration policy diagnostics belong in status. An
        # authenticated backend runtime failure is deliberately ephemeral.
        policy_diagnostics = config.diagnostic_codes
        print("GearCore status")
        print(f"profile: {_status_token(config.profile_name)}")
        print(f"source: {_status_token(config.profile_source)}")
        print(
            "enforced_profile: "
            f"{_status_token(config.enforced_profile_name) if config.enforced_profile_name else 'none'}"
        )
        print("constrained: true")
        print("active_mcp: none")
        print("denied_mcp: none")
        print("protected_mcp: none")
        print("active_skills: none")
        print("denied_skills: none")
        print("protected_skills: none")
        print(f"diagnostics: {ids(policy_diagnostics)}")
        return

    skill_registry_unavailable = False
    try:
        for skill_root in config.skills_dirs:
            if skill_root.is_symlink() and not skill_root.exists():
                raise OSError("broken skill registry root")
            if skill_root.exists() and not skill_root.is_dir():
                raise OSError("invalid skill registry root")
        with _silence_logger("gearcore.skill_manager"):
            skill_manager = SkillManager(config)
        global_skills = tuple(
            name
            for name, bundle in skill_manager.skills.items()
            if not bundle.is_project_local
        )
        project_skills = tuple(
            name
            for name, bundle in skill_manager.skills.items()
            if bundle.is_project_local
        )
        resolved_skills = config.resolve_skill_capabilities(
            global_skills, project_skills
        )
        loaded_skill_names = set(skill_manager.skills)
        protected_skill_names = set(resolved_skills.protected)
        trusted_protected: set[str] = set()
        if protected_skill_names:
            from gearcore_hub.registry import _trusted_global_skill_ids

            try:
                trusted_global, _conflicting_global = _trusted_global_skill_ids(
                    config.global_cfg
                )
                trusted_protected = protected_skill_names.intersection(
                    trusted_global
                )
            except ValueError:
                skill_registry_unavailable = True
        unavailable_protected = protected_skill_names.difference(
            trusted_protected
        )
        available_loaded_skill_names = loaded_skill_names.difference(
            unavailable_protected
        )
        active_skills = tuple(
            skill_id
            for skill_id in resolved_skills.active
            if skill_id in available_loaded_skill_names
        )
        if skill_manager.broken_skills:
            skill_registry_unavailable = True
    except OSError:
        resolved_skills = config.resolve_skill_capabilities((), ())
        active_skills = ()
        skill_registry_unavailable = True
        unavailable_protected = set()

    store = credential_store or CredentialStore()
    active_mcp_ids: list[str] = []
    credential_unavailable = False
    for server in config.mcp_servers:
        if server.auth is not None:
            try:
                store.check(server.auth.credential_ref)
            except CredentialError:
                credential_unavailable = True
                continue
        active_mcp_ids.append(server.id)

    policy_diagnostics = (
        *config.diagnostic_codes,
        *(("skill_registry_unavailable",) if skill_registry_unavailable else ()),
        *(("protected_skill_unavailable",) if unavailable_protected else ()),
        *(("credential_unavailable",) if credential_unavailable else ()),
    )
    is_constrained = (
        config.profile.constrained or config.enforced_profile_name is not None
    )
    print("GearCore status")
    print(f"profile: {_status_token(config.profile_name)}")
    print(f"source: {_status_token(config.profile_source)}")
    print(
        "enforced_profile: "
        f"{_status_token(config.enforced_profile_name) if config.enforced_profile_name else 'none'}"
    )
    print(f"constrained: {str(is_constrained).lower()}")
    print(f"active_mcp: {ids(active_mcp_ids)}")
    print(f"denied_mcp: {ids(config.denied_mcp_server_ids)}")
    print(f"protected_mcp: {ids(config.profile.scope.mcp_servers.protected)}")
    print(f"active_skills: {ids(active_skills)}")
    print(f"denied_skills: {ids(resolved_skills.denied)}")
    print(f"protected_skills: {ids(resolved_skills.protected)}")
    print(f"diagnostics: {ids(policy_diagnostics)}")
    # Retain the established human-readable labels while the lower-case fields
    # above provide a stable gate-oriented surface.
    print(f"Profile: {_status_token(config.profile_name)}")
    if config.enforced_profile_name is not None:
        print(f"Enforced profile: {_status_token(config.enforced_profile_name)}")
    print(f"Active server IDs: {ids(active_mcp_ids)}")
    print(f"Denied server IDs: {ids(config.denied_mcp_server_ids)}")
    from gearcore_hub.vendor import get_upstream_commit, load_vendor_manifest

    with _silence_logger("gearcore.vendor"):
        manifest = load_vendor_manifest()
    if manifest:
        print("\nVendored skills:")
        sha = manifest.vendored_commit
        short_sha = sha[:12] if len(sha) >= 12 else sha
        print(
            "  superpowers @ "
            f"{_status_token(short_sha)} ({_status_token(manifest.vendored_at)})"
        )
        with _silence_logger("gearcore.vendor"):
            upstream = get_upstream_commit(manifest.source, manifest.source_ref)
        if upstream and upstream != manifest.vendored_commit:
            upstream_short = upstream[:12] if len(upstream) >= 12 else upstream
            print(
                f"  update available: {_status_token(upstream_short)} "
                "(run 'gearcore update-superpowers' to refresh)"
            )

    print()


# ---------------------------------------------------------------------------
# list-skills command
# ---------------------------------------------------------------------------


def cmd_list_skills(config: EffectiveConfig):
    sm = SkillManager(config)
    skills = sm.list_available_skills()
    ctx = "diagnostic-only" if config.diagnostic_only else config.context_name
    print(f"GearCore skills ({ctx} context):\n")
    if not skills:
        print("  (no skills visible in this context)")
        return

    # Level-0 skills: reveal full instructions inline, before the listing.
    level0 = [
        name
        for name in config.disclosure.core_skills
        if name in sm.visible_skill_names
    ]
    for name in level0:
        bundle = sm.skills[name]
        print(f"=== LEVEL-0 SKILL: {name} ===")
        print("(revealed by default — read and follow these instructions now)\n")
        print(render_skill_instructions(bundle))
        print(f"=== END LEVEL-0 SKILL: {name} ===\n")

    broken = [s for s in skills if s["status"] == "broken"]
    healthy = [s for s in skills if s["status"] != "broken"]
    for s in healthy:
        tags = []
        if s["name"] in level0:
            tags.append("[level-0]")
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


def cmd_call(
    config: EffectiveConfig,
    server_id: str,
    tool: str,
    args_json: str,
    *,
    credential_store: CredentialStore | None = None,
):
    import json as _json

    if config.diagnostic_only:
        print(f"error: {', '.join(config.diagnostic_codes)}")
        sys.exit(1)

    # Resolve through the same unambiguous effective lookup as ProcessManager.
    server_cfg = config.mcp_server(server_id)

    if not server_cfg:
        print("error: capability_denied")
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
        process_manager = ProcessManager(
            config, credential_store=credential_store
        )
        server = process_manager.build_server(server_id)
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

    failure_message: str | None = None
    propagated: BaseException | None = None
    try:
        asyncio.run(_run())
    except (MCPAuthenticationError, MCPBackendStartError) as exc:
        failure_message = str(exc)
    except BaseException as caught:
        if server_cfg.auth is not None:
            propagated = sanitize_authenticated_control_flow(caught)
        if propagated is None:
            if isinstance(caught, Exception) and server_cfg.auth is not None:
                failure_message = AUTHENTICATED_BACKEND_FAILURE_MESSAGE
            elif isinstance(caught, Exception):
                failure_message = str(caught)
            else:
                propagated = caught
    if propagated is not None:
        raise propagated from None
    if failure_message is not None:
        print(f"error: {server_id}/{tool} — {failure_message}")
        raise SystemExit(1)


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
        "--config",
        metavar="PATH",
        help="Global GearCore configuration file.",
    )
    parser.add_argument(
        "--profile",
        metavar="NAME",
        help="Select a configured capability profile.",
    )
    parser.add_argument(
        "--context-envelope",
        metavar="PATH",
        help="Signed launch envelope supplied by a trusted launcher.",
    )
    parser.add_argument(
        "--envelope-public-key",
        metavar="PATH",
        help="Issuer-bound public key document for launch-envelope verification.",
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
    p_call.add_argument("server_id", help="MCP server ID (e.g. filesystem)")
    p_call.add_argument("tool", help="Tool name to call (e.g. read_file)")
    p_call.add_argument(
        "args_json", nargs="?", default="", help="JSON-encoded arguments (default: {})"
    )

    # add-mcp
    p_add_mcp = sub.add_parser("add-mcp", help="Register a new MCP server")
    p_add_mcp.add_argument("--id", required=True)
    p_add_mcp.add_argument("--type", default="stdio", choices=["stdio", "sse", "http"])
    # dest must not be "command": that would clobber the subparsers'
    # dest="command" and break dispatch (add-mcp would silently no-op).
    p_add_mcp.add_argument("--command", dest="mcp_command", default="")
    p_add_mcp.add_argument("--args", nargs="*", default=[])
    p_add_mcp.add_argument("--url", default="")
    p_add_mcp.add_argument("--env", nargs="*", metavar="KEY=VALUE", default=[])
    p_add_mcp.add_argument("--scope", default="global", choices=["global", "project"])
    p_add_mcp.add_argument(
        "--allowlist",
        action="store_true",
        help="With --scope project: allowlist an existing global server "
        "instead of writing a project-local definition",
    )
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

    # profile-set
    p_profile = sub.add_parser(
        "profile-set", help="Create or replace a global capability profile"
    )
    p_profile.add_argument("name", help="Global profile name")
    for option in ("mcp-include", "mcp-deny", "mcp-protect"):
        p_profile.add_argument(f"--{option}", action="append", default=[])
    for option in ("skill-include", "skill-deny", "skill-protect"):
        p_profile.add_argument(f"--{option}", action="append", default=[])
    p_profile.add_argument("--core-skill", action="append", default=[])
    p_profile.add_argument("--constrained", action="store_true")
    p_profile.add_argument("--default", dest="make_default", action="store_true")

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


def _resolve_launch_path(value: str | None) -> Path | str | None:
    """Resolve a nonblank launch path while preserving explicit blank input."""

    if value is None or not value.strip():
        return value
    return Path(value).resolve()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main():
    parser = build_parser()
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
        logger.debug("Verbose logging enabled")

    project_path = Path(args.project) if args.project is not None else None
    command = args.command or "serve"

    # Registry authority mutation must not auto-discover or parse a project.
    # It validates and atomically mutates the requested global document itself.
    if command == "profile-set":
        from gearcore_hub.registry import set_profile

        if project_path is not None:
            print("Error: profile-set is global-only", file=sys.stderr)
            raise SystemExit(1)
        try:
            result = set_profile(
                args.name,
                config_path=(Path(args.config).expanduser() if args.config else None),
                mcp_include=args.mcp_include,
                mcp_deny=args.mcp_deny,
                mcp_protect=args.mcp_protect,
                skill_include=args.skill_include,
                skill_deny=args.skill_deny,
                skill_protect=args.skill_protect,
                core_skills=args.core_skill,
                constrained=args.constrained,
                make_default=args.make_default,
            )
        except (ValueError, RuntimeError) as exc:
            print(f"Error: {exc}", file=sys.stderr)
            raise SystemExit(1) from None
        action = "updated" if result.changed else "unchanged"
        print(f"Profile '{result.profile}' {action}")
        return

    config_log_context = (
        _silence_logger("gearcore.config")
        if command in ("status", "list")
        else contextlib.nullcontext()
    )
    skill_log_context = (
        _silence_logger("gearcore.skill_manager")
        if command in ("status", "list")
        else contextlib.nullcontext()
    )
    with config_log_context, skill_log_context:
        config = load_config(
            project=project_path,
            global_config_path=Path(args.config).resolve() if args.config else None,
            profile_name=args.profile,
            context_envelope=_resolve_launch_path(args.context_envelope),
            envelope_public_key=_resolve_launch_path(args.envelope_public_key),
        )

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
                command=args.mcp_command,
                args=args.args,
                url=args.url,
                env=env,
                scope=args.scope,
                project_root=project_path,
                enabled=not args.disabled,
                allowlist=args.allowlist,
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
        except (RuntimeError, FileExistsError, ValueError) as exc:
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
        except (KeyError, FileNotFoundError, ValueError) as exc:
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
