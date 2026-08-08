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
import unicodedata
from collections.abc import Callable, Generator, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Self, cast
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
        assignment_handler: Callable[[Any, str, object], Any] | None = None,
    ) -> None:
        self._validator = validator
        self._error_message = error_message
        self._assignment_preflight = assignment_handler

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
            return self._assignment_preflight(instance, field_name, value)
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


def _validated_raw_state(model: BaseModel, error_message: str) -> dict[str, Any]:
    """Copy model state while rejecting keys injected outside Pydantic."""

    state = object.__getattribute__(model, "__dict__")
    if set(state).difference(type(model).model_fields):
        raise McpConfigError(error_message)
    extras = object.__getattribute__(model, "__pydantic_extra__")
    if extras:
        raise McpConfigError(error_message)
    return cast(dict[str, Any], state.copy())


def _restore_validated_model(
    model_type: type[BaseModel], payload: dict[str, Any], fields_set: set[str]
) -> BaseModel:
    restored = model_type.model_validate(payload)
    object.__setattr__(restored, "__pydantic_fields_set__", fields_set.copy())
    return restored


class _SafeModelSurfaces:
    """Validate every normal surface that can move model state downstream.

    Raw ``vars()``/``object.__setattr__`` writes require arbitrary in-process
    Python execution and are intentionally not treated as a containment
    boundary. Server authentication and process isolation own that threat.
    Standard model surfaces still reject or normalize such state before use.
    """

    def _validated_state(self) -> Self:
        raise NotImplementedError

    def _detached_validated_state(self) -> Self:
        candidate = self._validated_state()
        detached = type(self).model_validate(  # type: ignore[attr-defined]
            candidate.model_dump(round_trip=True)  # type: ignore[attr-defined]
        )
        fields_set = object.__getattribute__(self, "__pydantic_fields_set__")
        object.__setattr__(
            detached,
            "__pydantic_fields_set__",
            set(fields_set).intersection(type(self).model_fields),  # type: ignore[attr-defined]
        )
        return detached  # type: ignore[no-any-return]

    def __iter__(self) -> Generator[tuple[str, Any]]:
        yield from self.model_dump().items()  # type: ignore[attr-defined]

    def __str__(self) -> str:
        try:
            candidate = self._validated_state()
        except McpConfigError:
            return f"{type(self).__name__}(<invalid configuration>)"
        return BaseModel.__str__(candidate)  # type: ignore[arg-type]

    def __format__(self, format_spec: str) -> str:
        return format(str(self), format_spec)

    def __getstate__(self) -> dict[str, Any]:
        candidate = self._detached_validated_state()
        state = BaseModel.__getstate__(candidate)  # type: ignore[arg-type]
        state["__dict__"] = state["__dict__"].copy()
        state["__pydantic_fields_set__"] = state[
            "__pydantic_fields_set__"
        ].copy()
        if state["__pydantic_extra__"] is not None:
            state["__pydantic_extra__"] = state["__pydantic_extra__"].copy()
        if state["__pydantic_private__"] is not None:
            state["__pydantic_private__"] = state["__pydantic_private__"].copy()
        return state

    def __copy__(self) -> Self:
        return self._detached_validated_state()

    def __deepcopy__(self, memo: dict[int, Any] | None = None) -> Self:
        return self._detached_validated_state()

    def __reduce__(self) -> tuple[Any, tuple[Any, ...]]:
        candidate = self._detached_validated_state()
        return (
            _restore_validated_model,
            (
                type(self),
                candidate.model_dump(round_trip=True),  # type: ignore[attr-defined]
                candidate.model_fields_set.copy(),  # type: ignore[attr-defined]
            ),
        )


