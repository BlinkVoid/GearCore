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
import re
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, NoReturn, Self, SupportsIndex
from urllib.parse import parse_qsl, unquote, urlsplit

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    GetCoreSchemaHandler,
    SecretStr,
    ValidationError,
    field_serializer,
    field_validator,
    model_validator,
)
from pydantic_core import core_schema

from gearcore_hub.credentials import CredentialError, validate_credential_id
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


_AUTH_MODEL_CONFIG = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
_ENVIRONMENT_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_AUTH_REFERENCE_FIELDS = {"credential_ref", "http_scheme", "stdio_environment"}
_SENSITIVE_NAME_PARTS = {
    "auth",
    "accesskey",
    "apikey",
    "authorization",
    "bearer",
    "cookie",
    "credential",
    "credentials",
    "password",
    "pat",
    "privatekey",
    "secret",
    "token",
}
_SAFE_REFERENCE_SUFFIXES = {"file", "path", "ref"}
_SENSITIVE_CARRIER_SUFFIXES = {"header", "value"}
_SENSITIVE_SEGMENT_PAIRS = {("access", "key"), ("api", "key"), ("private", "key")}


class McpConfigError(RuntimeError):
    """Stable error that never retains rejected configuration input."""


class _SanitizedJsonSchemaValidator:
    """Delegate a model validator while hiding malformed JSON input.

    Pydantic parses JSON before entering a model's core schema.  This proxy is
    installed per model through ``__pydantic_on_complete__`` so parser failures
    receive the same sanitized domain boundary without changing global
    Pydantic behavior or any non-JSON validator method.
    """

    __slots__ = ("_assignment_preflight", "_error_message", "_validator")

    def __init__(
        self,
        validator: Any,
        error_message: str,
        assignment_preflight: Callable[[str, object], None] | None = None,
    ) -> None:
        self._validator = validator
        self._error_message = error_message
        self._assignment_preflight = assignment_preflight

    def __getattr__(self, name: str) -> Any:
        return getattr(self._validator, name)

    def validate_json(self, *args: Any, **kwargs: Any) -> Any:
        validation_failed = False
        try:
            return self._validator.validate_json(*args, **kwargs)
        except ValidationError:
            validation_failed = True
        if validation_failed:
            # Raise after leaving the except suite so the raw parser error and
            # its input are absent from both __context__ and __cause__.
            raise McpConfigError(self._error_message)
        raise AssertionError("unreachable sanitized JSON validation state")

    def validate_assignment(
        self, instance: object, field_name: str, value: object, *args: Any, **kwargs: Any
    ) -> Any:
        if self._assignment_preflight is not None:
            self._assignment_preflight(field_name, value)
        validation_failed = False
        try:
            validated = self._validator.validate_assignment(
                instance, field_name, value, *args, **kwargs
            )
        except ValidationError:
            validation_failed = True
        if validation_failed:
            raise McpConfigError(self._error_message)
        return validated


def _immutable_container_error() -> NoReturn:
    raise TypeError("MCP configuration containers are immutable")


class _FrozenList(tuple[Any, ...]):
    """Read-compatible list that cannot mutate model-owned state."""

    def __new__(cls, value: Any = ()) -> Self:
        return super().__new__(cls, value)

    def __repr__(self) -> str:
        return repr(list(self))

    def __eq__(self, other: object) -> bool:
        if isinstance(other, list):
            return list(self) == other
        return super().__eq__(other)

    __hash__ = tuple.__hash__

    def __copy__(self) -> Self:
        return self

    def __deepcopy__(self, memo: dict[int, Any]) -> Self:
        return self

    def __setitem__(self, key: Any, value: Any) -> None:
        _immutable_container_error()

    def __delitem__(self, key: Any) -> None:
        _immutable_container_error()

    def __iadd__(self, value: Any) -> Self:  # type: ignore[misc]
        _immutable_container_error()

    def __imul__(self, value: Any) -> Self:  # type: ignore[misc]
        _immutable_container_error()

    def append(self, value: Any) -> None:
        _immutable_container_error()

    def extend(self, value: Any) -> None:
        _immutable_container_error()

    def insert(self, index: SupportsIndex, value: Any) -> None:
        _immutable_container_error()

    def remove(self, value: Any) -> None:
        _immutable_container_error()

    def pop(self, index: SupportsIndex = -1) -> Any:
        _immutable_container_error()

    def clear(self) -> None:
        _immutable_container_error()

    def sort(self, *args: Any, **kwargs: Any) -> None:
        _immutable_container_error()

    def reverse(self) -> None:
        _immutable_container_error()


