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
  remove         Remove an MCP server, skill, or plugin registration
  sync           Install/update the GearCore self-skill on AI CLI tools
  onboard        Discover/register MCP servers, skills, or whole plugins from a package

Usage:
  gearcore [--project <path>] list-skills [--compact]
  gearcore [--project <path>] request-skill <name>
  gearcore call <server_id> <tool> ['<json_args>'] [--json]
  gearcore [--project <path>] [serve]
  gearcore add-mcp --id <id> --type stdio --command <cmd> [--args ...]
  gearcore add-skill <path> [--scope global|project] [--symlink]
  gearcore add-cli <program> [--scope global|project]
  gearcore remove mcp <id> | skill <name> | plugin <name> [--scope global|project]
  gearcore sync [--tool claude|codex|kimi|opencode] [--dry-run] [--remove]
  gearcore onboard <core-path> [--scope global|project]   # core or plugin root
  gearcore status
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import binascii
import contextlib
import hashlib
import json
import logging
import sys
from pathlib import Path

from mcp.server import NotificationOptions, Server
from mcp.server.models import InitializationOptions
from mcp.server.stdio import stdio_server
from mcp.types import (
    AudioContent,
    EmbeddedResource,
    ImageContent,
    ResourceLink,
    TextContent,
    TextResourceContents,
    Tool,
)

from gearcore_hub.config import EffectiveConfig, load_config
from gearcore_hub.conflict_resolver import ConflictResolver
from gearcore_hub.onboard import cmd_onboard
from gearcore_hub.process_manager import ProcessManager, SharedMCPServer
from gearcore_hub.render import render_skill_instructions
from gearcore_hub.skill_manager import SkillManager

# Max seconds to wait for any single MCP backend to start before giving up
# on it and continuing with the rest (one slow/OAuth-blocked backend must
# not prevent the hub from serving the others).
BACKEND_START_TIMEOUT = 15.0

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("gearcore")


def server_version() -> str:
    """Package version from installed metadata; never drifts from pyproject."""
    try:
        import importlib.metadata

        return importlib.metadata.version("gearcore")
    except Exception:
        return "0.0.0"