class _FrozenMapping(tuple[tuple[str, Any], ...], Mapping[str, Any]):
    """Attribute-less mapping whose complete payload is immutable tuple state."""

    __slots__ = ()

    def __new__(
        cls, items: tuple[tuple[str, Any], ...]
    ) -> _FrozenMapping:
        return tuple.__new__(cls, items)

    def __getitem__(self, key: object) -> Any:  # type: ignore[override]
        for candidate, value in tuple.__iter__(self):
            if candidate == key:
                return value
        raise KeyError(key)

    def __iter__(self) -> Iterator[str]:  # type: ignore[override]
        return (key for key, _ in tuple.__iter__(self))

    def __contains__(self, key: object) -> bool:
        return any(candidate == key for candidate, _ in tuple.__iter__(self))

    def items(self) -> Iterator[tuple[str, Any]]:  # type: ignore[override]
        return tuple.__iter__(self)

    def values(self) -> Iterator[Any]:  # type: ignore[override]
        return (value for _, value in tuple.__iter__(self))

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Mapping) or len(self) != len(other):
            return False
        try:
            return all(
                key in other and _config_values_equal(value, other[key])
                for key, value in tuple.__iter__(self)
            )
        except (KeyError, TypeError):
            return False

    def __hash__(self) -> int:
        return hash(frozenset(tuple.__iter__(self)))

    def __repr__(self) -> str:
        return repr(_thaw_config_value(self))

    def __copy__(self) -> Self:
        return self

    def __deepcopy__(self, memo: dict[int, Any]) -> Self:
        return self

    def __reduce__(self) -> tuple[type[Self], tuple[tuple[tuple[str, Any], ...]]]:
        return type(self), (tuple(tuple.__iter__(self)),)


_JSON_SCALAR_TYPES = (str, int, float, bool, type(None))
_MAX_CONFIG_NESTING = 64


def _config_values_equal(
    left: object,
    right: object,
    *,
    depth: int = 0,
    seen: set[tuple[int, int]] | None = None,
) -> bool:
    """Compare immutable snapshots to their ordinary JSON/YAML equivalents."""

    if left is right:
        return True
    if depth > _MAX_CONFIG_NESTING:
        return False
    if seen is None:
        seen = set()
    pair = (id(left), id(right))
    if pair in seen:
        return True
    if isinstance(left, Mapping) and isinstance(right, Mapping):
        if len(left) != len(right):
            return False
        seen.add(pair)
        try:
            return all(
                key in right
                and _config_values_equal(
                    value,
                    right[key],
                    depth=depth + 1,
                    seen=seen,
                )
                for key, value in left.items()
            )
        finally:
            seen.remove(pair)
    if isinstance(left, tuple) and type(right) in (list, tuple):
        right_sequence = cast(list[object] | tuple[object, ...], right)
        if len(left) != len(right_sequence):
            return False
        seen.add(pair)
        try:
            return all(
                _config_values_equal(
                    left_value,
                    right_value,
                    depth=depth + 1,
                    seen=seen,
                )
                for left_value, right_value in zip(
                    left, right_sequence, strict=True
                )
            )
        finally:
            seen.remove(pair)
    return left == right


def _freeze_config_value(
    value: Any, *, depth: int = 0, active: set[int] | None = None
) -> Any:
    if depth > _MAX_CONFIG_NESTING:
        raise McpConfigError("MCP configuration nesting limit exceeded")
    if isinstance(value, _FrozenMapping):
        return value
    if active is None:
        active = set()
    if isinstance(value, Mapping):
        identity = id(value)
        if identity in active:
            raise McpConfigError("cyclic MCP configuration value")
        active.add(identity)
        try:
            items: list[tuple[str, Any]] = []
            for key, nested in value.items():
                if type(key) is not str:
                    raise McpConfigError("invalid MCP configuration mapping key")
                items.append(
                    (
                        key,
                        _freeze_config_value(
                            nested, depth=depth + 1, active=active
                        ),
                    )
                )
            return _FrozenMapping(tuple(items))
        finally:
            active.remove(identity)
    if type(value) in (list, tuple):
        iterable_value = cast(list[object] | tuple[object, ...], value)
        identity = id(value)
        if identity in active:
            raise McpConfigError("cyclic MCP configuration value")
        active.add(identity)
        try:
            return tuple(
                _freeze_config_value(nested, depth=depth + 1, active=active)
                for nested in iterable_value
            )
        finally:
            active.remove(identity)
    if type(value) in _JSON_SCALAR_TYPES or isinstance(value, SecretStr):
        return value
    raise McpConfigError("unsupported MCP configuration value")


