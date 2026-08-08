"""Tests for the layered configuration loader."""

import copy
import json
import logging
import pickle
import warnings
from collections import UserDict, UserList, deque
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest
from pydantic import BaseModel, SecretStr, TypeAdapter
from pydantic_core import PydanticSerializationError

from gearcore_hub.config import (
    EffectiveConfig,
    GlobalConfig,
    McpAuthConfig,
    McpConfigError,
    McpServerConfig,
    ProjectConfig,
    _default_skills_dirs,
    load_config,
)
from gearcore_hub.credentials import CredentialStore
from gearcore_hub.main import cmd_status
from gearcore_hub.vendor import bundled_superpowers_dir

SENTINEL = "sentinel-plaintext-token-credential-149"


def _assert_exception_does_not_retain(exc: BaseException, sentinel: str) -> None:
    rendered: list[str] = []
    pending: list[object] = [exc]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        rendered.extend((str(current), repr(current)))
        if isinstance(current, BaseException):
            if current.__cause__ is not None:
                pending.append(current.__cause__)
            if current.__context__ is not None:
                pending.append(current.__context__)
            if hasattr(current, "errors"):
                errors = current.errors()
                rendered.append(repr(errors))
                pending.append(errors)
            if hasattr(current, "json"):
                rendered.append(current.json())
        elif isinstance(current, dict):
            pending.extend(current.keys())
            pending.extend(current.values())
        elif isinstance(current, (list, tuple, set)):
            pending.extend(current)
    assert sentinel not in "\n".join(rendered)


class TestGlobalConfig:
    def test_empty_config(self):
        cfg = GlobalConfig()
        assert cfg.version == 2
        assert cfg.mcp_servers == []
        assert cfg.skills_dirs == _default_skills_dirs()

    def test_mcp_servers_parsing(self):
        data = {
            "registry": {
                "mcp_servers": [
                    {"id": "fs", "type": "stdio", "command": "npx", "args": ["-y"]}
                ]
            }
        }
        cfg = GlobalConfig(**data)
        assert len(cfg.mcp_servers) == 1
        assert cfg.mcp_servers[0].id == "fs"
        assert cfg.mcp_servers[0].args == ["-y"]

    def test_disabled_server_filtered_in_effective(self):
        data = {
            "registry": {
                "mcp_servers": [
                    {"id": "fs", "type": "stdio", "command": "npx", "enabled": True},
                    {"id": "old", "type": "stdio", "command": "npx", "enabled": False},
                ]
            }
        }
        global_cfg = GlobalConfig(**data)
        effective = EffectiveConfig(global_cfg, None, None)
        assert len(effective.mcp_servers) == 1
        assert effective.mcp_servers[0].id == "fs"


@pytest.mark.parametrize("model_type", [GlobalConfig, ProjectConfig])
@pytest.mark.parametrize("carrier", ["raw", "typed"])
def test_registry_rejects_duplicate_server_ids_without_retaining_definitions(
    model_type, carrier: str, caplog
):
    command_sentinel = "secret-duplicate-command-sentinel"
    path_sentinel = "/secret/duplicate/path/sentinel"
    credential_sentinel = "secret-duplicate-credential-sentinel"
    first: dict[str, Any] | McpServerConfig = {
        "id": "duplicate-id",
        "type": "stdio",
        "command": command_sentinel,
        "args": [path_sentinel],
        "auth": {
            "credential_ref": credential_sentinel,
            "stdio_environment": "DUPLICATE_AUTH",
        },
    }
    second: dict[str, Any] | McpServerConfig = {
        "id": "duplicate-id",
        "type": "http",
        "url": "https://secret-duplicate-url.invalid/mcp",
        "auth": {
            "credential_ref": "opposing-duplicate-credential",
            "http_scheme": "bearer",
        },
    }
    if carrier == "typed":
        first = McpServerConfig.model_validate(first)
        second = McpServerConfig.model_validate(second)

    with pytest.raises(
        McpConfigError, match="invalid MCP server configuration"
    ) as exc_info:
        model_type.model_validate(
            {"version": 2, "registry": {"mcp_servers": [first, second]}}
        )

    rendered = f"{exc_info.value!s}\n{exc_info.value!r}\n{caplog.text}"
    for sentinel in (
        command_sentinel,
        path_sentinel,
        credential_sentinel,
        "secret-duplicate-url",
        "opposing-duplicate-credential",
        "duplicate-id",
    ):
        assert sentinel not in rendered
        _assert_exception_does_not_retain(exc_info.value, sentinel)


@pytest.mark.parametrize(
    "unsafe_id",
    [
        "",
        "   ",
        "line\nfeed",
        "carriage\rreturn",
        "tab\tseparated",
        "escape\x1bsequence",
        "nul\x00byte",
        "format\u200bcharacter",
        "line\u2028separator",
        "paragraph\u2029separator",
        "x" * 256,
    ],
)
def test_server_id_rejects_blank_control_format_and_oversized_values(
    unsafe_id: str, caplog
):
    with pytest.raises(McpConfigError) as exc_info:
        McpServerConfig(id=unsafe_id)

    rendered = f"{exc_info.value!s}\n{exc_info.value!r}\n{caplog.text}"
    if unsafe_id:
        assert unsafe_id not in rendered
    assert "invalid MCP server configuration" in rendered


@pytest.mark.parametrize(
    "server_id",
    [
        "visible space",
        " leading-and-trailing ",
        "日本語: worker, αβ",
        "x" * 255,
    ],
)
def test_server_id_preserves_visible_v2_identifiers(server_id: str):
    assert McpServerConfig(id=server_id).id == server_id


def test_server_id_assignment_is_atomic_and_sanitized(caplog):
    server = McpServerConfig(id="safe visible id")
    unsafe_id = "unsafe\nINJECTED_ASSIGNMENT"

    with pytest.raises(McpConfigError) as exc_info:
        server.id = unsafe_id

    rendered = f"{exc_info.value!s}\n{exc_info.value!r}\n{caplog.text}"
    assert unsafe_id not in rendered
    assert "INJECTED_ASSIGNMENT" not in rendered
    assert server.id == "safe visible id"


@pytest.mark.parametrize("model_type", [GlobalConfig, ProjectConfig])
@pytest.mark.parametrize("separator", ["\u2028", "\u2029"])
def test_outer_config_rejects_unicode_line_separator_server_ids_safely(
    model_type, separator: str, caplog
):
    unsafe_id = f"unsafe{separator}INJECTED_OUTER_LINE"

    with pytest.raises(McpConfigError) as exc_info:
        model_type.model_validate(
            {
                "version": 2,
                "registry": {
                    "mcp_servers": [{"id": unsafe_id, "command": "safe"}]
                },
            }
        )

    rendered = f"{exc_info.value!s}\n{exc_info.value!r}\n{caplog.text}"
    assert unsafe_id not in rendered
    assert "INJECTED_OUTER_LINE" not in rendered
    assert "invalid MCP server configuration" in rendered


@pytest.mark.parametrize("model_type", [GlobalConfig, ProjectConfig])
@pytest.mark.parametrize("carrier", ["raw", "typed"])
def test_duplicate_registry_assignment_is_atomic_and_sanitized(
    model_type, carrier: str, caplog
):
    config = model_type.model_validate(
        {
            "version": 2,
            "registry": {
                "mcp_servers": [{"id": "original", "command": "original"}]
            },
        }
    )
    before = config.model_dump(round_trip=True)
    command_sentinel = "secret-assignment-command-sentinel"
    path_sentinel = "/secret/assignment/path/sentinel"
    first: dict[str, Any] | McpServerConfig = {
        "id": "duplicate-assignment",
        "type": "stdio",
        "command": command_sentinel,
        "args": [path_sentinel],
    }
    second: dict[str, Any] | McpServerConfig = {
        "id": "duplicate-assignment",
        "type": "http",
        "url": "https://secret-assignment-url.invalid/mcp",
    }
    if carrier == "typed":
        first = McpServerConfig.model_validate(first)
        second = McpServerConfig.model_validate(second)

    with pytest.raises(McpConfigError) as exc_info:
        config.registry = {"mcp_servers": [first, second]}

    rendered = f"{exc_info.value!s}\n{exc_info.value!r}\n{caplog.text}"
    for sentinel in (
        "duplicate-assignment",
        command_sentinel,
        path_sentinel,
        "secret-assignment-url",
    ):
        assert sentinel not in rendered
        _assert_exception_does_not_retain(exc_info.value, sentinel)
    assert config.model_dump(round_trip=True) == before
    assert [server.id for server in config.mcp_servers] == ["original"]