class _FrozenDict(Mapping[Any, Any]):
    """Read-compatible dict that cannot mutate model-owned state."""

    __slots__ = ("_data",)

    def __init__(self, value: Mapping[Any, Any]) -> None:
        self._data = dict(value)

    def __getitem__(self, key: Any) -> Any:
        return self._data[key]

    def __iter__(self) -> Iterator[Any]:
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)

    def __repr__(self) -> str:
        return repr(self._data)

    def __copy__(self) -> Self:
        return self

    def __deepcopy__(self, memo: dict[int, Any]) -> Self:
        return self

    def __setitem__(self, key: Any, value: Any) -> None:
        _immutable_container_error()

    def __delitem__(self, key: Any) -> None:
        _immutable_container_error()

    def __ior__(self, value: Any) -> Self:
        _immutable_container_error()

    def clear(self) -> None:
        _immutable_container_error()

    def pop(self, key: Any, default: Any = None) -> Any:
        _immutable_container_error()

    def popitem(self) -> tuple[Any, Any]:
        _immutable_container_error()

    def setdefault(self, key: Any, default: Any = None) -> Any:
        _immutable_container_error()

    def update(self, *args: Any, **kwargs: Any) -> None:
        _immutable_container_error()


def _is_sensitive_config_name(name: object) -> bool:
    """Conservatively identify names conventionally used to carry secrets.

    A terminal sensitive segment (API_TOKEN, HIVE_AUTH), a known header/value
    carrier, or a compact conventional name (APIKEY) is rejected. Reference
    suffixes FILE, PATH, and REF are allowed, as are unrelated purposes such
    as PASSWORD_POLICY and TOKENIZER_PATH.
    """

    separated = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(name))
    normalized = separated.casefold()
    parts = tuple(part for part in re.split(r"[^a-z0-9]+", normalized) if part)
    if not parts or parts[-1] in _SAFE_REFERENCE_SUFFIXES:
        return False
    return (
        parts[-1] in _SENSITIVE_NAME_PARTS
        or (
            parts[-1] in _SENSITIVE_CARRIER_SUFFIXES
            and bool(_SENSITIVE_NAME_PARTS.intersection(parts[:-1]))
        )
        or any(
            pair in _SENSITIVE_SEGMENT_PAIRS
            for pair in zip(parts, parts[1:], strict=False)
        )
    )


def _contains_plaintext_auth_route(
    value: object, *, typed_auth: bool = False, server_root: bool = False
) -> bool:
    """Inspect server mappings without retaining or rendering their values."""

    if isinstance(value, dict):
        for key, nested in value.items():
            normalized_key = str(key).casefold().replace("-", "_")
            is_typed_auth_container = server_root and normalized_key == "auth"
            if (
                not is_typed_auth_container
                and not (typed_auth and normalized_key in _AUTH_REFERENCE_FIELDS)
                and (
                    normalized_key == "credential_ref"
                    or _is_sensitive_config_name(key)
                )
            ):
                return True
            if _contains_plaintext_auth_route(
                nested,
                typed_auth=is_typed_auth_container,
                server_root=False,
            ):
                return True
    elif isinstance(value, (list, tuple)):
        return any(_contains_plaintext_auth_route(item) for item in value)
    return False


def _header_contains_plaintext_auth(value: object) -> bool:
    if not isinstance(value, str):
        return False
    decoded = unquote(value).lstrip("=")
    name, separator, header_value = decoded.partition(":")
    if not separator:
        return False
    return _is_sensitive_config_name(name) or header_value.lstrip().casefold().startswith(
        "bearer "
    )