def _thaw_config_value(value: Any) -> Any:
    if isinstance(value, _FrozenMapping):
        return {key: _thaw_config_value(nested) for key, nested in value.items()}
    if isinstance(value, tuple):
        return [_thaw_config_value(nested) for nested in value]
    return value


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

    if isinstance(value, Mapping):
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
    if not isinstance(value, Mapping):
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


def _normalize_auth_input(value: object) -> object:
    return dict(value) if isinstance(value, Mapping) else value


def _materialize_nested_mappings(
    value: object, *, depth: int = 0, active: set[int] | None = None
) -> object:
    if depth > _MAX_CONFIG_NESTING:
        raise McpConfigError("MCP configuration nesting limit exceeded")
    if active is None:
        active = set()
    if isinstance(value, Mapping):
        identity = id(value)
        if identity in active:
            raise McpConfigError("cyclic MCP configuration value")
        active.add(identity)
        try:
            materialized: dict[str, object] = {}
            for key, nested in value.items():
                if type(key) is not str:
                    raise McpConfigError("invalid MCP configuration mapping key")
                materialized[key] = _materialize_nested_mappings(
                    nested, depth=depth + 1, active=active
                )
            return materialized
        finally:
            active.remove(identity)
    if type(value) in (list, tuple):
        iterable_value = cast(list[object] | tuple[object, ...], value)
        identity = id(value)
        if identity in active:
            raise McpConfigError("cyclic MCP configuration value")
        active.add(identity)
        try:
            materialized_items = [
                _materialize_nested_mappings(
                    nested, depth=depth + 1, active=active
                )
                for nested in iterable_value
            ]
            return materialized_items if type(value) is list else tuple(materialized_items)
        finally:
            active.remove(identity)
    if type(value) in _JSON_SCALAR_TYPES or isinstance(value, SecretStr):
        return value
    raise McpConfigError("unsupported MCP configuration value")


def _normalize_server_input(value: object) -> object:
    """Materialize supported repeatable carriers once before inspection."""

    if not isinstance(value, Mapping):
        return value
    if any(type(key) is not str for key in value):
        raise McpConfigError("invalid MCP server configuration")
    normalized = {
        key: nested._validated_state().model_dump(round_trip=True)
        if key == "auth" and isinstance(nested, McpAuthConfig)
        else _materialize_nested_mappings(nested)
        for key, nested in value.items()
    }
    environment = normalized.get("env")
    if environment is not None:
        if not isinstance(environment, Mapping):
            raise McpConfigError("invalid MCP server configuration")
        normalized["env"] = dict(environment)
    auth = normalized.get("auth")
    if isinstance(auth, Mapping):
        normalized["auth"] = dict(auth)
    return normalized


def _model_validation_payload(model: BaseModel) -> dict[str, Any]:
    """Return exactly the explicitly set model state for revalidation."""

    state = object.__getattribute__(model, "__dict__")
    payload = {
        field_name: state[field_name]
        for field_name in model.__pydantic_fields_set__
        if field_name in state
    }
    extras = model.__pydantic_extra__
    if extras:
        payload.update(extras)
    return payload


def _commit_validated_model(target: BaseModel, candidate: BaseModel) -> None:
    """Atomically replace model state only after full candidate validation."""

    object.__setattr__(target, "__dict__", candidate.__dict__.copy())
    object.__setattr__(
        target, "__pydantic_fields_set__", candidate.__pydantic_fields_set__.copy()
    )
    object.__setattr__(target, "__pydantic_extra__", candidate.__pydantic_extra__)