def test_effective_and_process_boundaries_reject_corrupted_duplicate_ids():
    from gearcore_hub.process_manager import ProcessManager

    effective = EffectiveConfig(
        GlobalConfig(
            registry={
                "mcp_servers": [
                    {"id": "first", "command": "first"},
                    {"id": "second", "command": "second"},
                ]
            }
        ),
        None,
        None,
    )
    duplicate = (
        McpServerConfig(id="duplicate", type="stdio", command="first"),
        McpServerConfig(
            id="duplicate", type="http", url="https://opposing.invalid/mcp"
        ),
    )
    object.__setattr__(effective, "_mcp_servers", duplicate)

    with pytest.raises(McpConfigError, match="invalid MCP server configuration"):
        effective.mcp_server("duplicate")
    with pytest.raises(McpConfigError, match="invalid MCP server configuration"):
        ProcessManager(effective)


class TestMcpAuthConfig:
    def test_stdio_credential_reference_is_valid(self):
        server = McpServerConfig(
            id="dispatcher",
            type="stdio",
            command="hive-dispatcher",
            auth={
                "credential_ref": "hive-dispatcher-operator",
                "stdio_environment": "HIVE_DISPATCHER_CREDENTIAL",
            },
        )

        assert isinstance(server.auth, McpAuthConfig)
        assert server.auth.credential_id() == "hive-dispatcher-operator"
        assert server.auth.stdio_environment == "HIVE_DISPATCHER_CREDENTIAL"
        assert server.auth.http_scheme == ""

    def test_credential_reference_is_masked_but_has_explicit_lookup_accessor(self):
        server = McpServerConfig(
            id="dispatcher",
            type="stdio",
            auth={
                "credential_ref": SENTINEL,
                "stdio_environment": "HIVE_AUTH",
            },
        )

        assert server.auth is not None
        assert server.auth.credential_id() == SENTINEL
        rendered = "\n".join(
            (
                repr(server.auth),
                repr(server),
                repr(server.auth.model_dump()),
                repr(server.model_dump()),
                server.auth.model_dump_json(),
                server.model_dump_json(),
            )
        )
        assert SENTINEL not in rendered

    def test_prevalidated_auth_model_remains_composable(self):
        auth = McpAuthConfig(
            credential_ref=SENTINEL,
            stdio_environment="HIVE_AUTH",
        )

        server = McpServerConfig(id="dispatcher", type="stdio", auth=auth)
        global_config = GlobalConfig(
            registry={"mcp_servers": [{"id": "dispatcher", "auth": auth}]}
        )

        assert server.auth is not auth
        assert global_config.mcp_servers[0].auth is not None
        assert global_config.mcp_servers[0].auth.credential_id() == SENTINEL
        assert SENTINEL not in repr(global_config.model_dump())

    @pytest.mark.parametrize("transport", ["sse", "http"])
    def test_http_credential_reference_is_valid(self, transport: str):
        server = McpServerConfig(
            id="dispatcher",
            type=transport,
            url="http://127.0.0.1/mcp",
            auth={"credential_ref": "operator", "http_scheme": "bearer"},
        )

        assert server.auth is not None
        assert server.auth.http_scheme == "bearer"

    @pytest.mark.parametrize(
        ("transport", "auth"),
        [
            (
                "stdio",
                {"credential_ref": "operator", "http_scheme": "bearer"},
            ),
            (
                "stdio",
                {"credential_ref": "operator", "stdio_environment": ""},
            ),
            (
                "sse",
                {
                    "credential_ref": "operator",
                    "stdio_environment": "TOKEN",
                    "http_scheme": "bearer",
                },
            ),
            ("sse", {"credential_ref": "operator"}),
            ("http", {"credential_ref": "operator", "http_scheme": ""}),
            (
                "custom",
                {"credential_ref": "operator", "http_scheme": "bearer"},
            ),
        ],
    )
    def test_wrong_auth_transport_combinations_are_rejected(
        self, transport: str, auth: dict[str, str]
    ):
        with pytest.raises(McpConfigError, match="authentication"):
            McpServerConfig(id="dispatcher", type=transport, auth=auth)

    def test_unauthenticated_v2_server_remains_compatible(self):
        server = McpServerConfig(
            id="legacy", type="stdio", command="legacy", legacy_extension=True
        )

        assert server.auth is None
        assert server.command == "legacy"

    @pytest.mark.parametrize(
        "plaintext_field",
        [
            "token",
            "API_TOKEN",
            "api_key",
            "APIKEY",
            "secret",
            "client_secret",
            "password",
            "credential",
            "authorization",
            "Authorization_Header",
        ],
    )
    def test_plaintext_top_level_credentials_are_rejected_without_echo(
        self, plaintext_field: str
    ):
        with pytest.raises(McpConfigError) as exc_info:
            McpServerConfig(
                id="dispatcher",
                type="stdio",
                command="hive-dispatcher",
                **{plaintext_field: SENTINEL},
            )

        assert SENTINEL not in str(exc_info.value)
        assert SENTINEL not in repr(exc_info.value)
        _assert_exception_does_not_retain(exc_info.value, SENTINEL)

    @pytest.mark.parametrize("method", ["model_validate", "model_validate_json"])
    def test_alternate_model_validation_does_not_retain_rejected_input(
        self, method: str
    ):
        payload: object = {"id": "dispatcher", "API_TOKEN": SENTINEL}
        if method == "model_validate_json":
            payload = f'{{"id":"dispatcher","API_TOKEN":"{SENTINEL}"}}'

        with pytest.raises(McpConfigError) as exc_info:
            getattr(McpServerConfig, method)(payload)

        _assert_exception_does_not_retain(exc_info.value, SENTINEL)

    @pytest.mark.parametrize(
        ("model_type", "payload"),
        [
            (
                McpAuthConfig,
                {
                    "credential_ref": "operator",
                    "stdio_environment": "HIVE_AUTH",
                    "unexpected": SENTINEL,
                },
            ),
            (McpServerConfig, {"id": "legacy", "API_TOKEN": SENTINEL}),
            (
                GlobalConfig,
                {
                    "registry": {
                        "mcp_servers": [{"id": "legacy", "API_TOKEN": SENTINEL}]
                    }
                },
            ),
            (
                ProjectConfig,
                {
                    "registry": {
                        "mcp_servers": [{"id": "legacy", "API_TOKEN": SENTINEL}]
                    }
                },
            ),
        ],
    )
    @pytest.mark.parametrize("method", ["validate_python", "validate_json"])
    def test_type_adapter_uses_sentinel_free_core_schema_boundary(
        self, model_type, payload: dict[str, object], method: str
    ):
        adapter = TypeAdapter(model_type)
        value: object = payload if method == "validate_python" else json.dumps(payload)

        with pytest.raises(McpConfigError) as exc_info:
            getattr(adapter, method)(value)

        _assert_exception_does_not_retain(exc_info.value, SENTINEL)

    @pytest.mark.parametrize(
        "model_type",
        [McpAuthConfig, McpServerConfig, GlobalConfig, ProjectConfig],
    )
    @pytest.mark.parametrize("api", ["model", "type_adapter"])
    def test_malformed_json_does_not_retain_input(self, model_type, api: str):
        malformed = f'{{"API_TOKEN":"{SENTINEL}"'

        with pytest.raises(McpConfigError) as exc_info:
            if api == "model":
                model_type.model_validate_json(malformed)
            else:
                TypeAdapter(model_type).validate_json(malformed)

        _assert_exception_does_not_retain(exc_info.value, SENTINEL)

    @pytest.mark.parametrize(
        ("model_type", "valid_payload"),
        [
            (McpAuthConfig, {"credential_ref": "operator"}),
            (McpServerConfig, {"id": "legacy"}),
            (GlobalConfig, {}),
            (ProjectConfig, {}),
        ],
    )
    def test_json_sanitizer_survives_model_rebuild(
        self, model_type, valid_payload: dict[str, object]
    ):
        assert model_type.model_rebuild(force=True) is True
        adapter = TypeAdapter(model_type)

        validated = adapter.validate_json(json.dumps(valid_payload))
        assert isinstance(validated, model_type)
        assert isinstance(validated.model_dump_json(), str)
        assert isinstance(model_type.model_json_schema(), dict)

        malformed = f'{{"API_TOKEN":"{SENTINEL}"'
        with pytest.raises(McpConfigError) as exc_info:
            adapter.validate_json(malformed)

        _assert_exception_does_not_retain(exc_info.value, SENTINEL)

    def test_json_sanitizer_delegates_non_json_validator_methods(self):
        validator = McpServerConfig.__pydantic_validator__

        python_server = validator.validate_python({"id": "python"})
        strings_server = validator.validate_strings({"id": "strings"})
        assigned_server = validator.validate_assignment(
            python_server, "enabled", False
        )

        assert python_server.id == "python"
        assert strings_server.id == "strings"
        assert assigned_server.enabled is False

    @pytest.mark.parametrize(
        ("model_type", "payload"),
        [
            (GlobalConfig, {"registry": SENTINEL}),
            (GlobalConfig, {"disclosure": SENTINEL}),
            (ProjectConfig, {"registry": SENTINEL}),
            (ProjectConfig, {"scope": SENTINEL}),
        ],
    )
    @pytest.mark.parametrize(
        "api",
        ["constructor", "model_python", "adapter_python", "model_json", "adapter_json"],
    )
    def test_outer_structural_errors_do_not_retain_input(
        self, model_type, payload: dict[str, object], api: str
    ):
        with pytest.raises(McpConfigError) as exc_info:
            if api == "constructor":
                model_type(**payload)
            elif api == "model_python":
                model_type.model_validate(payload)
            elif api == "adapter_python":
                TypeAdapter(model_type).validate_python(payload)
            elif api == "model_json":
                model_type.model_validate_json(json.dumps(payload))
            else:
                TypeAdapter(model_type).validate_json(json.dumps(payload))

        _assert_exception_does_not_retain(exc_info.value, SENTINEL)

    @pytest.mark.parametrize("api", ["direct", "validator"])
    @pytest.mark.parametrize(
        ("field_name", "value"),
        [
            ("credential_ref", SENTINEL),
            ("stdio_environment", SENTINEL),
        ],
    )
    def test_frozen_auth_assignment_is_sanitized_and_atomic(
        self, field_name: str, value: str, api: str
    ):
        auth = McpAuthConfig(credential_ref="operator")
        before = auth.model_dump()

        with pytest.raises(McpConfigError) as exc_info:
            if api == "direct":
                setattr(auth, field_name, value)
            else:
                auth.__pydantic_validator__.validate_assignment(
                    auth, field_name, value
                )

        _assert_exception_does_not_retain(exc_info.value, SENTINEL)
        assert auth.model_dump() == before

    @pytest.mark.parametrize("api", ["direct", "validator"])
    @pytest.mark.parametrize(
        ("field_name", "value"),
        [
            ("auth", {"token": SENTINEL}),
            ("env", {"API_TOKEN": SENTINEL}),
            ("args", ["--token", SENTINEL]),
        ],
    )
    def test_server_assignment_is_sanitized_and_atomic(
        self, field_name: str, value: object, api: str
    ):
        server = McpServerConfig(
            id="legacy", command="legacy", args=["--mode", "safe"], env={"MODE": "safe"}
        )
        before = server.model_dump()

        with pytest.raises(McpConfigError) as exc_info:
            if api == "direct":
                setattr(server, field_name, value)
            else:
                server.__pydantic_validator__.validate_assignment(
                    server, field_name, value
                )

        _assert_exception_does_not_retain(exc_info.value, SENTINEL)
        assert server.model_dump() == before

    def test_server_benign_assignment_remains_supported(self):
        server = McpServerConfig(id="legacy")
        before_extra = server.model_extra

        server.enabled = False

        assert server.enabled is False
        assert server.model_fields_set == {"id", "enabled"}
        assert server.model_extra == before_extra

    def test_server_containers_cannot_be_mutated_in_place(self):
        input_args = ["--mode", "safe"]
        input_env = {"MODE": "safe"}
        server = McpServerConfig(
            id="legacy",
            args=input_args,
            env=input_env,
        )

        input_args.append(SENTINEL)
        input_env["API_TOKEN"] = SENTINEL
        args_copy = server.args
        args_copy.append(SENTINEL)
        assert server.env is not None
        env_copy = server.env
        env_copy["API_TOKEN"] = SENTINEL

        assert isinstance(server.args, list)
        assert isinstance(server.env, dict)
        assert server.args.copy() == ["--mode", "safe"]
        assert server.env.copy() == {"MODE": "safe"}
        assert SENTINEL not in server.args
        assert "API_TOKEN" not in server.env
        dumped = server.model_dump()
        assert isinstance(dumped["args"], list)
        assert isinstance(dumped["env"], dict)
        assert SENTINEL not in repr(server)

    @pytest.mark.parametrize("route", ["direct", "assignment", "global", "project"])
    @pytest.mark.parametrize(
        ("field_name", "value_factory"),
        [
            ("env", lambda: MappingProxyType({"API_TOKEN": SENTINEL})),
            ("env", lambda: UserDict({"API_TOKEN": SENTINEL})),
            (
                "metadata",
                lambda: MappingProxyType(
                    {"auth": UserDict({"token": SENTINEL})}
                ),
            ),
            ("args", lambda: deque(["--token", SENTINEL])),
            ("args", lambda: iter(["--token", SENTINEL])),
            ("args", lambda: {"--token", SENTINEL}),
        ],
    )
    def test_nonconcrete_plaintext_carriers_fail_closed(
        self, field_name: str, value_factory, route: str
    ):
        payload = {"id": "legacy", field_name: value_factory()}

        with pytest.raises(McpConfigError) as exc_info:
            if route == "direct":
                McpServerConfig(**payload)
            elif route == "assignment":
                server = McpServerConfig(id="legacy")
                setattr(server, field_name, payload[field_name])
            else:
                config_type = GlobalConfig if route == "global" else ProjectConfig
                config_type(registry={"mcp_servers": [payload]})

        _assert_exception_does_not_retain(exc_info.value, SENTINEL)

    @pytest.mark.parametrize(
        "environment",
        [
            MappingProxyType({"MODE": "safe"}),
            UserDict({"MODE": "safe"}),
        ],
    )
    def test_safe_mapping_environments_are_materialized_once(self, environment):
        server = McpServerConfig(id="legacy", env=environment)

        assert isinstance(server.env, dict)
        assert server.env == {"MODE": "safe"}

        server.env = environment
        assert server.env == {"MODE": "safe"}

        outer = GlobalConfig(
            registry={"mcp_servers": [{"id": "legacy", "env": environment}]}
        )
        assert outer.mcp_servers[0].env == {"MODE": "safe"}
        assert "MODE" in outer.model_dump_json()

    def test_tuple_arguments_keep_the_public_list_contract(self):
        server = McpServerConfig(id="legacy", args=("--mode", "safe"))

        assert isinstance(server.args, list)
        assert server.args == ["--mode", "safe"]

    @pytest.mark.parametrize(
        ("model_type", "instance", "unsafe_update"),
        [
            (
                McpAuthConfig,
                McpAuthConfig(credential_ref="operator"),
                {"unexpected": SENTINEL},
            ),
            (
                McpServerConfig,
                McpServerConfig(id="legacy"),
                {"API_TOKEN": SENTINEL},
            ),
            (GlobalConfig, GlobalConfig(), {"registry": SENTINEL}),
            (ProjectConfig, ProjectConfig(), {"scope": SENTINEL}),
        ],
    )
    @pytest.mark.parametrize("escape", ["model_copy", "model_construct"])
    def test_pydantic_unvalidated_escape_hatches_are_closed(
        self, model_type, instance, unsafe_update: dict[str, object], escape: str
    ):
        before_dump = instance.model_dump()
        before_repr = repr(instance)
        with (
            warnings.catch_warnings(record=True) as caught,
            pytest.raises(McpConfigError) as exc_info,
        ):
            if escape == "model_copy":
                instance.model_copy(update=unsafe_update)
            else:
                model_type.model_construct(**unsafe_update)

        _assert_exception_does_not_retain(exc_info.value, SENTINEL)
        assert SENTINEL not in repr(caught)
        assert instance.model_dump() == before_dump
        assert repr(instance) == before_repr

    @pytest.mark.parametrize(
        "model",
        [
            McpAuthConfig(credential_ref="operator"),
            McpServerConfig(id="legacy", args=["--mode", "safe"]),
            GlobalConfig(),
            ProjectConfig(),
        ],
    )
    def test_valid_model_copy_and_construct_remain_supported(self, model):
        with warnings.catch_warnings(record=True) as caught:
            shallow = model.model_copy()
            deep = model.model_copy(deep=True)
            rebuilt = type(model).model_construct(**model.model_dump())

        assert shallow == model
        assert deep == model
        assert rebuilt == model
        assert shallow.model_fields_set == model.model_fields_set
        assert deep.model_fields_set == model.model_fields_set
        assert shallow.model_extra == model.model_extra
        assert deep.model_extra == model.model_extra
        assert not caught

    @pytest.mark.parametrize("api", ["direct", "validator"])
    @pytest.mark.parametrize("change", ["transport", "bearer_auth"])
    def test_cross_field_assignment_is_transactional(self, api: str, change: str):
        server = McpServerConfig(
            id="dispatcher",
            type="stdio",
            auth={
                "credential_ref": "operator",
                "stdio_environment": "HIVE_AUTH",
            },
        )
        field_name = "type" if change == "transport" else "auth"
        value: object = "sse"
        if change == "bearer_auth":
            value = McpAuthConfig(
                credential_ref=SENTINEL,
                http_scheme="bearer",
            )
        before_dump = server.model_dump()
        before_repr = repr(server)
        before_fields_set = server.model_fields_set.copy()
        before_extra = server.model_extra

        with pytest.raises(McpConfigError) as exc_info:
            if api == "direct":
                setattr(server, field_name, value)
            else:
                server.__pydantic_validator__.validate_assignment(
                    server, field_name, value
                )

        _assert_exception_does_not_retain(exc_info.value, SENTINEL)
        assert server.model_dump() == before_dump
        assert repr(server) == before_repr
        assert server.model_fields_set == before_fields_set
        assert server.model_extra == before_extra

    def test_normal_state_apis_do_not_expose_mutable_server_backing(self):
        server = McpServerConfig(
            id="legacy", args=["--mode", "safe"], env={"MODE": "safe"}
        )
        shallow = copy.copy(server)
        deep = copy.deepcopy(server)
        round_tripped = pickle.loads(pickle.dumps(server))
        internal_surfaces = [
            vars(server),
            server.__getstate__()["__dict__"],
            vars(shallow),
            vars(deep),
            vars(round_tripped),
        ]

        for surface in internal_surfaces:
            assert not isinstance(surface["args"], list)
            assert not isinstance(surface["env"], dict)
        for surface in (dict(server), dict(shallow)):
            assert isinstance(surface["args"], list)
            assert isinstance(surface["env"], dict)
            surface["args"].append(SENTINEL)
            surface["env"]["API_TOKEN"] = SENTINEL

        server.args.append(SENTINEL)
        assert server.env is not None
        server.env["API_TOKEN"] = SENTINEL
        shallow.args.append(SENTINEL)

        rendered = repr(server) + repr(shallow) + repr(deep) + repr(round_tripped)
        rendered += repr(server.model_dump()) + server.model_dump_json()
        assert SENTINEL not in rendered
        assert server.args == ["--mode", "safe"]
        assert shallow.args == ["--mode", "safe"]
        assert deep.args == ["--mode", "safe"]
        assert round_tripped.args == ["--mode", "safe"]
        with warnings.catch_warnings(record=True) as caught:
            shallow.model_dump_json()
            deep.model_dump_json()
            round_tripped.model_dump_json()
        assert not caught

    def test_forcibly_corrupted_auth_is_never_trusted_or_rendered(self):
        auth = McpAuthConfig(credential_ref="operator")
        vars(auth)["credential_ref"] = SENTINEL

        assert SENTINEL not in repr(auth)
        for render in (auth.model_dump, auth.model_dump_json):
            with pytest.raises(
                (McpConfigError, PydanticSerializationError)
            ) as exc_info:
                render()
            _assert_exception_does_not_retain(exc_info.value, SENTINEL)
        with pytest.raises(McpConfigError) as compose_exc:
            McpServerConfig(id="legacy", auth=auth)
        _assert_exception_does_not_retain(compose_exc.value, SENTINEL)

    def test_auth_state_surfaces_keep_opaque_secret_references(self):
        auth = McpAuthConfig(credential_ref=SENTINEL)
        copies = [
            auth,
            copy.copy(auth),
            copy.deepcopy(auth),
            pickle.loads(pickle.dumps(auth)),
        ]

        for candidate in copies:
            for surface in (
                vars(candidate),
                dict(candidate),
                candidate.__getstate__()["__dict__"],
            ):
                reference = surface["credential_ref"]
                assert isinstance(reference, SecretStr)
                assert SENTINEL not in repr(reference)
            rendered = (
                repr(candidate)
                + repr(candidate.model_dump())
                + candidate.model_dump_json()
            )
            assert SENTINEL not in rendered
            assert candidate.credential_id() == SENTINEL

    @pytest.mark.parametrize("config_type", [GlobalConfig, ProjectConfig])
    @pytest.mark.parametrize(
        "carrier_factory",
        [
            lambda: deque([{"auth": {"token": SENTINEL}}]),
            lambda: UserList([{"auth": {"token": SENTINEL}}]),
            lambda: {SENTINEL},
            lambda: McpAuthConfig(credential_ref=SENTINEL),
        ],
    )
    def test_unsupported_legacy_extra_carriers_fail_closed(
        self, config_type, carrier_factory
    ):
        payload = {
            "id": "legacy",
            "metadata": carrier_factory(),
        }

        with pytest.raises(McpConfigError) as exc_info:
            config_type(registry={"mcp_servers": [payload]})

        _assert_exception_does_not_retain(exc_info.value, SENTINEL)

    @pytest.mark.parametrize("config_type", [GlobalConfig, ProjectConfig])
    def test_legacy_generator_carrier_is_rejected_without_consumption(
        self, config_type
    ):
        consumed: list[bool] = []

        def carrier():
            consumed.append(True)
            yield {"auth": {"token": SENTINEL}}

        with pytest.raises(McpConfigError) as exc_info:
            config_type(
                registry={
                    "mcp_servers": [{"id": "legacy", "metadata": carrier()}]
                }
            )

        _assert_exception_does_not_retain(exc_info.value, SENTINEL)
        assert consumed == []

    @pytest.mark.parametrize("config_type", [GlobalConfig, ProjectConfig])
    def test_outer_registry_state_is_immutable_and_defensive(self, config_type):
        config = config_type(
            registry={
                "mcp_servers": [
                    {
                        "id": "legacy",
                        "args": ["--mode", "safe"],
                        "metadata": {"nested": ["safe"]},
                    }
                ]
            }
        )
        before = config.model_dump()
        shallow = copy.copy(config)
        deep = copy.deepcopy(config)
        round_tripped = pickle.loads(pickle.dumps(config))

        public_registry = config.registry
        public_registry["mcp_servers"][0]["args"].append(SENTINEL)
        public_registry["mcp_servers"][0]["metadata"]["nested"].append(SENTINEL)

        for surface in (
            vars(config),
            config.__getstate__()["__dict__"],
            vars(shallow),
            vars(deep),
            vars(round_tripped),
        ):
            assert not isinstance(surface["registry"], dict)
            with pytest.raises(AttributeError):
                surface["registry"]._items = (("API_TOKEN", SENTINEL),)

        iterated_registry = dict(config)["registry"]
        assert isinstance(iterated_registry, dict)
        iterated_registry["mcp_servers"][0]["args"].append(SENTINEL)

        assert config.model_dump() == before
        assert shallow.model_dump() == before
        assert deep.model_dump() == before
        assert round_tripped.model_dump() == before
        assert round_tripped.model_fields_set == config.model_fields_set
        assert config.model_extra is None
        with warnings.catch_warnings(record=True) as caught:
            shallow.model_dump_json()
            deep.model_dump_json()
            round_tripped.model_dump_json()
        assert not caught

        with pytest.raises(McpConfigError) as direct_exc:
            config.registry = {"mcp_servers": [{"id": "x", "API_TOKEN": SENTINEL}]}
        _assert_exception_does_not_retain(direct_exc.value, SENTINEL)
        with pytest.raises(McpConfigError) as proxy_exc:
            config.__pydantic_validator__.validate_assignment(
                config, "registry", SENTINEL
            )
        _assert_exception_does_not_retain(proxy_exc.value, SENTINEL)
        assert config.model_dump() == before

        copied = config.model_copy()
        assert copied.model_dump() == before
        if config_type is GlobalConfig:
            effective = EffectiveConfig(config, None, None)
            assert effective.mcp_servers[0].args == ["--mode", "safe"]
            public_registry["mcp_servers"][0]["args"].append(SENTINEL)
            assert SENTINEL not in repr(effective.mcp_servers)

    @pytest.mark.parametrize(
        "model",
        [
            McpAuthConfig(credential_ref="operator"),
            McpServerConfig(id="legacy", args=["--mode", "safe"]),
            GlobalConfig(registry={"metadata": {"mode": "safe"}}),
            ProjectConfig(registry={"metadata": {"mode": "safe"}}),
        ],
    )
    def test_valid_raw_state_is_normalized_at_every_serialization_boundary(
        self, model
    ):
        # Deliberate raw-state writes model already-running arbitrary Python.
        # They are outside the hostile-process boundary; every normal consumer
        # must nevertheless revalidate and detach the resulting snapshot.
        if isinstance(model, McpAuthConfig):
            vars(model)["stdio_environment"] = "NEXT_ENV"
        elif isinstance(model, McpServerConfig):
            vars(model)["args"] = ["--mode", "next"]
        else:
            vars(model)["registry"] = {"metadata": {"mode": "next"}}

        adapter = TypeAdapter(type(model))
        with warnings.catch_warnings(record=True) as caught:
            dumped = model.model_dump()
            dumped_json = model.model_dump_json()
            adapter_dump = adapter.dump_python(model)
            adapter_json = adapter.dump_json(model)
            iterated = dict(model)
            state = model.__getstate__()
            copies = (copy.copy(model), copy.deepcopy(model))
            round_tripped = pickle.loads(pickle.dumps(model))

        assert not caught
        rendered = repr((dumped, dumped_json, adapter_dump, adapter_json, iterated))
        assert SENTINEL not in rendered
        assert all(copy_.model_dump() == dumped for copy_ in copies)
        assert round_tripped.model_dump() == dumped
        assert state["__dict__"] is not vars(model)

    @pytest.mark.parametrize(
        ("model", "corrupt"),
        [
            (
                McpAuthConfig(credential_ref="operator"),
                lambda model: vars(model).__setitem__("credential_ref", SENTINEL),
            ),
            (
                McpServerConfig(id="legacy"),
                lambda model: vars(model).__setitem__(
                    "args", ["--token", SENTINEL]
                ),
            ),
            (
                GlobalConfig(),
                lambda model: vars(model).__setitem__(
                    "registry",
                    {"mcp_servers": [{"id": "x", "API_TOKEN": SENTINEL}]},
                ),
            ),
            (
                ProjectConfig(),
                lambda model: vars(model).__setitem__("unexpected", SENTINEL),
            ),
            (
                ProjectConfig(),
                lambda model: object.__setattr__(
                    model,
                    "__pydantic_extra__",
                    {"authorization": SENTINEL},
                ),
            ),
        ],
    )
    def test_invalid_raw_state_never_crosses_standard_boundaries(
        self, model, corrupt
    ):
        corrupt(model)
        assert SENTINEL not in repr(model)
        adapter = TypeAdapter(type(model))
        consumers = (
            model.model_dump,
            model.model_dump_json,
            lambda: adapter.dump_python(model),
            lambda: adapter.dump_json(model),
            lambda: dict(model),
            model.__getstate__,
            lambda: copy.copy(model),
            lambda: copy.deepcopy(model),
            lambda: pickle.dumps(model),
        )

        for consume in consumers:
            with (
                warnings.catch_warnings(record=True) as caught,
                pytest.raises(
                    (McpConfigError, PydanticSerializationError)
                ) as exc_info,
            ):
                consume()
            _assert_exception_does_not_retain(exc_info.value, SENTINEL)
            assert SENTINEL not in repr(caught)

        with pytest.raises(McpConfigError):
            if isinstance(model, McpAuthConfig):
                McpServerConfig(id="x", auth=model)
            elif isinstance(model, McpServerConfig):
                GlobalConfig(registry={"mcp_servers": [model]})
            else:
                EffectiveConfig(
                    model if isinstance(model, GlobalConfig) else GlobalConfig(),
                    model if isinstance(model, ProjectConfig) else None,
                    None,
                )

    def test_frozen_mapping_is_attribute_less_and_hash_stable(self):
        config = GlobalConfig(registry={"metadata": {"nested": ["safe"]}})
        registry = vars(config)["registry"]

        before_hash = hash(registry)
        with pytest.raises((AttributeError, TypeError)):
            object.__setattr__(registry, "payload", SENTINEL)
        assert hash(registry) == before_hash

    @pytest.mark.parametrize(
        "bad_key",
        [1, SecretStr(SENTINEL), object()],
    )
    def test_non_string_mapping_keys_fail_closed(self, bad_key):
        for payload in (
            {bad_key: SENTINEL},
            {"registry": {"metadata": {bad_key: SENTINEL}}},
        ):
            with pytest.raises(McpConfigError) as exc_info:
                GlobalConfig.model_validate(payload)
            _assert_exception_does_not_retain(exc_info.value, SENTINEL)

    @pytest.mark.parametrize("kind", ["cycle", "deep"])
    def test_recursive_registry_values_fail_closed(self, kind: str):
        if kind == "cycle":
            nested: dict[str, object] = {}
            nested["next"] = nested
        else:
            nested = {"value": "safe"}
            for _ in range(100):
                nested = {"next": nested}

        with pytest.raises(McpConfigError) as exc_info:
            GlobalConfig(registry={"metadata": nested})
        _assert_exception_does_not_retain(exc_info.value, SENTINEL)

    def test_effective_config_deeply_snapshots_nested_models(self):
        global_config = GlobalConfig(
            disclosure={"strategy": "manual"},
            resolution={"categories": {"mcp": {"preferred": "safe"}}},
        )
        project_config = ProjectConfig(
            context={"name": "safe"},
            scope={"mcp_servers": {"include": ["safe"]}},
        )
        effective = EffectiveConfig(global_config, project_config, None)

        assert effective.global_cfg.disclosure is not global_config.disclosure
        assert effective.global_cfg.resolution is not global_config.resolution
        assert effective.project_cfg is not None
        assert effective.project_cfg.context is not project_config.context
        assert effective.project_cfg.scope is not project_config.scope

        global_config.disclosure.strategy = SENTINEL
        global_config.resolution.categories["mcp"].preferred = SENTINEL
        project_config.context.name = SENTINEL
        project_config.scope.mcp_servers["include"].append(SENTINEL)

        assert effective.global_cfg.disclosure.strategy == "manual"
        assert effective.global_cfg.resolution.categories["mcp"].preferred == "safe"
        assert effective.project_cfg.context.name == "safe"
        assert effective.project_cfg.scope.mcp_servers["include"] == ["safe"]

    def test_frozen_mapping_items_and_values_are_structurally_linear(self):
        registry = {f"key-{index}": index for index in range(1_000)}
        config = GlobalConfig(registry=registry)
        backing = vars(config)["registry"]

        assert list(backing.items()) == list(registry.items())
        assert list(backing.values()) == list(registry.values())
        assert all(key in backing for key in registry)

    def test_security_models_keep_native_schema_serializer(self):
        for model_type in (
            McpAuthConfig,
            McpServerConfig,
            GlobalConfig,
            ProjectConfig,
        ):
            assert type(model_type.__pydantic_serializer__).__name__ == "SchemaSerializer"

    @pytest.mark.parametrize(
        "adapter_factory",
        [
            lambda: TypeAdapter(list[McpServerConfig]),
            lambda: TypeAdapter(list[Any]),
            lambda: TypeAdapter(list[object]),
            lambda: TypeAdapter(list[object | None]),
            lambda: TypeAdapter(Any),
            lambda: TypeAdapter(object),
        ],
    )
    def test_generic_serialization_uses_model_schema_boundary(
        self, adapter_factory
    ):
        server = McpServerConfig(id="safe", args=["--mode", "safe"])
        adapter = adapter_factory()
        wrapped: object = server if adapter.core_schema.get("type") == "any" else [server]

        vars(server)["args"] = ["--mode", "next"]
        with warnings.catch_warnings(record=True) as caught:
            dumped = adapter.dump_python(wrapped)
            dumped_json = adapter.dump_json(wrapped)
        assert not caught
        assert "next" in repr(dumped)
        assert b"next" in dumped_json

        vars(server)["args"] = ["--token", SENTINEL]
        for dump in (adapter.dump_python, adapter.dump_json):
            with (
                warnings.catch_warnings(record=True) as caught,
                pytest.raises(
                    (McpConfigError, PydanticSerializationError)
                ) as exc_info,
            ):
                dump(wrapped)
            _assert_exception_does_not_retain(exc_info.value, SENTINEL)
            assert SENTINEL not in repr(caught)

    def test_nested_wrapper_serialization_uses_model_schema_boundary(self):
        class Wrapper(BaseModel):
            server: McpServerConfig

        wrapper = Wrapper(server=McpServerConfig(id="safe"))
        vars(wrapper.server)["args"] = ["--token", SENTINEL]

        for dump in (wrapper.model_dump, wrapper.model_dump_json):
            with (
                warnings.catch_warnings(record=True) as caught,
                pytest.raises(
                    (McpConfigError, PydanticSerializationError)
                ) as exc_info,
            ):
                dump()
            _assert_exception_does_not_retain(exc_info.value, SENTINEL)
            assert SENTINEL not in repr(caught)

    def test_credential_reference_is_opaque_standard_secret(self):
        auth = McpAuthConfig(credential_ref=SENTINEL)
        reference = auth.credential_ref

        assert isinstance(reference, SecretStr)
        assert auth.credential_id() == SENTINEL
        assert SENTINEL not in repr(reference)
        assert SENTINEL not in str(reference)
        assert reference != SENTINEL
        for operation in (
            lambda: reference[:],
            lambda: reference + "suffix",
            lambda: reference.encode(),
            lambda: json.dumps(reference),
        ):
            with pytest.raises((AttributeError, TypeError)):
                operation()
        assert isinstance(auth.model_dump()["credential_ref"], SecretStr)
        assert SENTINEL not in auth.model_dump_json()

    def test_mutated_secretstr_is_revalidated_at_downstream_boundaries(self):
        auth = McpAuthConfig(credential_ref="operator")
        # Arbitrary in-process object mutation is outside the config boundary.
        vars(auth.credential_ref)["_secret_value"] = f"../{SENTINEL}"

        with pytest.raises(PydanticSerializationError) as dump_exc:
            auth.model_dump()
        _assert_exception_does_not_retain(dump_exc.value, SENTINEL)
        with pytest.raises(McpConfigError) as compose_exc:
            McpServerConfig(id="safe", auth=auth)
        _assert_exception_does_not_retain(compose_exc.value, SENTINEL)

    @pytest.mark.parametrize(
        "model",
        [
            McpServerConfig(id="legacy"),
            GlobalConfig(),
            ProjectConfig(),
        ],
    )
    def test_string_formatting_and_logging_are_safe_after_corruption(
        self, model, caplog
    ):
        if isinstance(model, McpServerConfig):
            vars(model)["args"] = ["--token", SENTINEL]
        else:
            vars(model)["registry"] = {
                "mcp_servers": [{"id": "x", "API_TOKEN": SENTINEL}]
            }

        with caplog.at_level(logging.WARNING, logger="gearcore.config-test"):
            logging.getLogger("gearcore.config-test").warning("model=%s", model)
        format_rendered = format(model, "")
        percent_rendered = "%s".__mod__(model)
        rendered = "\n".join(
            (str(model), f"{model}", format_rendered, percent_rendered, caplog.text)
        )
        assert SENTINEL not in rendered
        assert "invalid configuration" in rendered

    def test_frozen_mapping_has_order_independent_mapping_equality_and_hash(self):
        expected = {"a": {"nested": [1, 2]}, "b": 2}
        first = vars(GlobalConfig(registry=expected))["registry"]
        second = vars(
            GlobalConfig(registry={"b": 2, "a": {"nested": [1, 2]}})
        )["registry"]

        assert first == second
        assert first == expected
        assert first == MappingProxyType(expected)
        assert hash(first) == hash(second)

    def test_frozen_mapping_lookup_uses_mapping_semantics_only(self):
        backing = vars(GlobalConfig(registry={"zero": 0}))["registry"]

        assert backing["zero"] == 0
        for key in (0, slice(None)):
            assert key not in backing
            with pytest.raises(KeyError):
                backing[key]

    def test_same_object_nan_is_reflexive_across_safe_copy_paths(self):
        nan = float("nan")
        config = GlobalConfig(registry={"value": nan})
        copies = (
            config.model_copy(),
            copy.copy(config),
            copy.deepcopy(config),
        )

        assert config == config
        assert all(candidate == config for candidate in copies)
        assert GlobalConfig(registry={"value": float("nan")}) != config

    @pytest.mark.parametrize("bad_key", [1, SecretStr(SENTINEL), object()])
    def test_server_root_mapping_keys_must_be_plain_strings(self, bad_key):
        with pytest.raises(McpConfigError) as exc_info:
            McpServerConfig.model_validate({"id": "safe", bad_key: SENTINEL})
        _assert_exception_does_not_retain(exc_info.value, SENTINEL)

    @pytest.mark.parametrize("method", ["model_validate", "model_validate_json"])
    def test_alternate_auth_validation_does_not_retain_unknown_input(
        self, method: str
    ):
        payload: object = {
            "credential_ref": "operator",
            "stdio_environment": "HIVE_AUTH",
            "unexpected": SENTINEL,
        }
        if method == "model_validate_json":
            payload = (
                '{"credential_ref":"operator","stdio_environment":"HIVE_AUTH",'
                f'"unexpected":"{SENTINEL}"}}'
            )

        with pytest.raises(McpConfigError) as exc_info:
            getattr(McpAuthConfig, method)(payload)

        _assert_exception_does_not_retain(exc_info.value, SENTINEL)

    @pytest.mark.parametrize(
        "sensitive_name",
        [
            "HIVE_AUTH",
            "SERVICE_PAT",
            "BEARER",
            "API_TOKEN",
            "service_api_key",
            "PASSWORD",
            "HIVE_DISPATCHER_CREDENTIAL",
            "Authorization",
        ],
    )
    def test_sensitive_environment_values_are_rejected_without_echo(
        self, sensitive_name: str
    ):
        with pytest.raises(McpConfigError) as exc_info:
            McpServerConfig(
                id="legacy",
                type="stdio",
                command="legacy",
                env={sensitive_name: SENTINEL},
            )

        assert SENTINEL not in str(exc_info.value)
        assert SENTINEL not in repr(exc_info.value)
        _assert_exception_does_not_retain(exc_info.value, SENTINEL)

    @pytest.mark.parametrize(
        "safe_name",
        [
            "PASSWORD_POLICY",
            "PASSWORD_FILE",
            "API_TOKEN_PATH",
            "HIVE_AUTH_REF",
            "TOKENIZER_PATH",
        ],
    )
    def test_reference_and_policy_environment_names_are_not_false_positives(
        self, safe_name: str
    ):
        server = McpServerConfig(
            id="legacy",
            type="stdio",
            command="legacy",
            env={safe_name: "/safe/reference-or-policy"},
        )

        assert server.env == {safe_name: "/safe/reference-or-policy"}

    @pytest.mark.parametrize(
        "args",
        [
            ["--token", SENTINEL],
            ["--api-key", SENTINEL],
            ["--password", SENTINEL],
            ["--auth", SENTINEL],
            [f"--bearer={SENTINEL}"],
            ["--header", f"Authorization: Bearer {SENTINEL}"],
            ["--HEADER", f"Authorization: Bearer {SENTINEL}"],
            ["--http-header", f"Authorization: Bearer {SENTINEL}"],
            ["-H", f"authorization: bearer {SENTINEL}"],
            [f"--header=Authorization%3A%20Bearer%20{SENTINEL}"],
            [f"--Headers=Authorization: Bearer {SENTINEL}"],
            [f"--http-header=Authorization: Bearer {SENTINEL}"],
            [f"--HTTP-HEADER=authorization: bearer {SENTINEL}"],
            [f"--http_headers=authorization: bearer {SENTINEL}"],
            [f"-HX-API-Key: {SENTINEL}"],
        ],
    )
    def test_secret_bearing_arguments_are_rejected_without_retention(
        self, args: list[str]
    ):
        with pytest.raises(McpConfigError) as exc_info:
            McpServerConfig(id="legacy", type="stdio", command="legacy", args=args)

        _assert_exception_does_not_retain(exc_info.value, SENTINEL)

    @pytest.mark.parametrize(
        "args",
        [
            ["--token-file", "/safe/token"],
            ["--password-policy", "strict"],
            ["--config", "/safe/config.yaml"],
            ["--header", "Accept: application/json"],
            ["--HTTP-HEADER", "Accept: application/json"],
            ["--headers=X-Mode: operator"],
            ["-H", "X-Mode: operator"],
        ],
    )
    def test_normal_and_reference_arguments_are_accepted(self, args: list[str]):
        server = McpServerConfig(
            id="legacy", type="stdio", command="legacy", args=args
        )

        assert server.args == args

    @pytest.mark.parametrize(
        "url",
        [
            f"https://user:{SENTINEL}@example.test/mcp",
            f"https://example.test/mcp?api_key={SENTINEL}",
            f"https://example.test/mcp?access_token={SENTINEL}",
            f"https://example.test/mcp?auth={SENTINEL}",
            f"https://example.test/mcp?API%5FTOKEN={SENTINEL}",
            f"http://[bad?token={SENTINEL}",
        ],
    )
    def test_url_userinfo_and_sensitive_query_are_rejected_without_retention(
        self, url: str
    ):
        with pytest.raises(McpConfigError) as exc_info:
            McpServerConfig(id="remote", type="sse", url=url)

        _assert_exception_does_not_retain(exc_info.value, SENTINEL)

    @pytest.mark.parametrize(
        "url",
        [
            "https://example.test/mcp",
            "https://example.test/mcp?token_file=%2Fsafe%2Ftoken",
            "http://127.0.0.1:8765/sse?mode=operator",
        ],
    )
    def test_normal_and_reference_urls_are_accepted(self, url: str):
        server = McpServerConfig(id="remote", type="sse", url=url)

        assert server.url == url

    @pytest.mark.parametrize("container", ["headers", "http_headers"])
    def test_sensitive_header_like_extra_is_rejected_without_echo(
        self, container: str
    ):
        with pytest.raises(McpConfigError) as exc_info:
            McpServerConfig(
                id="legacy",
                type="sse",
                **{container: {"Authorization": SENTINEL}},
            )

        assert SENTINEL not in str(exc_info.value)
        assert SENTINEL not in repr(exc_info.value)
        _assert_exception_does_not_retain(exc_info.value, SENTINEL)

    def test_nested_legacy_auth_container_cannot_claim_typed_reference_safety(self):
        payload = {
            "id": "legacy",
            "type": "stdio",
            "metadata": {"auth": {"credential_ref": SENTINEL}},
        }

        with pytest.raises(McpConfigError) as exc_info:
            McpServerConfig(**payload)

        _assert_exception_does_not_retain(exc_info.value, SENTINEL)

        for config_type in (GlobalConfig, ProjectConfig):
            with pytest.raises(McpConfigError) as outer_exc:
                config_type(registry={"mcp_servers": [payload]})
            _assert_exception_does_not_retain(outer_exc.value, SENTINEL)

    def test_legitimate_nonsecret_environment_and_v2_extra_remain_compatible(self):
        server = McpServerConfig(
            id="legacy",
            type="stdio",
            command="legacy",
            env={
                "PATH": "/usr/bin",
                "LOG_LEVEL": "debug",
                "TOKENIZER_PATH": "/models/tokenizer",
            },
            legacy_extension="retained-v2-input",
        )

        assert server.env == {
            "PATH": "/usr/bin",
            "LOG_LEVEL": "debug",
            "TOKENIZER_PATH": "/models/tokenizer",
        }
        global_config = GlobalConfig(
            registry={
                "mcp_servers": [
                    {
                        "id": "legacy",
                        "type": "stdio",
                        "legacy_extension": "retained-v2-input",
                    }
                ]
            }
        )
        assert (
            global_config.registry["mcp_servers"][0]["legacy_extension"]
            == "retained-v2-input"
        )

    @pytest.mark.parametrize(
        "safe_name",
        ["COMPAT", "APP_COMPAT", "NOAUTH", "OAUTH"],
    )
    def test_sensitive_alias_matching_has_no_compact_suffix_false_positives(
        self, safe_name: str
    ):
        server = McpServerConfig(
            id="legacy", type="stdio", env={safe_name: "enabled"}
        )

        assert server.env == {safe_name: "enabled"}

    @pytest.mark.parametrize("config_type", [GlobalConfig, ProjectConfig])
    @pytest.mark.parametrize(
        "entrypoint",
        ["constructor", "model_validate", "model_validate_json", "model_validate_strings"],
    )
    def test_outer_config_entrypoints_return_sentinel_free_exception_graphs(
        self, config_type, entrypoint: str
    ):
        payload = {
            "registry": {
                "mcp_servers": [
                    {"id": "legacy", "type": "stdio", "API_TOKEN": SENTINEL}
                ]
            }
        }

        with pytest.raises(McpConfigError) as exc_info:
            if entrypoint == "constructor":
                config_type(**payload)
            elif entrypoint == "model_validate_json":
                config_type.model_validate_json(json.dumps(payload))
            else:
                getattr(config_type, entrypoint)(payload)

        _assert_exception_does_not_retain(exc_info.value, SENTINEL)

    @pytest.mark.parametrize(
        "plaintext_field",
        [
            "token",
            "api_key",
            "apikey",
            "secret",
            "password",
            "credential",
            "authorization",
        ],
    )
    def test_plaintext_nested_credentials_are_rejected_without_echo(
        self, plaintext_field: str
    ):
        with pytest.raises(McpConfigError) as exc_info:
            McpServerConfig(
                id="dispatcher",
                type="stdio",
                auth={
                    "credential_ref": "operator",
                    "stdio_environment": "TOKEN",
                    plaintext_field: SENTINEL,
                },
            )

        assert SENTINEL not in str(exc_info.value)
        assert SENTINEL not in repr(exc_info.value)
        _assert_exception_does_not_retain(exc_info.value, SENTINEL)

    def test_plaintext_yaml_is_rejected_before_entering_config_model(
        self, tmp_path: Path, caplog
    ):
        from gearcore_hub.config import load_global_config

        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            "version: 2\n"
            "registry:\n"
            "  mcp_servers:\n"
            "    - id: dispatcher\n"
            "      type: stdio\n"
            f"      token: {SENTINEL}\n",
            encoding="utf-8",
        )

        with pytest.raises(McpConfigError) as exc_info:
            load_global_config(config_file)

        assert SENTINEL not in str(exc_info.value)
        assert SENTINEL not in repr(exc_info.value)
        _assert_exception_does_not_retain(exc_info.value, SENTINEL)
        assert SENTINEL not in caplog.text

    @pytest.mark.parametrize(
        ("field", "yaml_fragment"),
        [
            ("api_key", "      api_key: {sentinel}\n"),
            ("credential", "      credential: {sentinel}\n"),
            ("env", "      env:\n        API_TOKEN: {sentinel}\n"),
            ("args", "      args: [--token, {sentinel}]\n"),
            (
                "url",
                "      url: https://user:{sentinel}@example.test/mcp\n",
            ),
            (
                "headers",
                "      headers:\n        Authorization: {sentinel}\n",
            ),
        ],
    )
    def test_plaintext_yaml_routes_are_rejected_without_ingestion(
        self, tmp_path: Path, caplog, field: str, yaml_fragment: str
    ):
        from gearcore_hub.config import load_global_config

        config_file = tmp_path / f"{field}.yaml"
        config_file.write_text(
            "version: 2\n"
            "registry:\n"
            "  mcp_servers:\n"
            "    - id: dispatcher\n"
            "      type: stdio\n"
            + yaml_fragment.format(sentinel=SENTINEL),
            encoding="utf-8",
        )

        with pytest.raises(McpConfigError) as exc_info:
            load_global_config(config_file)

        rendered_error = f"{exc_info.value!s}\n{exc_info.value!r}\n{caplog.text}"
        assert SENTINEL not in rendered_error
        _assert_exception_does_not_retain(exc_info.value, SENTINEL)

    def test_unknown_and_duplicate_auth_settings_are_rejected(self):
        with pytest.raises(McpConfigError, match="authentication") as exc_info:
            McpServerConfig(
                id="dispatcher",
                type="stdio",
                auth={
                    "credential_ref": "operator",
                    "stdio_environment": "TOKEN",
                    "environment": SENTINEL,
                },
            )

        assert SENTINEL not in str(exc_info.value)
        assert SENTINEL not in repr(exc_info.value)
        _assert_exception_does_not_retain(exc_info.value, SENTINEL)

    def test_bad_credential_reference_error_does_not_retain_input(self):
        bad_reference = f"../{SENTINEL}"

        with pytest.raises(McpConfigError) as exc_info:
            McpServerConfig(
                id="dispatcher",
                type="stdio",
                auth={
                    "credential_ref": bad_reference,
                    "stdio_environment": "HIVE_AUTH",
                },
            )

        _assert_exception_does_not_retain(exc_info.value, SENTINEL)

    @pytest.mark.parametrize("model_type", [McpAuthConfig, McpServerConfig])
    def test_inner_pydantic_failure_has_sentinel_free_exception_graph(
        self, model_type
    ):
        auth = {
            "credential_ref": SENTINEL,
            "stdio_environment": f"BAD={SENTINEL}",
        }

        with pytest.raises(McpConfigError) as exc_info:
            if model_type is McpAuthConfig:
                model_type(**auth)
            else:
                model_type(id="dispatcher", type="stdio", auth=auth)

        _assert_exception_does_not_retain(exc_info.value, SENTINEL)

    def test_duplicate_yaml_auth_key_is_rejected_without_secret_log(
        self, tmp_path: Path, caplog, capsys
    ):
        from gearcore_hub.config import load_global_config

        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            "version: 2\n"
            "registry:\n"
            "  mcp_servers:\n"
            "    - id: dispatcher\n"
            "      type: stdio\n"
            "      auth:\n"
            "        credential_ref: operator\n"
            f"        credential_ref: {SENTINEL}\n"
            "        stdio_environment: TOKEN\n",
            encoding="utf-8",
        )

        with caplog.at_level("ERROR", logger="gearcore.config"):
            config = load_global_config(config_file)

        assert config.mcp_servers == []
        effective = EffectiveConfig(config, None, None)
        cmd_status(effective)
        rendered = "\n".join(
            (
                repr(config),
                repr(config.model_dump()),
                repr(effective),
                repr(effective.mcp_servers),
                repr(effective.diagnostic_codes),
                capsys.readouterr().out,
                caplog.text,
            )
        )
        assert "Failed to parse config" in caplog.text
        assert SENTINEL not in rendered

    @pytest.mark.parametrize(
        "auth",
        [
            {"credential_ref": ""},
            {"credential_ref": "   ", "stdio_environment": "TOKEN"},
            {"credential_ref": "operator", "stdio_environment": "bad=name"},
        ],
    )
    def test_auth_structure_is_strictly_validated(self, auth: dict[str, str]):
        with pytest.raises(McpConfigError):
            McpServerConfig(id="dispatcher", type="stdio", auth=auth)

    def test_secret_never_enters_config_dump_repr_status_logs_or_errors(
        self, tmp_path: Path, capsys, caplog
    ):
        credential_root = tmp_path / "credentials"
        credential_root.mkdir(mode=0o700)
        credential_file = credential_root / "operator"
        credential_file.write_text(f"{SENTINEL}\n", encoding="utf-8")
        credential_file.chmod(0o600)
        loaded_secret = CredentialStore(credential_root).read("operator")
        assert loaded_secret.get_secret_value() == SENTINEL
        assert SENTINEL not in str(loaded_secret)
        assert SENTINEL not in repr(loaded_secret)

        global_cfg = GlobalConfig(
            version=3,
            profiles={"default": "operator", "entries": {"operator": {}}},
            registry={
                "mcp_servers": [
                    {
                        "id": "dispatcher",
                        "type": "stdio",
                        "command": "hive-dispatcher",
                        "auth": {
                            "credential_ref": SENTINEL,
                            "stdio_environment": "HIVE_DISPATCHER_CREDENTIAL",
                        },
                    }
                ]
            },
        )
        effective = EffectiveConfig(global_cfg, None, None)

        cmd_status(effective)
        rendered = "\n".join(
            (
                repr(global_cfg),
                repr(effective),
                repr(effective.mcp_servers),
                repr(global_cfg.model_dump()),
                repr(effective.mcp_servers[0].model_dump()),
                repr(effective.diagnostic_codes),
                capsys.readouterr().out,
                caplog.text,
            )
        )

        assert SENTINEL not in rendered