def _arguments_contain_plaintext_auth(value: object) -> bool:
    if not isinstance(value, (list, tuple)):
        return False
    for index, argument in enumerate(value):
        if not isinstance(argument, str):
            continue
        if argument == "-H":
            header = value[index + 1] if index + 1 < len(value) else None
            if _header_contains_plaintext_auth(header):
                return True
            continue
        if argument.startswith("-H") and len(argument) > 2:
            if _header_contains_plaintext_auth(argument[2:]):
                return True
            continue
        if argument.startswith("--"):
            option, separator, inline_value = argument[2:].partition("=")
            separated = re.sub(r"([a-z0-9])([A-Z])", r"\1-\2", option)
            option_parts = tuple(
                part
                for part in re.split(r"[^a-z0-9]+", separated.casefold())
                if part
            )
            if option_parts and option_parts[-1] in {"header", "headers"}:
                header = (
                    inline_value
                    if separator
                    else value[index + 1]
                    if index + 1 < len(value)
                    else None
                )
                if _header_contains_plaintext_auth(header):
                    return True
                continue
            if _is_sensitive_config_name(option):
                return True
    return False


def _url_contains_plaintext_auth(value: object) -> bool:
    if not isinstance(value, str) or not value:
        return False
    invalid = False
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme.casefold() not in {"http", "https"}
            or not parsed.netloc
            or parsed.hostname is None
        ):
            invalid = True
        # Access validates a malformed/non-numeric port eagerly.
        _ = parsed.port
        if parsed.username is not None or parsed.password is not None:
            return True
        if any(_is_sensitive_config_name(key) for key, _ in parse_qsl(parsed.query)):
            return True
    except ValueError:
        invalid = True
    if invalid:
        # Malformed authority syntax can obscure userinfo or query boundaries.
        raise McpConfigError("invalid MCP server URL")
    return False


def _preflight_auth_input(value: object) -> None:
    if value is None:
        return
    if not isinstance(value, dict):
        raise McpConfigError("invalid MCP authentication configuration")
    if set(value).difference(_AUTH_REFERENCE_FIELDS):
        raise McpConfigError("invalid MCP authentication configuration")
    reference = value.get("credential_ref")
    if isinstance(reference, SecretStr):
        reference = reference.get_secret_value()
    if not isinstance(reference, str):
        raise McpConfigError("invalid MCP authentication configuration")
    invalid_reference = False
    try:
        validate_credential_id(reference)
    except CredentialError:
        invalid_reference = True
    if invalid_reference:
        raise McpConfigError("invalid MCP authentication configuration")


class _SanitizedConfigModel(BaseModel):
    """Core-schema boundary that replaces input-retaining errors."""

    @classmethod
    def _preflight_input(cls, value: object) -> None:
        raise NotImplementedError

    @classmethod
    def _validation_error_message(cls, value: object) -> str:
        return "invalid configuration document"

    @classmethod
    def _preflight_assignment(cls, field_name: str, value: object) -> None:
        return

    @classmethod
    def _install_json_validation_boundary(cls) -> None:
        validator = cls.__pydantic_validator__
        if not isinstance(validator, _SanitizedJsonSchemaValidator):
            cls.__pydantic_validator__ = _SanitizedJsonSchemaValidator(  # type: ignore[assignment]
                validator,
                cls._validation_error_message(None),
                cls._preflight_assignment,
            )

    @classmethod
    def __pydantic_on_complete__(cls) -> None:
        cls._install_json_validation_boundary()

    @classmethod
    def model_rebuild(cls, **kwargs: Any) -> bool | None:
        rebuilt = super().model_rebuild(**kwargs)
        cls._install_json_validation_boundary()
        return rebuilt

    def __setattr__(self, field_name: str, value: Any) -> None:
        model_type = type(self)
        model_type._preflight_assignment(field_name, value)
        validation_failed = False
        try:
            super().__setattr__(field_name, value)
        except ValidationError:
            validation_failed = True
        if validation_failed:
            raise McpConfigError(
                model_type._validation_error_message({field_name: value})
            )

    @classmethod
    def __get_pydantic_core_schema__(
        cls, source_type: Any, handler: GetCoreSchemaHandler
    ) -> core_schema.CoreSchema:
        schema = handler(source_type)
        return core_schema.no_info_wrap_validator_function(
            cls._validate_without_retaining_input, schema
        )

    @classmethod
    def _validate_without_retaining_input(
        cls, value: object, handler: core_schema.ValidatorFunctionWrapHandler
    ) -> object:
        if isinstance(value, cls):
            return handler(value)
        cls._preflight_input(value)
        validation_failed = False
        try:
            validated = handler(value)
        except ValidationError:
            validation_failed = True
        if validation_failed:
            # Raise outside the except suite so the discarded Pydantic error is
            # absent from both __context__ and __cause__.
            raise McpConfigError(cls._validation_error_message(value))
        return validated