def parse_env_args(values: list[str] | None) -> dict[str, str] | None:
    """Parse KEY=value CLI args, warning about and skipping malformed entries."""
    env: dict[str, str] = {}
    for kv in values or []:
        if "=" not in kv:
            logger.warning("Ignoring malformed --env entry %r (expected KEY=value)", kv)
            continue
        key, value = kv.split("=", 1)
        env[key] = value
    return env or None


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
        self.failed_backends: dict[str, str] = {}
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

    async def _start_one_backend(self, server_cfg) -> str | None:
        """Start one backend; return its id on failure, None on success."""
        try:
            await asyncio.wait_for(
                self.process_manager.register_and_start(server_cfg.model_dump()),
                timeout=BACKEND_START_TIMEOUT,
            )
        except TimeoutError:
            logger.error(
                "Backend '%s' failed to start within %ss",
                server_cfg.id,
                BACKEND_START_TIMEOUT,
            )
        except Exception as exc:
            logger.error("Failed to start backend '%s': %s", server_cfg.id, exc)
        else:
            return None
        return str(server_cfg.id)

    async def _start_backends(self):
        # Start all backends concurrently so one slow/OAuth-blocked backend
        # cannot multiply startup latency by the number of registered servers.
        results = await asyncio.gather(
            *(self._start_one_backend(cfg) for cfg in self.config.mcp_servers)
        )
        failed = [server_id for server_id in results if server_id is not None]
        self.failed_backends = dict.fromkeys(failed, "failed to start or timed out")

    async def run(self):
        await self._start_backends()
        if getattr(self, "failed_backends", None):
            logger.warning(
                "GearCore ready (context: %s); unavailable backends: %s",
                self.config.context_name,
                ", ".join(sorted(self.failed_backends)),
            )
        else:
            logger.info("GearCore ready (context: %s)", self.config.context_name)

        try:
            async with stdio_server() as (read_stream, write_stream):
                await self.server.run(
                    read_stream,
                    write_stream,
                    InitializationOptions(
                        server_name="gearcore-hub",
                        server_version=server_version(),
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
    # Lazy import: tests patch gearcore_hub.vendor.* and the other command
    # handlers follow the same deferred-import style.
    from gearcore_hub.vendor import get_upstream_commit_cached, load_vendor_manifest

    manifest = load_vendor_manifest()
    if manifest:
        print("\nVendored skills:")
        sha = manifest.vendored_commit
        short_sha = sha[:12] if len(sha) >= 12 else sha
        print(f"  superpowers @ {short_sha} ({manifest.vendored_at})")
        upstream = get_upstream_commit_cached(manifest.source, manifest.source_ref)
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

# Version tag embedded in `list-skills --compact` payloads so consumers can
# detect the schema shape independently of the package version.
LIST_SKILLS_COMPACT_SCHEMA = "gearcore.list-skills/2"
COMPACT_SKILL_FIELDS = ("name", "description", "scope", "status")
COMPACT_BROKEN_FIELDS = ("name", "target")


def compact_skills_payload(config: EffectiveConfig, sm: SkillManager) -> dict:
    """
    Metadata-only listing for the opt-in `--compact` output mode.

    Never includes SKILL.md bodies, skills hidden by a project allowlist,
    or metadata for broken/malformed bundles outside the visible scope.
    `complete` is always True: this ticket imposes no response budget.

    Version 2 is a columnar payload: `skill_fields` declares each value in a
    skill row, `source_identity` derives its stable source identity, and
    `request_template` derives the exact request command. `broken_fields`
    similarly declares the values in each broken row.
    """
    visible = sm.visible_skill_names
    skills: list[list[str]] = []
    for name in sorted(n for n in sm.skills if n in visible):
        bundle = sm.skills[name]
        skills.append(
            [
                bundle.manifest.name,
                bundle.manifest.description,
                "project" if bundle.is_project_local else "global",
                "active" if name in sm.active_skills else "available",
            ]
        )
    broken = [
        [broken_name, target]
        for broken_name, target in sorted(sm.broken_skills.items())
        if broken_name in sm.visible_broken_skill_names
    ]
    return {
        "schema": LIST_SKILLS_COMPACT_SCHEMA,
        "context": config.context_name,
        "complete": True,
        "skill_fields": COMPACT_SKILL_FIELDS,
        "source_identity": "{scope}:{name}",
        "request_template": "gearcore request-skill {name}",
        "skills": skills,
        "broken_fields": COMPACT_BROKEN_FIELDS,
        "broken": broken,
    }


def cmd_list_skills(config: EffectiveConfig, compact: bool = False):
    sm = SkillManager(config)
    if compact:
        payload = compact_skills_payload(config, sm)
        print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
        return

    skills = sm.list_available_skills()
    ctx = config.context_name
    print(f"GearCore skills ({ctx} context):\n")
    if not skills:
        print("  (no skills visible in this context)")
        return

    # Level-0 skills: reveal full instructions inline, before the listing.
    level0 = [
        name for name in config.disclosure.core_skills if name in sm.visible_skill_names
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

# Version tag embedded in `call --json` envelopes so shell automation can
# detect the schema shape independently of the package version.
CALL_SCHEMA = "gearcore.call/1"

DEVCORE_SERVER_ID = "devcore"
DEVCORE_COMMAND_TOOLS = frozenset({"devcore_run", "devcore_poll"})

# Outcome statuses carried by the structured envelope. The three failure
# classes required by the ticket plus usage/config errors that happen before
# any backend is contacted.
CALL_STATUS_SUCCESS = "success"
CALL_STATUS_USAGE_ERROR = "usage_error"
CALL_STATUS_TRANSPORT_ERROR = "transport_error"
CALL_STATUS_MCP_TOOL_ERROR = "mcp_tool_error"
CALL_STATUS_NESTED_COMMAND_FAILURE = "nested_command_failure"

# Exit mapping for `call --json`. 2 keeps the argparse usage-error convention
# (argparse exits 2 for malformed CLI usage; this is the semantic equivalent
# for unknown/disabled servers and bad JSON args). 3-5 distinguish the three
# tool-outcome failure classes. Legacy text mode keeps its single coarse
# nonzero code (1) for every failure.
CALL_EXIT_USAGE_ERROR = 2
CALL_EXIT_TRANSPORT_ERROR = 3
CALL_EXIT_MCP_TOOL_ERROR = 4
CALL_EXIT_NESTED_COMMAND_FAILURE = 5

_CALL_STATUS_EXIT = {
    CALL_STATUS_USAGE_ERROR: CALL_EXIT_USAGE_ERROR,
    CALL_STATUS_TRANSPORT_ERROR: CALL_EXIT_TRANSPORT_ERROR,
    CALL_STATUS_MCP_TOOL_ERROR: CALL_EXIT_MCP_TOOL_ERROR,
    CALL_STATUS_NESTED_COMMAND_FAILURE: CALL_EXIT_NESTED_COMMAND_FAILURE,
}


def _binary_content_fields(encoded: str) -> dict:
    """Describe base64 content without emitting the encoded or raw payload."""
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (binascii.Error, TypeError, ValueError):
        # Undecodable payload: keep an explicitly bounded description of the
        # base64 text instead of guessing a byte length or echoing it.
        return {"data_encoding": "base64", "encoded_length": len(encoded)}
    return {
        "byte_length": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }


def _binary_value_metadata(value) -> dict:
    """Describe an unknown binary field without retaining its value."""
    if isinstance(value, (bytes, bytearray, memoryview)):
        raw = bytes(value)
        return {
            "byte_length": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
        }
    if isinstance(value, str):
        return _binary_content_fields(value)

    metadata: dict[str, object] = {"binary_type": type(value).__name__}
    with contextlib.suppress(TypeError):
        metadata["value_length"] = len(value)
    return metadata


def _sanitize_unknown_json(value, *, field_name: str | None = None):
    """Recursively make future content JSON-safe without leaking binary fields."""
    if field_name is not None and field_name.casefold() in {"data", "blob"}:
        return _binary_value_metadata(value)
    if isinstance(value, dict):
        return {
            str(key): _sanitize_unknown_json(item, field_name=str(key))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_sanitize_unknown_json(item) for item in value]
    if isinstance(value, tuple):
        return [_sanitize_unknown_json(item) for item in value]

    try:
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError):
        return {"type": type(value).__name__}
    return value


def _model_json_dict(item) -> dict | None:
    """Serialize a Pydantic content model to a JSON-safe mapping."""
    try:
        data = json.loads(item.model_dump_json(exclude_none=True))
    except Exception:
        try:
            data = item.model_dump(mode="json", exclude_none=True)
        except Exception:
            return None
    return data if isinstance(data, dict) else None


def _stable_mime_type(data: dict) -> dict:
    """Retain the structured envelope's snake-case media-type key."""
    if "mimeType" in data:
        data["mime_type"] = data.pop("mimeType")
    return data


def _normalize_content(content) -> list[dict]:
    """Normalize MCP content blocks in order, never emitting raw binary.

    Pydantic JSON-safe metadata is preserved for every known block. Text and
    text-resource payloads are preserved verbatim; image/audio/blob payloads
    are represented by type, media type, byte length, and a stable sha256
    digest; resource links keep their metadata fields.
    """
    blocks: list[dict] = []
    for item in content or []:
        if isinstance(item, TextContent):
            data = _model_json_dict(item)
            blocks.append(data or {"type": "text", "text": item.text})
        elif isinstance(item, ImageContent):
            data = _model_json_dict(item) or {"type": "image"}
            data.pop("data", None)
            data.update(_binary_content_fields(item.data))
            blocks.append(_stable_mime_type(data))
        elif isinstance(item, AudioContent):
            data = _model_json_dict(item) or {"type": "audio"}
            data.pop("data", None)
            data.update(_binary_content_fields(item.data))
            blocks.append(_stable_mime_type(data))
        elif isinstance(item, EmbeddedResource):
            resource = item.resource
            outer = _model_json_dict(item) or {"type": "resource"}
            nested = _model_json_dict(resource) or {}
            block = {
                "type": outer.get("type", "resource"),
                "uri": str(resource.uri),
            }
            if resource.mimeType is not None:
                block["mime_type"] = resource.mimeType
            if isinstance(resource, TextResourceContents):
                block["text"] = resource.text
            else:
                binary = nested.pop("blob", resource.blob)
                binary_metadata = _binary_content_fields(binary)
                block.update(binary_metadata)
                nested.update(binary_metadata)

            # Keep the established flattened keys while retaining the outer
            # block metadata and the complete sanitized nested resource.
            for key, value in outer.items():
                if key not in {"type", "resource"}:
                    block[key] = value
            nested = _stable_mime_type(nested)
            block["resource"] = nested
            blocks.append(block)
        elif isinstance(item, ResourceLink):
            data = _model_json_dict(item) or {
                "type": "resource_link",
                "name": item.name,
                "uri": str(item.uri),
            }
            data["uri"] = str(item.uri)
            blocks.append(_stable_mime_type(data))
        else:
            blocks.append(_normalize_unknown_content(item))
    return blocks


def _normalize_unknown_content(item) -> dict:
    """JSON-safe fallback for future content types; binary fields bounded."""
    data = _model_json_dict(item)
    if data is None:
        return {"type": type(item).__name__}
    sanitized = _sanitize_unknown_json(data)
    return sanitized if isinstance(sanitized, dict) else {"type": type(item).__name__}


def _devcore_command_failure(result) -> bool:
    """Narrow nested-adapter gate for the DevCore command tools.

    DevCore command tools (devcore_run/devcore_poll)
    return a single JSON text object. Only when that object satisfies the
    DevCore run contract — ``ok`` bool, ``exit_code`` int, ``timed_out`` bool,
    ``elapsed_seconds`` non-negative number, with ``ok == (exit_code == 0 and
    not timed_out)``, the exact validation DevCore itself applies — and ``ok``
    is False is this a nested command failure. Anything else (domain payloads,
    plain text, multi-block results, malformed JSON) is not interpreted.
    """
    if len(result.content) != 1:
        return False
    block = result.content[0]
    if not isinstance(block, TextContent):
        return False
    try:
        data = json.loads(block.text)
    except ValueError:
        return False
    if not isinstance(data, dict):
        return False
    ok = data.get("ok")
    exit_code = data.get("exit_code")
    timed_out = data.get("timed_out")
    elapsed = data.get("elapsed_seconds")
    if (
        type(ok) is not bool
        or type(exit_code) is not int
        or type(timed_out) is not bool
        or not isinstance(elapsed, (int, float))
        or isinstance(elapsed, bool)
        or elapsed < 0
    ):
        return False
    if ok != (exit_code == 0 and not timed_out):
        return False
    return ok is False


def _build_call_envelope(
    server_id: str,
    tool: str,
    status: str,
    *,
    mcp_is_error: bool = False,
    result=None,
    error: str | None = None,
) -> dict:
    envelope = {
        "schema": CALL_SCHEMA,
        "server": server_id,
        "tool": tool,
        "ok": status == CALL_STATUS_SUCCESS,
        "status": status,
        "mcp_is_error": bool(mcp_is_error),
        "content": _normalize_content(result.content) if result is not None else [],
        "structured_content": (
            getattr(result, "structuredContent", None) if result is not None else None
        ),
    }
    if error is not None:
        envelope["error"] = error
    return envelope


def _emit_call_envelope(envelope: dict) -> None:
    print(json.dumps(envelope, sort_keys=True, separators=(",", ":")))


def cmd_call(
    config: EffectiveConfig,
    server_id: str,
    tool: str,
    args_json: str,
    structured: bool = False,
):
    """Invoke one tool on one MCP backend, statelessly.

    Legacy text mode (default) prints content exactly as before; the only
    behavior change is failure-aware exits (nonzero for MCP tool errors and
    nested DevCore command failures). Structured mode (--json) emits exactly
    one deterministic JSON envelope on stdout, diagnostics on stderr, and
    classifies failures by exit code.
    """
    import json as _json

    # Find the server config (use effective config to respect project scope)
    server_cfg = None
    for s in config.mcp_servers:
        if s.id == server_id:
            server_cfg = s
            break

    if not server_cfg:
        message = f"server '{server_id}' not found in gearcore config"
        if structured:
            _emit_call_envelope(
                _build_call_envelope(
                    server_id, tool, CALL_STATUS_USAGE_ERROR, error=message
                )
            )
            print(f"error: {message}", file=sys.stderr)
            sys.exit(CALL_EXIT_USAGE_ERROR)
        print(f"error: {message}")
        sys.exit(1)

    # config.mcp_servers already filters to enabled servers, but double-check
    if not server_cfg.enabled:
        message = f"server '{server_id}' is disabled in gearcore config"
        if structured:
            _emit_call_envelope(
                _build_call_envelope(
                    server_id, tool, CALL_STATUS_USAGE_ERROR, error=message
                )
            )
            print(f"error: {message}", file=sys.stderr)
            sys.exit(CALL_EXIT_USAGE_ERROR)
        print(f"error: {message}")
        sys.exit(1)

    try:
        tool_args = _json.loads(args_json) if args_json else {}
    except _json.JSONDecodeError as exc:
        message = f"invalid JSON arguments: {exc}"
        if structured:
            _emit_call_envelope(
                _build_call_envelope(
                    server_id, tool, CALL_STATUS_USAGE_ERROR, error=message
                )
            )
            print(f"error: {message}", file=sys.stderr)
            sys.exit(CALL_EXIT_USAGE_ERROR)
        print(f"error: {message}")
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
        finally:
            await server.stop()
        return result

    try:
        result = asyncio.run(_run())
    except Exception as exc:
        if structured:
            _emit_call_envelope(
                _build_call_envelope(
                    server_id,
                    tool,
                    CALL_STATUS_TRANSPORT_ERROR,
                    error=str(exc),
                )
            )
            print(f"error: {server_id}/{tool} — {exc}", file=sys.stderr)
            sys.exit(CALL_EXIT_TRANSPORT_ERROR)
        print(f"error: {server_id}/{tool} — {exc}")
        sys.exit(1)

    nested_failure = (
        server_id == DEVCORE_SERVER_ID
        and tool in DEVCORE_COMMAND_TOOLS
        and _devcore_command_failure(result)
    )

    if structured:
        if result.isError:
            status = CALL_STATUS_MCP_TOOL_ERROR
        elif nested_failure:
            status = CALL_STATUS_NESTED_COMMAND_FAILURE
        else:
            status = CALL_STATUS_SUCCESS
        _emit_call_envelope(
            _build_call_envelope(
                server_id,
                tool,
                status,
                mcp_is_error=bool(result.isError),
                result=result,
            )
        )
        if status != CALL_STATUS_SUCCESS:
            print(f"error: {server_id}/{tool} — {status}", file=sys.stderr)
            sys.exit(_CALL_STATUS_EXIT[status])
        return

    # Legacy text mode: stdout shape is frozen compatibility surface.
    for content in result.content:
        if hasattr(content, "text"):
            print(content.text)
        elif hasattr(content, "data"):
            data = content.data
            if isinstance(data, bytes):
                data = data.decode("utf-8", errors="replace")
            print(data)

    if result.isError:
        print(f"error: {server_id}/{tool} — tool reported isError", file=sys.stderr)
        sys.exit(1)
    if nested_failure:
        print(
            f"error: {server_id}/{tool} — DevCore command failed (ok=false)",
            file=sys.stderr,
        )
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
    p_list_skills = sub.add_parser(
        "list-skills", help="Enumerate available skills in the current context"
    )
    p_list_skills.add_argument(
        "--compact",
        action="store_true",
        help="Emit deterministic compact JSON metadata (no SKILL.md bodies)",
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
    p_call.add_argument(
        "--json",
        action="store_true",
        help="Emit one deterministic versioned JSON envelope on stdout "
        "(schema gearcore.call/1); exit codes: 0 success, 2 usage error, "
        "3 transport error, 4 MCP tool error, 5 nested command failure",
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

    # remove
    p_remove = sub.add_parser("remove", help="Remove an MCP server, skill, or plugin")
    p_remove.add_argument("type", choices=["mcp", "skill", "plugin"])
    p_remove.add_argument(
        "name", help="ID (for mcp), name (for skill), or plugin name (for plugin)"
    )
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

    # onboard
    p_onboard = sub.add_parser(
        "onboard",
        help="Discover/register MCP servers, skills, or whole plugins from a package",
    )
    p_onboard.add_argument("core_path", help="Path to the core directory")
    p_onboard.add_argument("--scope", default="global", choices=["global", "project"])
    p_onboard.add_argument(
        "--mcp-id",
        help="Explicit MCP id (defaults to single skill name or script-derived name)",
    )
    p_onboard.add_argument(
        "--mcp-script",
        help="Explicit MCP script name from [project.scripts] (required if ambiguous)",
    )
    p_onboard.add_argument(
        "--copy-skills",
        action="store_true",
        help="Copy instead of symlink (whole plugin root for detected plugins)",
    )
    p_onboard.add_argument("--dry-run", action="store_true")
    p_onboard.add_argument(
        "--tool",
        nargs="*",
        metavar="TOOL",
        help="Specific tools to sync after onboarding (claude|codex|kimi|opencode).",
    )
    p_onboard.add_argument(
        "--no-sync", action="store_true", help="Skip sync after onboarding"
    )

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

    # update
    p_update = sub.add_parser(
        "update",
        help="Refresh registered MCP servers, skills, superpowers, and self-skill sync",
    )
    p_update.add_argument(
        "resource",
        nargs="?",
        choices=["mcp", "skill", "superpowers"],
        help="Resource type to update",
    )
    p_update.add_argument(
        "name",
        nargs="?",
        help="MCP server id or skill name (only when resource is specified)",
    )
    p_update.add_argument(
        "--dry-run",
        action="store_true",
        help="Show pending changes without applying",
    )
    p_update.add_argument(
        "--source-path",
        type=Path,
        help="Override source path for mcp/skill update",
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
        cmd_list_skills(config, compact=args.compact)
        return

    if command == "request-skill":
        cmd_request_skill(config, args.name)
        return

    if command == "call":
        cmd_call(
            config, args.server_id, args.tool, args.args_json, structured=args.json
        )
        return

    if command == "serve":
        hub = GearCoreHub(config)
        with contextlib.suppress(KeyboardInterrupt):
            asyncio.run(hub.run())
        return

    if command == "add-mcp":
        from gearcore_hub.registry import add_mcp

        env = parse_env_args(args.env)
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
        except (RuntimeError, FileExistsError) as exc:
            print(f"Error: {exc}", file=sys.stderr)
            sys.exit(1)
        return

    if command == "remove":
        from gearcore_hub.registry import remove_mcp, remove_plugin, remove_skill

        try:
            if args.type == "mcp":
                remove_mcp(args.name, scope=args.scope, project_root=project_path)
            elif args.type == "skill":
                remove_skill(args.name, scope=args.scope, project_root=project_path)
            else:
                remove_plugin(args.name, scope=args.scope, project_root=project_path)
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

    if command == "update":
        from gearcore_hub.update import cmd_update

        cmd_update(config, args)
        return

    if command == "onboard":
        cmd_onboard(args, project_path)
        return

    parser.print_help()


if __name__ == "__main__":
    main()
