"""
Layered configuration loader for GearCore.

Resolution order (highest priority last):
  1. Built-in defaults
  2. Global  — ~/.config/gearcore/config.yaml
  3. Project — <project>/.gearcore/config.yaml  (only when project context present)
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Literal, Self

import yaml
from pydantic import BaseModel, Field, model_validator

from gearcore_hub.profiles import (
    DisclosureConfig as ProfileDisclosureConfig,
)
from gearcore_hub.profiles import (
    ProfileConfig,
    ProfilesConfig,
    ProjectProfilesConfig,
    ResolvedCapabilities,
    resolve_capabilities,
)
from gearcore_hub.vendor import bundled_superpowers_dir

logger = logging.getLogger("gearcore.config")

# ---------------------------------------------------------------------------
# Schema models
# ---------------------------------------------------------------------------


class McpServerConfig(BaseModel):
    id: str
    type: str = "stdio"
    command: str = ""
    args: list[str] = Field(default_factory=list)
    url: str = ""
    env: dict[str, str] | None = None
    enabled: bool = True


class ResolutionCategory(BaseModel):
    preferred: str = ""
    strategy: str = "namespace"  # suppress_others | namespace | unify
    namespace_prefix: str = ""
    unified_name: str = ""


class ResolutionConfig(BaseModel):
    auto_deduplicate: bool = True
    categories: dict[str, ResolutionCategory] = Field(default_factory=dict)


class DisclosureConfig(BaseModel):
    """Legacy version-2 disclosure configuration."""

    strategy: str = "manual"  # manual | semantic
    activation_threshold: float = 0.85
    core_skills: list[str] = Field(default_factory=list)


_DEFAULT_SKILLS_DIRS = [
    Path.home() / ".config" / "gearcore" / "skills",
    Path.home() / ".config" / "agents" / "skills",
]


def _default_skills_dirs() -> list[Path]:
    dirs = list(_DEFAULT_SKILLS_DIRS)
    bundled = bundled_superpowers_dir()
    if bundled is not None and bundled not in dirs:
        dirs.append(bundled)
    return dirs


class GlobalConfig(BaseModel):
    """Schema for ~/.config/gearcore/config.yaml"""

    version: Literal[2, 3] = 2
    registry: dict[str, Any] = Field(default_factory=dict)
    disclosure: DisclosureConfig = Field(default_factory=DisclosureConfig)
    resolution: ResolutionConfig = Field(default_factory=ResolutionConfig)
    profiles: ProfilesConfig | None = None

    @model_validator(mode="after")
    def validate_profiles_version(self) -> Self:
        if self.version == 3 and self.profiles is None:
            raise ValueError("profiles are required for configuration version 3")
        if self.version == 2 and self.profiles is not None:
            raise ValueError("profiles require configuration version 3")
        return self

    @property
    def mcp_servers(self) -> list[McpServerConfig]:
        raw = self.registry.get("mcp_servers", [])
        return [McpServerConfig(**s) for s in raw]

    @property
    def skills_dirs(self) -> list[Path]:
        raw = self.registry.get("skills_dirs", [])
        dirs: list[Path] = [Path(os.path.expanduser(str(p))) for p in raw]
        if not dirs:
            dirs = _default_skills_dirs()
        bundled = bundled_superpowers_dir()
        if bundled is not None and bundled not in dirs:
            dirs.append(bundled)
        return dirs


class ProjectScope(BaseModel):
    mcp_servers: dict[str, list[str]] = Field(default_factory=dict)  # include: [ids]
    skills: dict[str, list[str]] = Field(default_factory=dict)  # include: [names]


class ProjectContext(BaseModel):
    name: str = ""
    description: str = ""


class ProjectConfig(BaseModel):
    """Schema for <project>/.gearcore/config.yaml"""

    version: Literal[2, 3] = 2
    context: ProjectContext = Field(default_factory=ProjectContext)
    scope: ProjectScope = Field(default_factory=ProjectScope)
    registry: dict[str, Any] = Field(default_factory=dict)  # project-local defs
    disclosure: DisclosureConfig | None = None  # overrides global if present
    profiles: ProjectProfilesConfig | None = None

    @model_validator(mode="after")
    def validate_profiles_version(self) -> Self:
        if self.version == 2 and self.profiles is not None:
            raise ValueError("project profile overlays require configuration version 3")
        return self

    @property
    def mcp_allowlist(self) -> list[str] | None:
        inc = self.scope.mcp_servers.get("include")
        return inc if inc is not None else None

    @property
    def skill_allowlist(self) -> list[str] | None:
        inc = self.scope.skills.get("include")
        return inc if inc is not None else None

    @property
    def mcp_servers(self) -> list[McpServerConfig]:
        raw = self.registry.get("mcp_servers", [])
        return [McpServerConfig(**s) for s in raw]


class EffectiveConfig:
    """
    Merged view produced by the loader.  Consumers should use this only —
    never GlobalConfig / ProjectConfig directly.
    """

    def __init__(
        self,
        global_cfg: GlobalConfig,
        project_cfg: ProjectConfig | None,
        project_root: Path | None,
        *,
        profile_name: str | None = None,
        profile_source: str = "default",
        enforced_profile_name: str | None = None,
        diagnostic_code: str | None = None,
    ):
        self.global_cfg = global_cfg
        self.project_cfg = (
            None if project_cfg is None else project_cfg.model_copy(deep=True)
        )
        self.project_root = project_root
        self._runtime_diagnostic_codes: set[str] = set()
        self._profile_source = profile_source
        self._enforced_profile_name = enforced_profile_name
        self._diagnostic_only = diagnostic_code is not None
        self._project_profile: ProfileConfig | None = None
        self._project_rules: dict[
            str, tuple[tuple[str, ...] | None, tuple[str, ...]]
        ]
        self._mcp_servers: tuple[McpServerConfig, ...]
        if diagnostic_code is not None:
            self._profile_name = "unavailable"
            self._profile = ProfileConfig.model_validate(
                {
                    "constrained": True,
                    "scope": {
                        "mcp_servers": {"include": []},
                        "skills": {"include": []},
                    },
                }
            )
            self._project_profile = None
            self._project_rules = {
                "mcp_servers": (None, ()),
                "skills": (None, ()),
            }
            self._mcp_servers = ()
            self._mcp_capabilities = ResolvedCapabilities(
                active=(), denied=(), protected=(), diagnostics=(diagnostic_code,)
            )
            self._skill_policy_capabilities = ResolvedCapabilities(
                active=(), denied=(), protected=(), diagnostics=()
            )
            return
        if global_cfg.version == 2:
            if profile_name not in (None, "default"):
                raise ValueError("version-2 configuration only has the default profile")
            self._profile_name = "default"
            self._profile = ProfileConfig.model_validate(
                {"disclosure": global_cfg.disclosure.model_dump()}
            )
        else:
            profiles = global_cfg.profiles
            if profiles is None:  # GlobalConfig validation makes this unreachable.
                raise ValueError("profiles are required for configuration version 3")
            self._profile_name = profile_name or profiles.default
            if self._profile_name not in profiles.entries:
                raise ValueError(f"unknown profile {self._profile_name!r}")
            self._profile = profiles.entries[self._profile_name].model_copy(deep=True)
        if (
            self.project_cfg is not None
            and self.project_cfg.version == 3
            and self.project_cfg.profiles is not None
        ):
            overlay = self.project_cfg.profiles.entries.get(self.profile_name)
            if overlay is not None:
                self._project_profile = overlay.model_copy(deep=True)
        self._project_rules = {
            "mcp_servers": self._snapshot_project_capability_rules(
                "mcp_servers"
            ),
            "skills": self._snapshot_project_capability_rules("skills"),
        }
        self._mcp_servers, self._mcp_capabilities = self._resolve_mcp_servers()
        self._skill_policy_capabilities = self.resolve_skill_capabilities((), ())

    @property
    def profile_name(self) -> str:
        return self._profile_name

    @property
    def profile_source(self) -> str:
        return self._profile_source

    @property
    def enforced_profile_name(self) -> str | None:
        return self._enforced_profile_name

    @property
    def diagnostic_only(self) -> bool:
        return self._diagnostic_only

    @property
    def profile(self) -> ProfileConfig:
        return self._profile

    # --- MCP servers ---

    def _snapshot_project_capability_rules(
        self, capability_kind: Literal["mcp_servers", "skills"]
    ) -> tuple[tuple[str, ...] | None, tuple[str, ...]]:
        if self.project_cfg is None:
            return None, ()

        if self.project_cfg.version == 3:
            if self._project_profile is None:
                return None, ()
            policy = getattr(self._project_profile.scope, capability_kind)
            return policy.include, policy.deny

        scope = getattr(self.project_cfg.scope, capability_kind)
        include = scope.get("include")
        # Version-2 denies remain ignored for wholly version-2 configuration.
        deny = scope.get("deny", []) if self.global_cfg.version == 3 else []
        return (
            None if include is None else tuple(include),
            tuple(deny),
        )

    def _project_capability_rules(
        self, capability_kind: Literal["mcp_servers", "skills"]
    ) -> tuple[tuple[str, ...] | None, tuple[str, ...]]:
        return self._project_rules[capability_kind]

    def _filter_project_capabilities(
        self,
        capability_kind: Literal["mcp_servers", "skills"],
        global_capabilities: tuple[str, ...],
        project_capabilities: tuple[str, ...],
        project_include: tuple[str, ...] | None,
    ) -> tuple[str, ...]:
        profile_policy = getattr(self.profile.scope, capability_kind)
        profile_include = profile_policy.include
        global_ids = set(global_capabilities)
        result: list[str] = []
        for capability_id in project_capabilities:
            if (
                self.project_cfg is not None
                and self.project_cfg.version == 3
                and project_include is not None
                and capability_id not in project_include
            ):
                continue
            if self.profile.constrained or self.enforced_profile_name is not None:
                if profile_include is not None:
                    if capability_id not in profile_include:
                        continue
                elif capability_id not in global_ids:
                    continue
            result.append(capability_id)
        return tuple(result)

    def _resolve_mcp_servers(
        self,
    ) -> tuple[tuple[McpServerConfig, ...], ResolvedCapabilities]:
        global_servers = [s for s in self.global_cfg.mcp_servers if s.enabled]
        project_servers = (
            []
            if self.project_cfg is None
            else [s for s in self.project_cfg.mcp_servers if s.enabled]
        )
        project_include, project_deny = self._project_capability_rules(
            "mcp_servers"
        )
        global_ids = tuple(server.id for server in global_servers)
        project_ids = tuple(server.id for server in project_servers)
        resolved = resolve_capabilities(
            global_ids,
            self.profile.scope.mcp_servers,
            project_capabilities=self._filter_project_capabilities(
                "mcp_servers",
                global_ids,
                project_ids,
                project_include,
            ),
            project_include=project_include,
            project_deny=project_deny,
            project_override_attempts=(
                ()
                if self.project_cfg is None
                else tuple(server.id for server in self.project_cfg.mcp_servers)
            ),
        )

        global_by_id = {server.id: server for server in global_servers}
        project_by_id = {server.id: server for server in project_servers}
        protected = set(resolved.protected)
        shadowed = sorted(
            set(global_by_id).intersection(project_by_id).difference(protected)
        )
        if shadowed:
            logger.warning(
                "Project MCP server definition(s) %s override global "
                "definition(s) with the same id",
                ", ".join(shadowed),
            )

        active = set(resolved.active)
        servers = [
            server
            for server in global_servers
            if server.id in active
            and (server.id in protected or server.id not in project_by_id)
        ]
        servers.extend(
            server
            for server in project_servers
            if server.id in active and server.id not in protected
        )
        return tuple(servers), resolved

    @property
    def mcp_servers(self) -> list[McpServerConfig]:
        return [server.model_copy(deep=True) for server in self._mcp_servers]

    @property
    def diagnostic_codes(self) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                (
                    *self._mcp_capabilities.diagnostics,
                    *self._skill_policy_capabilities.diagnostics,
                    *sorted(self._runtime_diagnostic_codes),
                )
            )
        )

    def _record_diagnostic_code(self, code: str) -> None:
        self._runtime_diagnostic_codes.add(code)

    # --- Skills dirs (global first, then project-local) ---

    @property
    def skills_dirs(self) -> list[Path]:
        dirs: list[Path] = list(self.global_cfg.skills_dirs)
        if self.project_root is not None:
            local = self.project_root / ".gearcore" / "skills"
            if local not in dirs:
                dirs.append(local)
        return dirs

    @property
    def global_skill_allowlist(self) -> list[str] | None:
        """None means 'allow all globals'. A list is the explicit allowlist."""
        if self.project_cfg is None:
            return None
        return self.project_cfg.skill_allowlist

    @property
    def protected_skill_names(self) -> tuple[str, ...]:
        return self.profile.scope.skills.protected

    def resolve_skill_capabilities(
        self,
        global_skills: tuple[str, ...],
        project_skills: tuple[str, ...],
    ) -> ResolvedCapabilities:
        project_include, project_deny = self._project_capability_rules("skills")
        return resolve_capabilities(
            global_skills,
            self.profile.scope.skills,
            project_capabilities=self._filter_project_capabilities(
                "skills",
                global_skills,
                project_skills,
                project_include,
            ),
            project_include=project_include,
            project_deny=project_deny,
            project_override_attempts=project_skills,
        )

    # --- Disclosure ---

    @property
    def disclosure(self) -> DisclosureConfig | ProfileDisclosureConfig:
        if self.global_cfg.version == 2:
            if self.project_cfg and self.project_cfg.disclosure:
                return self.project_cfg.disclosure
            return self.global_cfg.disclosure
        return self.profile.disclosure

    # --- Resolution ---

    @property
    def resolution(self) -> ResolutionConfig:
        return self.global_cfg.resolution

    # --- Context label ---

    @property
    def context_name(self) -> str:
        if self.project_cfg and self.project_cfg.context.name:
            return self.project_cfg.context.name
        return "global"


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------

GLOBAL_CONFIG_PATH = Path.home() / ".config" / "gearcore" / "config.yaml"
PROJECT_CONFIG_NAME = ".gearcore"


def _load_yaml(path: Path) -> dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        logger.debug("Loaded config from %s", path)
        return data
    except FileNotFoundError:
        logger.debug("Config not found at %s, skipping", path)
        return {}
    except Exception as exc:
        logger.error("Failed to parse config at %s: %s", path, exc)
        return {}


def find_project_root(start: Path | None = None) -> Path | None:
    """Walk up from *start* (default CWD) looking for a .gearcore/ directory."""
    current = (start or Path.cwd()).resolve()
    while True:
        if (current / PROJECT_CONFIG_NAME).is_dir():
            return current
        parent = current.parent
        if parent == current:
            return None
        current = parent


def load_global_config(global_config_path: Path | None = None) -> GlobalConfig:
    """Load only the global layer (no project detection)."""
    g_path = global_config_path or GLOBAL_CONFIG_PATH
    return GlobalConfig(**_load_yaml(g_path))


def load_config(
    project: Path | None = None,
    global_config_path: Path | None = None,
    *,
    profile_name: str | None = None,
    context_envelope: Path | str | None = None,
    envelope_public_key: Path | str | None = None,
    now: int | None = None,
) -> EffectiveConfig:
    """
    Load and merge global + optional project config.

    Args:
        project: Explicit project root. If None, auto-detect via CWD walk-up.
        global_config_path: Override for global config file (testing / custom installs).
    """
    # --- Global ---
    global_cfg = load_global_config(global_config_path)

    # --- Project ---
    project_root: Path | None = None
    project_cfg: ProjectConfig | None = None

    project_root = project.resolve() if project is not None else find_project_root()

    if project_root is not None:
        p_file = project_root / PROJECT_CONFIG_NAME / "config.yaml"
        p_data = _load_yaml(p_file)
        if p_data:
            project_cfg = ProjectConfig(**p_data)
            logger.info(
                "Project context: %s (%s)",
                project_cfg.context.name or project_root.name,
                project_root,
            )
        else:
            logger.debug("No project config found at %s", p_file)

    envelope_was_supplied = context_envelope is not None
    key_was_supplied = envelope_public_key is not None
    if envelope_was_supplied or key_was_supplied:
        from gearcore_hub.envelope import (
            ENVELOPE_EXPANSION_DIAGNOSTIC,
            INVALID_ENVELOPE_DIAGNOSTIC,
            EnvelopeValidationError,
            profile_is_subset,
            verify_envelope_file,
        )

        def diagnostic(code: str) -> EffectiveConfig:
            return EffectiveConfig(
                global_cfg,
                project_cfg,
                project_root,
                profile_source="invalid-envelope",
                diagnostic_code=code,
            )

        if context_envelope is None or envelope_public_key is None:
            return diagnostic(INVALID_ENVELOPE_DIAGNOSTIC)
        profiles = global_cfg.profiles
        if global_cfg.version != 3 or profiles is None:
            return diagnostic(INVALID_ENVELOPE_DIAGNOSTIC)
        try:
            verified = verify_envelope_file(
                Path(context_envelope),
                Path(envelope_public_key),
                profiles.entries,
                now=now,
            )
        except EnvelopeValidationError:
            return diagnostic(INVALID_ENVELOPE_DIAGNOSTIC)

        enforced_profile = profiles.entries[verified.profile]
        selected_profile_name = profile_name or verified.profile
        selected_profile = profiles.entries.get(selected_profile_name)
        if selected_profile is None or not profile_is_subset(
            selected_profile, enforced_profile
        ):
            return EffectiveConfig(
                global_cfg,
                project_cfg,
                project_root,
                profile_source="envelope",
                enforced_profile_name=verified.profile,
                diagnostic_code=ENVELOPE_EXPANSION_DIAGNOSTIC,
            )
        return EffectiveConfig(
            global_cfg,
            project_cfg,
            project_root,
            profile_name=selected_profile_name,
            profile_source="envelope",
            enforced_profile_name=verified.profile,
        )

    return EffectiveConfig(
        global_cfg, project_cfg, project_root, profile_name=profile_name
    )