class McpAuthConfig(_SanitizedConfigModel):
    """References and transport settings only; never credential material."""

    model_config = _AUTH_MODEL_CONFIG

    credential_ref: SecretStr
    stdio_environment: str = ""
    http_scheme: Literal["bearer", ""] = ""

    @classmethod
    def _preflight_input(cls, value: object) -> None:
        _preflight_auth_input(value)

    @classmethod
    def _validation_error_message(cls, value: object) -> str:
        return "invalid MCP authentication configuration"

    @classmethod
    def _preflight_assignment(cls, field_name: str, value: object) -> None:
        raise McpConfigError("MCP authentication configuration is immutable")

    @field_validator("credential_ref", mode="before")
    @classmethod
    def validate_reference(cls, value: object) -> str:
        raw_value = value.get_secret_value() if isinstance(value, SecretStr) else value
        if not isinstance(raw_value, str):
            raise ValueError("invalid credential reference")
        try:
            return validate_credential_id(raw_value)
        except CredentialError:
            raise ValueError("invalid credential reference") from None

    @field_validator("stdio_environment")
    @classmethod
    def validate_environment(cls, value: str) -> str:
        if value and _ENVIRONMENT_NAME.fullmatch(value) is None:
            raise ValueError("invalid stdio credential environment")
        return value

    def credential_id(self) -> str:
        """Return the identifier only at the credential lookup boundary."""

        return self.credential_ref.get_secret_value()


def _preflight_server_input(value: object) -> None:
    if not isinstance(value, dict):
        raise McpConfigError("invalid MCP server configuration")
    if (
        _contains_plaintext_auth_route(value, server_root=True)
        or _arguments_contain_plaintext_auth(value.get("args"))
        or _url_contains_plaintext_auth(value.get("url"))
    ):
        raise McpConfigError("plaintext authentication material is forbidden")
    auth = value.get("auth")
    if not isinstance(auth, McpAuthConfig):
        _preflight_auth_input(auth)


class McpServerConfig(_SanitizedConfigModel):
    # Keep legacy unknown extension fields permissive for version-2
    # compatibility, but prevent Pydantic errors from echoing accidental
    # plaintext credential values.
    model_config = ConfigDict(hide_input_in_errors=True, validate_assignment=True)

    id: str
    type: str = "stdio"
    command: str = ""
    args: list[str] = Field(default_factory=list)
    url: str = ""
    env: dict[str, str] | None = None
    auth: McpAuthConfig | None = None
    enabled: bool = True

    @classmethod
    def _preflight_input(cls, value: object) -> None:
        _preflight_server_input(value)

    @classmethod
    def _validation_error_message(cls, value: object) -> str:
        if isinstance(value, dict) and value.get("auth") is not None:
            return "invalid MCP authentication configuration"
        return "invalid MCP server configuration"

    @classmethod
    def _preflight_assignment(cls, field_name: str, value: object) -> None:
        _preflight_server_input({field_name: value})

    @field_validator("args", mode="after")
    @classmethod
    def freeze_args(cls, value: list[str]) -> list[str]:
        return _FrozenList(value)  # type: ignore[return-value]

    @field_validator("env", mode="after")
    @classmethod
    def freeze_environment(
        cls, value: dict[str, str] | None
    ) -> dict[str, str] | None:
        return None if value is None else _FrozenDict(value)  # type: ignore[return-value]

    @field_serializer("args")
    def serialize_args(self, value: list[str]) -> list[str]:
        return list(value)

    @field_serializer("env")
    def serialize_environment(
        self, value: dict[str, str] | None
    ) -> dict[str, str] | None:
        return None if value is None else dict(value)

    @model_validator(mode="before")
    @classmethod
    def reject_plaintext_auth_fields(cls, value: Any) -> Any:
        if _contains_plaintext_auth_route(value, server_root=True):
            raise ValueError("plaintext authentication fields are forbidden")
        return value

    @model_validator(mode="after")
    def validate_auth_transport(self) -> Self:
        if self.auth is not None:
            if self.type == "stdio":
                valid = bool(self.auth.stdio_environment) and not self.auth.http_scheme
            elif self.type in {"sse", "http"}:
                valid = (
                    not self.auth.stdio_environment
                    and self.auth.http_scheme == "bearer"
                )
            else:
                valid = False
            if not valid:
                raise ValueError(
                    "invalid authentication configuration for MCP transport"
                )
        return self


