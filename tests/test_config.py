"""Tests for the layered configuration loader."""

import json
from pathlib import Path

import pytest
from pydantic import TypeAdapter

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

        assert server.auth is auth
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