class _SanitizedConfigModel(_SafeModelSurfaces, BaseModel):
    """Core-schema boundary that replaces input-retaining errors."""

    @classmethod
    def _preflight_input(cls, value: object) -> None:
        raise NotImplementedError

    @classmethod
    def _validation_error_message(cls, value: object) -> str:
        return "invalid configuration document"

    @classmethod
    def _normalize_input(cls, value: object) -> object:
        return value

    @classmethod
    def _preflight_assignment(cls, field_name: str, value: object) -> None: ...

    @classmethod
    def _normalize_assignment_value(
        cls, field_name: str, value: object
    ) -> object:
        return value

    @classmethod
    def _validated_assignment(
        cls, instance: Self, field_name: str, value: object
    ) -> Self:
        normalized_value = cls._normalize_assignment_value(field_name, value)
        cls._preflight_assignment(field_name, normalized_value)
        payload = _model_validation_payload(instance)
        payload[field_name] = normalized_value
        candidate = cls.model_validate(payload)
        _commit_validated_model(instance, candidate)
        return instance

    @classmethod
    def _install_json_validation_boundary(cls) -> None:
        validator = cls.__pydantic_validator__
        if not isinstance(validator, _SanitizedJsonSchemaValidator):
            cls.__pydantic_validator__ = _SanitizedJsonSchemaValidator(  # type: ignore[assignment]
                validator,
                cls._validation_error_message(None),
                cls._validated_assignment,
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
        type(self)._validated_assignment(self, field_name, value)

    def model_copy(
        self, *, update: Mapping[str, Any] | None = None, deep: bool = False
    ) -> Self:
        if update is None:
            source = self._detached_validated_state()
            return BaseModel.model_copy(source, deep=deep)
        source = self._detached_validated_state()
        payload = _model_validation_payload(source)
        payload.update(dict(update))
        return type(self).model_validate(payload)

    @classmethod
    def model_construct(
        cls, _fields_set: set[str] | None = None, **values: Any
    ) -> Self:
        validated = cls.model_validate(values)
        if _fields_set is not None:
            object.__setattr__(
                validated, "__pydantic_fields_set__", set(_fields_set)
            )
        return validated

    @classmethod
    def __get_pydantic_core_schema__(
        cls, source_type: Any, handler: GetCoreSchemaHandler
    ) -> core_schema.CoreSchema:
        schema = handler(source_type)
        return core_schema.no_info_wrap_validator_function(
            cls._validate_without_retaining_input,
            schema,
            serialization=core_schema.wrap_serializer_function_ser_schema(
                cls._serialize_validated_state
            ),
        )

    @classmethod
    def _serialize_validated_state(
        cls,
        value: object,
        handler: core_schema.SerializerFunctionWrapHandler,
    ) -> object:
        candidate = value._validated_state() if isinstance(value, cls) else value
        return handler(candidate)

    @classmethod
    def _validate_without_retaining_input(
        cls, value: object, handler: core_schema.ValidatorFunctionWrapHandler
    ) -> object:
        if isinstance(value, cls):
            value = _validated_raw_state(
                value._validated_state(), cls._validation_error_message(value)
            )
        normalized = cls._normalize_input(value)
        cls._preflight_input(normalized)
        validation_failed = False
        try:
            validated = handler(normalized)
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

    def __getattribute__(self, field_name: str) -> Any:
        value = super().__getattribute__(field_name)
        if field_name == "credential_ref" and not isinstance(value, SecretStr):
            raise McpConfigError("invalid MCP authentication configuration")
        return value

    @classmethod
    def _preflight_input(cls, value: object) -> None:
        _preflight_auth_input(value)

    @classmethod
    def _normalize_input(cls, value: object) -> object:
        return _normalize_auth_input(value)

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

    def _validated_state(self) -> McpAuthConfig:
        state = _validated_raw_state(
            self, "invalid MCP authentication configuration"
        )
        credential = state.get("credential_ref")
        environment = state.get("stdio_environment")
        scheme = state.get("http_scheme")
        if (
            not isinstance(credential, SecretStr)
            or not isinstance(environment, str)
            or not isinstance(scheme, str)
        ):
            raise McpConfigError("invalid MCP authentication configuration")
        return type(self).model_validate(
            {
                "credential_ref": credential,
                "stdio_environment": environment,
                "http_scheme": scheme,
            }
        )

    def __repr_args__(self) -> Any:
        try:
            validated = self._validated_state()
        except McpConfigError:
            return [("configuration", "<invalid>")]
        return [
            ("credential_ref", SecretStr("redacted")),
            ("stdio_environment", validated.stdio_environment),
            ("http_scheme", validated.http_scheme),
        ]

    def model_dump(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return super().model_dump(*args, **kwargs)

    def model_dump_json(self, *args: Any, **kwargs: Any) -> str:
        return super().model_dump_json(*args, **kwargs)

    def credential_id(self) -> str:
        """Return the identifier only at the credential lookup boundary."""

        return self._validated_state().credential_ref.get_secret_value()


def _preflight_server_input(value: object) -> None:
    if not isinstance(value, Mapping):
        raise McpConfigError("invalid MCP server configuration")
    args = value.get("args")
    if args is not None and not isinstance(args, (list, tuple)):
        raise McpConfigError("invalid MCP server configuration")
    auth_value = value.get("auth")
    if isinstance(auth_value, McpAuthConfig):
        auth_value._validated_state()
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
    if TYPE_CHECKING:
        args: list[str]
    else:
        args: tuple[str, ...] = Field(default_factory=tuple)
    url: str = ""
    if TYPE_CHECKING:
        env: dict[str, str] | None
    else:
        env: Mapping[str, str] | None = None
    auth: McpAuthConfig | None = None
    enabled: bool = True

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        if (
            not value.strip()
            or len(value) > 255
            or any(
                unicodedata.category(character) in {"Cc", "Cf", "Zl", "Zp"}
                for character in value
            )
        ):
            raise ValueError("invalid MCP server id")
        return value

    def __getattribute__(self, field_name: str) -> Any:
        value = super().__getattribute__(field_name)
        if field_name == "args":
            if not isinstance(value, tuple):
                raise McpConfigError("invalid MCP server configuration")
            return list(value)
        if field_name == "env":
            if value is None:
                return None
            if not isinstance(value, _FrozenMapping):
                raise McpConfigError("invalid MCP server configuration")
            return _thaw_config_value(value)
        return value

    def _validated_state(self) -> McpServerConfig:
        state = _validated_raw_state(self, "invalid MCP server configuration")
        return type(self).model_validate(state)

    def __repr__(self) -> str:
        try:
            candidate = self._validated_state()
        except McpConfigError:
            return "McpServerConfig(<invalid configuration>)"
        return BaseModel.__repr__(candidate)

    def model_dump(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return super().model_dump(*args, **kwargs)

    def model_dump_json(self, *args: Any, **kwargs: Any) -> str:
        return super().model_dump_json(*args, **kwargs)

    @classmethod
    def _preflight_input(cls, value: object) -> None:
        _preflight_server_input(value)

    @classmethod
    def _normalize_input(cls, value: object) -> object:
        return _normalize_server_input(value)

    @classmethod
    def _validation_error_message(cls, value: object) -> str:
        if isinstance(value, dict) and value.get("auth") is not None:
            return "invalid MCP authentication configuration"
        return "invalid MCP server configuration"

    @classmethod
    def _preflight_assignment(cls, field_name: str, value: object) -> None:
        _preflight_server_input({field_name: value})

    @classmethod
    def _normalize_assignment_value(
        cls, field_name: str, value: object
    ) -> object:
        normalized = _normalize_server_input({field_name: value})
        if isinstance(normalized, Mapping):
            return normalized[field_name]
        return value

    @field_validator("args", mode="after")
    @classmethod
    def snapshot_args(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(value)

    @field_validator("env", mode="after")
    @classmethod
    def snapshot_environment(
        cls, value: Mapping[str, str] | None
    ) -> Mapping[str, str] | None:
        return None if value is None else _freeze_config_value(value)

    @field_serializer("args")
    def serialize_args(self, value: tuple[str, ...]) -> list[str]:
        return list(value)

    @field_serializer("env")
    def serialize_environment(
        self, value: Mapping[str, str] | None
    ) -> dict[str, str] | None:
        return None if value is None else _thaw_config_value(value)

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


def _sanitize_registry_servers(data: Mapping[str, Any]) -> dict[str, Any]:
    """Validate server entries before outer Pydantic models can retain input."""

    if any(type(key) is not str for key in data):
        raise McpConfigError("invalid configuration document")
    sanitized_data = dict(data)
    registry = data.get("registry")
    if not isinstance(registry, Mapping):
        return sanitized_data
    sanitized_registry = dict(registry)
    sanitized_data["registry"] = sanitized_registry
    if "mcp_servers" not in registry:
        return sanitized_data
    raw_servers = registry["mcp_servers"]
    if not isinstance(raw_servers, (list, tuple)):
        raise McpConfigError("invalid MCP server configuration")
    servers: list[dict[str, Any]] = []
    server_ids: set[str] = set()
    duplicate_id = False
    for raw_server in raw_servers:
        if isinstance(raw_server, McpServerConfig):
            server = raw_server._validated_state()
            sanitized_server = server.model_dump()
        elif isinstance(raw_server, Mapping):
            materialized_server = _normalize_server_input(raw_server)
            if not isinstance(materialized_server, dict):
                raise McpConfigError("invalid MCP server configuration")
            server = McpServerConfig(**materialized_server)
            # Preserve permissive version-2 extension keys while replacing
            # the only sensitive reference with its redacting wrapper.
            sanitized_server = materialized_server
            validated_server = server.model_dump()
            for field_name in McpServerConfig.model_fields:
                if field_name in sanitized_server:
                    sanitized_server[field_name] = validated_server[field_name]
            if server.auth is not None:
                raw_auth = materialized_server["auth"]
                sanitized_auth = (
                    dict(raw_auth)
                    if isinstance(raw_auth, Mapping)
                    else server.auth.model_dump()
                )
                sanitized_auth["credential_ref"] = server.auth.credential_ref
                sanitized_server["auth"] = sanitized_auth
        else:
            raise McpConfigError("invalid MCP server configuration")
        if server.id in server_ids:
            duplicate_id = True
        server_ids.add(server.id)
        servers.append(sanitized_server)
    if duplicate_id:
        raise McpConfigError("invalid MCP server configuration")
    sanitized_registry["mcp_servers"] = servers
    return sanitized_data


class _SanitizedRegistryConfigModel(_SafeModelSurfaces, BaseModel):
    """Outer config boundary that redacts server references before validation."""

    def __getattribute__(self, field_name: str) -> Any:
        value = super().__getattribute__(field_name)
        if field_name == "registry":
            if not isinstance(value, _FrozenMapping):
                raise McpConfigError("invalid configuration document")
            return _thaw_config_value(value)
        return value

    @classmethod
    def _validated_assignment(
        cls, instance: Self, field_name: str, value: object
    ) -> Self:
        payload = _model_validation_payload(instance)
        payload[field_name] = value
        candidate = cls.model_validate(payload)
        _commit_validated_model(instance, candidate)
        return instance

    def __setattr__(self, field_name: str, value: Any) -> None:
        type(self)._validated_assignment(self, field_name, value)

    def _validated_state(self) -> Self:
        state = _validated_raw_state(self, "invalid configuration document")
        return type(self).model_validate(state)

    def __repr__(self) -> str:
        try:
            candidate = self._validated_state()
        except McpConfigError:
            return f"{type(self).__name__}(<invalid configuration>)"
        return BaseModel.__repr__(candidate)

    def model_dump(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return super().model_dump(*args, **kwargs)

    def model_dump_json(self, *args: Any, **kwargs: Any) -> str:
        return super().model_dump_json(*args, **kwargs)

    @field_validator("registry", mode="after", check_fields=False)
    @classmethod
    def snapshot_registry(cls, value: Mapping[str, Any]) -> Mapping[str, Any]:
        return cast(Mapping[str, Any], _freeze_config_value(value))

    @field_serializer("registry", check_fields=False)
    def serialize_registry(self, value: Mapping[str, Any]) -> dict[str, Any]:
        return cast(dict[str, Any], _thaw_config_value(value))

    @classmethod
    def _install_json_validation_boundary(cls) -> None:
        validator = cls.__pydantic_validator__
        if not isinstance(validator, _SanitizedJsonSchemaValidator):
            cls.__pydantic_validator__ = _SanitizedJsonSchemaValidator(  # type: ignore[assignment]
                validator,
                "invalid configuration document",
                cls._validated_assignment,
            )

    @classmethod
    def __pydantic_on_complete__(cls) -> None:
        cls._install_json_validation_boundary()

    @classmethod
    def model_rebuild(cls, **kwargs: Any) -> bool | None:
        rebuilt = super().model_rebuild(**kwargs)
        cls._install_json_validation_boundary()
        return rebuilt

    def model_copy(
        self, *, update: Mapping[str, Any] | None = None, deep: bool = False
    ) -> Self:
        if update is None:
            source = self._detached_validated_state()
            return BaseModel.model_copy(source, deep=deep)
        source = self._detached_validated_state()
        payload = _model_validation_payload(source)
        payload.update(dict(update))
        return type(self).model_validate(payload)

    @classmethod
    def model_construct(
        cls, _fields_set: set[str] | None = None, **values: Any
    ) -> Self:
        validated = cls.model_validate(values)
        if _fields_set is not None:
            object.__setattr__(
                validated, "__pydantic_fields_set__", set(_fields_set)
            )
        return validated

    @classmethod
    def __get_pydantic_core_schema__(
        cls, source_type: Any, handler: GetCoreSchemaHandler
    ) -> core_schema.CoreSchema:
        schema = handler(source_type)
        return core_schema.no_info_wrap_validator_function(
            cls._sanitize_registry_for_validation,
            schema,
            serialization=core_schema.wrap_serializer_function_ser_schema(
                cls._serialize_validated_state
            ),
        )

    @classmethod
    def _serialize_validated_state(
        cls,
        value: object,
        handler: core_schema.SerializerFunctionWrapHandler,
    ) -> object:
        candidate = value._validated_state() if isinstance(value, cls) else value
        return handler(candidate)

    @classmethod
    def _sanitize_registry_for_validation(
        cls, value: object, handler: core_schema.ValidatorFunctionWrapHandler
    ) -> object:
        if isinstance(value, cls):
            value = _validated_raw_state(
                value._validated_state(), "invalid configuration document"
            )
        sanitized = (
            _sanitize_registry_servers(value) if isinstance(value, Mapping) else value
        )
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
    if TYPE_CHECKING:
        registry: dict[str, Any]
    else:
        registry: Mapping[str, Any] = Field(
            default_factory=dict, validate_default=True
        )
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
    if TYPE_CHECKING:
        registry: dict[str, Any]
    else:
        registry: Mapping[str, Any] = Field(
            default_factory=dict, validate_default=True
        )  # project-local defs
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
        validated_global = global_cfg._validated_state()
        global_cfg = GlobalConfig.model_validate(
            validated_global.model_dump(round_trip=True)
        )
        project_cfg = (
            None
            if project_cfg is None
            else ProjectConfig.model_validate(
                project_cfg._validated_state().model_dump(round_trip=True)
            )
        )
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
        global_servers = list(
            self._require_unique_mcp_servers(
                tuple(s for s in self.global_cfg.mcp_servers if s.enabled)
            )
        )
        project_servers = list(
            self._require_unique_mcp_servers(
                ()
                if self.project_cfg is None
                else tuple(s for s in self.project_cfg.mcp_servers if s.enabled)
            )
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
        return self._require_unique_mcp_servers(tuple(servers)), resolved

    @staticmethod
    def _require_unique_mcp_servers(
        servers: tuple[McpServerConfig, ...],
    ) -> tuple[McpServerConfig, ...]:
        """Validate a server sequence without selecting a first/last winner."""

        validated: list[McpServerConfig] = []
        ids: set[str] = set()
        duplicate_id = False
        for server in servers:
            if not isinstance(server, McpServerConfig):
                raise McpConfigError("invalid MCP server configuration")
            candidate = server._validated_state()
            if candidate.id in ids:
                duplicate_id = True
            ids.add(candidate.id)
            validated.append(candidate)
        if duplicate_id:
            raise McpConfigError("invalid MCP server configuration")
        return tuple(validated)

    def mcp_server(self, server_id: str) -> McpServerConfig | None:
        """Return one unambiguous effective server definition by ID."""

        for server in self._require_unique_mcp_servers(self._mcp_servers):
            if server.id == server_id:
                return server.model_copy(deep=True)
        return None

    @property
    def mcp_servers(self) -> list[McpServerConfig]:
        return [
            server.model_copy(deep=True)
            for server in self._require_unique_mcp_servers(self._mcp_servers)
        ]

    @property
    def active_mcp_server_ids(self) -> tuple[str, ...]:
        return tuple(
            server.id
            for server in self._require_unique_mcp_servers(self._mcp_servers)
        )

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