def _sanitize_registry_servers(data: dict[str, Any]) -> dict[str, Any]:
    """Validate server entries before outer Pydantic models can retain input."""

    registry = data.get("registry")
    if not isinstance(registry, dict) or "mcp_servers" not in registry:
        return data
    raw_servers = registry["mcp_servers"]
    if not isinstance(raw_servers, list):
        raise McpConfigError("invalid MCP server configuration")
    servers: list[dict[str, Any]] = []
    for raw_server in raw_servers:
        if isinstance(raw_server, McpServerConfig):
            server = raw_server
            sanitized_server = server.model_dump()
        elif isinstance(raw_server, dict):
            server = McpServerConfig(**raw_server)
            # Preserve permissive version-2 extension keys while replacing
            # the only sensitive reference with its redacting wrapper.
            sanitized_server = dict(raw_server)
            if server.auth is not None:
                raw_auth = raw_server["auth"]
                sanitized_auth = (
                    dict(raw_auth)
                    if isinstance(raw_auth, dict)
                    else server.auth.model_dump()
                )
                sanitized_auth["credential_ref"] = server.auth.credential_ref
                sanitized_server["auth"] = sanitized_auth
        else:
            raise McpConfigError("invalid MCP server configuration")
        servers.append(sanitized_server)
    sanitized_registry = dict(registry)
    sanitized_registry["mcp_servers"] = servers
    sanitized_data = dict(data)
    sanitized_data["registry"] = sanitized_registry
    return sanitized_data


class _SanitizedRegistryConfigModel(BaseModel):
    """Outer config boundary that redacts server references before validation."""

    @classmethod
    def _install_json_validation_boundary(cls) -> None:
        validator = cls.__pydantic_validator__
        if not isinstance(validator, _SanitizedJsonSchemaValidator):
            cls.__pydantic_validator__ = _SanitizedJsonSchemaValidator(  # type: ignore[assignment]
                validator, "invalid configuration document"
            )

    @classmethod
    def __pydantic_on_complete__(cls) -> None:
        cls._install_json_validation_boundary()

    @classmethod
    def model_rebuild(cls, **kwargs: Any) -> bool | None:
        rebuilt = super().model_rebuild(**kwargs)
        cls._install_json_validation_boundary()
        return rebuilt

    @classmethod
    def __get_pydantic_core_schema__(
        cls, source_type: Any, handler: GetCoreSchemaHandler
    ) -> core_schema.CoreSchema:
        schema = handler(source_type)
        return core_schema.no_info_wrap_validator_function(
            cls._sanitize_registry_for_validation, schema
        )

    @classmethod
    def _sanitize_registry_for_validation(
        cls, value: object, handler: core_schema.ValidatorFunctionWrapHandler
    ) -> object:
        if isinstance(value, cls):
            return handler(value)
        sanitized = _sanitize_registry_servers(value) if isinstance(value, dict) else value
        validation_failed = False
        try:
            validated = handler(sanitized)
        except ValidationError:
            validation_failed = True
        if validation_failed:
            raise McpConfigError("invalid configuration document")
        return validated


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


class GlobalConfig(_SanitizedRegistryConfigModel):
    """Schema for ~/.config/gearcore/config.yaml"""

    model_config = ConfigDict(hide_input_in_errors=True)

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
        for raw_server in self.registry.get("mcp_servers", []):
            McpServerConfig.model_validate(raw_server)
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