class TestProjectConfig:
    def test_allowlist_parsing(self):
        data = {
            "scope": {
                "mcp_servers": {"include": ["fs"]},
                "skills": {"include": ["web-research"]},
            }
        }
        cfg = ProjectConfig(**data)
        assert cfg.mcp_allowlist == ["fs"]
        assert cfg.skill_allowlist == ["web-research"]

    def test_no_allowlist_means_allow_all(self):
        cfg = ProjectConfig()
        assert cfg.mcp_allowlist is None
        assert cfg.skill_allowlist is None


class TestEffectiveConfig:
    def test_global_only(self):
        global_cfg = GlobalConfig(
            registry={
                "mcp_servers": [
                    {"id": "fs", "type": "stdio", "command": "npx"},
                    {"id": "web", "type": "sse", "url": "http://localhost"},
                ]
            }
        )
        effective = EffectiveConfig(global_cfg, None, None)
        assert len(effective.mcp_servers) == 2
        assert effective.context_name == "global"

    def test_project_filters_mcp_servers(self):
        global_cfg = GlobalConfig(
            registry={
                "mcp_servers": [
                    {"id": "fs", "type": "stdio", "command": "npx"},
                    {"id": "web", "type": "sse", "url": "http://localhost"},
                ]
            }
        )
        project_cfg = ProjectConfig(scope={"mcp_servers": {"include": ["fs"]}})
        effective = EffectiveConfig(global_cfg, project_cfg, Path("/tmp/fake"))
        assert len(effective.mcp_servers) == 1
        assert effective.mcp_servers[0].id == "fs"
        assert effective.profile_name == "default"
        assert effective.context_name == "global"  # no project name set

    def test_project_local_skills_dir_appended(self):
        global_cfg = GlobalConfig()
        project_cfg = ProjectConfig()
        effective = EffectiveConfig(global_cfg, project_cfg, Path("/tmp/fake"))
        assert any(".gearcore/skills" in str(d) for d in effective.skills_dirs)

    def test_project_scoped_server_def_included(self):
        global_cfg = GlobalConfig(
            registry={"mcp_servers": [{"id": "fs", "type": "stdio", "command": "npx"}]}
        )
        project_cfg = ProjectConfig(
            registry={
                "mcp_servers": [
                    {"id": "hive-gateway", "type": "sse", "url": "http://127.0.0.1:8765/sse"}
                ]
            }
        )
        effective = EffectiveConfig(global_cfg, project_cfg, Path("/tmp/fake"))
        ids = [s.id for s in effective.mcp_servers]
        assert ids == ["fs", "hive-gateway"]
        gateway = effective.mcp_servers[1]
        assert gateway.type == "sse"
        assert gateway.url == "http://127.0.0.1:8765/sse"

    def test_project_scoped_server_def_not_visible_without_project(self):
        global_cfg = GlobalConfig(
            registry={"mcp_servers": [{"id": "fs", "type": "stdio", "command": "npx"}]}
        )
        effective = EffectiveConfig(global_cfg, None, None)
        assert [s.id for s in effective.mcp_servers] == ["fs"]

    def test_project_scoped_disabled_server_filtered(self):
        global_cfg = GlobalConfig()
        project_cfg = ProjectConfig(
            registry={
                "mcp_servers": [
                    {"id": "off", "type": "sse", "url": "http://x", "enabled": False}
                ]
            }
        )
        effective = EffectiveConfig(global_cfg, project_cfg, Path("/tmp/fake"))
        assert effective.mcp_servers == []

    def test_project_def_overrides_global_with_same_id(self, caplog):
        global_cfg = GlobalConfig(
            registry={
                "mcp_servers": [
                    {"id": "gw", "type": "sse", "url": "http://global:1/sse"},
                    {"id": "fs", "type": "stdio", "command": "npx"},
                ]
            }
        )
        project_cfg = ProjectConfig(
            registry={
                "mcp_servers": [
                    {"id": "gw", "type": "sse", "url": "http://project:2/sse"}
                ]
            }
        )
        with caplog.at_level("WARNING", logger="gearcore.config"):
            effective = EffectiveConfig(global_cfg, project_cfg, Path("/tmp/fake"))
            servers = effective.mcp_servers
        assert [s.id for s in servers] == ["fs", "gw"]
        assert servers[1].url == "http://project:2/sse"
        assert any("gw" in r.message and "override" in r.message for r in caplog.records)

    def test_project_allowlist_does_not_hide_project_scoped_defs(self):
        global_cfg = GlobalConfig(
            registry={"mcp_servers": [{"id": "fs", "type": "stdio", "command": "npx"}]}
        )
        project_cfg = ProjectConfig(
            scope={"mcp_servers": {"include": ["fs"]}},
            registry={
                "mcp_servers": [
                    {"id": "hive-gateway", "type": "sse", "url": "http://127.0.0.1:8765/sse"}
                ]
            },
        )
        effective = EffectiveConfig(global_cfg, project_cfg, Path("/tmp/fake"))
        assert [s.id for s in effective.mcp_servers] == ["fs", "hive-gateway"]