class ProjectConfig(_SanitizedRegistryConfigModel):
    """Schema for <project>/.gearcore/config.yaml"""

    model_config = ConfigDict(hide_input_in_errors=True)

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
        for raw_server in self.registry.get("mcp_servers", []):
            McpServerConfig.model_validate(raw_server)
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


@dataclass(frozen=True, slots=True)
class SkillBindingCeiling:
    """An immutable skill name-to-source binding enforced after launch."""

    name: str
    path: Path
    is_project_local: bool


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
        enforced_skill_bindings: frozenset[SkillBindingCeiling] | None = None,
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
        self._enforced_skill_bindings = enforced_skill_bindings
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
    def enforced_skill_bindings(self) -> frozenset[SkillBindingCeiling] | None:
        return self._enforced_skill_bindings

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
    def active_mcp_server_ids(self) -> tuple[str, ...]:
        return tuple(server.id for server in self._mcp_servers)

    @property
    def denied_mcp_server_ids(self) -> tuple[str, ...]:
        return self._mcp_capabilities.denied

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
        if self.diagnostic_only:
            return []
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

    def skill_binding_is_allowed(
        self, name: str, path: Path, is_project_local: bool
    ) -> bool:
        ceiling = self.enforced_skill_bindings
        if ceiling is None:
            return True
        return SkillBindingCeiling(name, path.resolve(), is_project_local) in ceiling

    def resolve_skill_capabilities(
        self,
        global_skills: tuple[str, ...],
        project_skills: tuple[str, ...],
    ) -> ResolvedCapabilities:
        project_include, project_deny = self._project_capability_rules("skills")
        resolved = resolve_capabilities(
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
        ceiling = self.enforced_skill_bindings
        if ceiling is None:
            return resolved
        allowed_names = {binding.name for binding in ceiling}
        return ResolvedCapabilities(
            active=tuple(
                name for name in resolved.active if name in allowed_names
            ),
            denied=resolved.denied,
            protected=resolved.protected,
            diagnostics=resolved.diagnostics,
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


class _UniqueKeyLoader(yaml.SafeLoader):
    """Safe YAML loader that rejects ambiguous duplicate mapping keys."""


def _construct_unique_mapping(
    loader: _UniqueKeyLoader, node: yaml.MappingNode, deep: bool = False
) -> dict[Any, Any]:
    loader.flatten_mapping(node)
    result: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in result
        except TypeError:
            raise yaml.constructor.ConstructorError(
                None,
                None,
                "unhashable mapping key",
                key_node.start_mark,
            ) from None
        if duplicate:
            raise yaml.constructor.ConstructorError(
                None,
                None,
                "duplicate mapping key",
                key_node.start_mark,
            )
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def _load_yaml(path: Path) -> dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.load(f, Loader=_UniqueKeyLoader) or {}
        logger.debug("Loaded config from %s", path)
        return data
    except FileNotFoundError:
        logger.debug("Config not found at %s, skipping", path)
        return {}
    except Exception:
        # Parser exceptions can contain source-line excerpts. Never include
        # those excerpts because malformed YAML may contain a plaintext secret.
        logger.error("Failed to parse config at %s", path)
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


def _load_project_config(
    project: Path | None,
) -> tuple[Path | None, ProjectConfig | None]:
    project_root = project.resolve() if project is not None else find_project_root()
    if project_root is None:
        return None, None

    p_file = project_root / PROJECT_CONFIG_NAME / "config.yaml"
    p_data = _load_yaml(p_file)
    if not p_data:
        logger.debug("No project config found at %s", p_file)
        return project_root, None

    project_cfg = ProjectConfig(**p_data)
    logger.info(
        "Project context: %s (%s)",
        project_cfg.context.name or project_root.name,
        project_root,
    )
    return project_root, project_cfg


def _approved_skill_binding_ceiling(
    candidate: EffectiveConfig, enforced: EffectiveConfig
) -> frozenset[SkillBindingCeiling] | None:
    """Approve resolved authority and return its persistent skill ceiling."""

    enforced_servers = {
        server.id: server.model_dump() for server in enforced.mcp_servers
    }
    for server in candidate.mcp_servers:
        if enforced_servers.get(server.id) != server.model_dump():
            return None

    # Skill names and winning global/project bindings require discovery. Import
    # lazily to avoid the SkillManager -> EffectiveConfig module cycle.
    from gearcore_hub.skill_manager import SkillManager

    candidate_skills = SkillManager(candidate)
    enforced_skills = SkillManager(enforced)
    candidate_names = candidate_skills.visible_skill_names
    enforced_names = enforced_skills.visible_skill_names
    if not candidate_names.issubset(enforced_names):
        return None
    for name in candidate_names:
        candidate_bundle = candidate_skills.skills.get(name)
        enforced_bundle = enforced_skills.skills.get(name)
        if candidate_bundle is None or enforced_bundle is None:
            return None
        if (
            candidate_bundle.path.resolve() != enforced_bundle.path.resolve()
            or candidate_bundle.is_project_local
            != enforced_bundle.is_project_local
        ):
            return None
    return frozenset(
        SkillBindingCeiling(
            name=name,
            path=enforced_skills.skills[name].path.resolve(),
            is_project_local=enforced_skills.skills[name].is_project_local,
        )
        for name in enforced_names
    )


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

        def diagnostic(
            code: str,
            *,
            profile_source: str = "invalid-envelope",
            enforced_profile_name: str | None = None,
        ) -> EffectiveConfig:
            return EffectiveConfig(
                global_cfg,
                None,
                None,
                profile_source=profile_source,
                enforced_profile_name=enforced_profile_name,
                diagnostic_code=code,
            )

        envelope_is_blank = (
            isinstance(context_envelope, str) and not context_envelope.strip()
        )
        key_is_blank = (
            isinstance(envelope_public_key, str)
            and not envelope_public_key.strip()
        )
        if envelope_is_blank or key_is_blank:
            return diagnostic(INVALID_ENVELOPE_DIAGNOSTIC)
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

        # The signature and issuer/profile constraints are known-valid before
        # any cwd discovery or project YAML read/log occurs.
        project_root, project_cfg = _load_project_config(project)

        enforced_profile = profiles.entries[verified.profile]
        selected_profile_name = profile_name or verified.profile
        selected_profile = profiles.entries.get(selected_profile_name)
        candidate_overlay: ProfileConfig | None = None
        enforced_overlay: ProfileConfig | None = None
        if (
            project_cfg is not None
            and project_cfg.version == 3
            and project_cfg.profiles is not None
        ):
            candidate_overlay = project_cfg.profiles.entries.get(
                selected_profile_name
            )
            enforced_overlay = project_cfg.profiles.entries.get(verified.profile)
        if selected_profile is None or not profile_is_subset(
            selected_profile,
            enforced_profile,
            candidate_overlay=candidate_overlay,
            enforced_overlay=enforced_overlay,
        ):
            return diagnostic(
                ENVELOPE_EXPANSION_DIAGNOSTIC,
                profile_source="envelope",
                enforced_profile_name=verified.profile,
            )
        candidate_effective = EffectiveConfig(
            global_cfg,
            project_cfg,
            project_root,
            profile_name=selected_profile_name,
            profile_source="envelope",
            enforced_profile_name=verified.profile,
        )
        if selected_profile_name != verified.profile:
            enforced_effective = EffectiveConfig(
                global_cfg,
                project_cfg,
                project_root,
                profile_name=verified.profile,
                profile_source="envelope",
                enforced_profile_name=verified.profile,
            )
            skill_ceiling = _approved_skill_binding_ceiling(
                candidate_effective, enforced_effective
            )
            if skill_ceiling is None:
                return diagnostic(
                    ENVELOPE_EXPANSION_DIAGNOSTIC,
                    profile_source="envelope",
                    enforced_profile_name=verified.profile,
                )
            return EffectiveConfig(
                global_cfg,
                project_cfg,
                project_root,
                profile_name=selected_profile_name,
                profile_source="envelope",
                enforced_profile_name=verified.profile,
                enforced_skill_bindings=skill_ceiling,
            )
        return candidate_effective

    project_root, project_cfg = _load_project_config(project)
    return EffectiveConfig(
        global_cfg, project_cfg, project_root, profile_name=profile_name
    )