class TestLoadConfig:
    def test_v3_default_profile_selected_without_project(self, monkeypatch, tmp_path):
        config_file = tmp_path / "global.yaml"
        config_file.write_text(
            "version: 3\n"
            "profiles:\n"
            "  default: operator\n"
            "  entries:\n"
            "    operator: {}\n"
            "    hive-worker:\n"
            "      constrained: true\n"
        )
        cwd = tmp_path / "outside-project"
        cwd.mkdir()
        monkeypatch.chdir(cwd)

        cfg = load_config(global_config_path=config_file)

        assert cfg.project_root is None
        assert cfg.profile_name == "operator"
        assert cfg.profile_source == "default"

    def test_v3_default_profile_selected_in_nested_v2_project(
        self, monkeypatch, tmp_path
    ):
        config_file = tmp_path / "global.yaml"
        config_file.write_text(
            "version: 3\n"
            "profiles:\n"
            "  default: operator\n"
            "  entries:\n"
            "    operator: {}\n"
            "    hive-worker:\n"
            "      constrained: true\n"
        )
        project = tmp_path / "project"
        project_config_dir = project / ".gearcore"
        project_config_dir.mkdir(parents=True)
        (project_config_dir / "config.yaml").write_text(
            "version: 2\ncontext:\n  name: hive-worker\n"
        )
        nested = project / "src" / "nested"
        nested.mkdir(parents=True)
        monkeypatch.chdir(nested)

        cfg = load_config(global_config_path=config_file)

        assert cfg.project_root == project
        assert cfg.context_name == "hive-worker"
        assert cfg.profile_name == "operator"
        assert cfg.profile_source == "default"

    def test_loads_from_explicit_global_path(self, tmp_path: Path):
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            "version: 2\nregistry:\n  mcp_servers:\n    - id: test\n      type: stdio\n      command: echo\n"
        )
        cfg = load_config(project=tmp_path, global_config_path=config_file)
        assert cfg.context_name == "global"
        assert len(cfg.mcp_servers) == 1

    def test_missing_config_is_graceful(self, tmp_path: Path):
        config_file = tmp_path / "nonexistent.yaml"
        cfg = load_config(project=tmp_path, global_config_path=config_file)
        assert cfg.context_name == "global"
        assert cfg.mcp_servers == []


def test_default_skills_dirs_include_bundled_superpowers(monkeypatch, tmp_path):
    fake_root = tmp_path / "third_party" / "superpowers"
    (fake_root / "skills").mkdir(parents=True)
    monkeypatch.setattr("gearcore_hub.vendor.VENDOR_ROOT", fake_root)
    cfg = GlobalConfig()
    assert bundled_superpowers_dir() in cfg.skills_dirs


def test_load_global_config_reads_core_skills(tmp_path):
    from gearcore_hub.config import load_global_config

    cfg = tmp_path / "config.yaml"
    cfg.write_text("disclosure:\n  core_skills:\n    - continuity-core\n")
    g = load_global_config(cfg)
    assert g.disclosure.core_skills == ["continuity-core"]


def test_load_global_config_missing_file_gives_defaults(tmp_path):
    from gearcore_hub.config import load_global_config

    g = load_global_config(tmp_path / "nope.yaml")
    assert g.disclosure.core_skills == []
