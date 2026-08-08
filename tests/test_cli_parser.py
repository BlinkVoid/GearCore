import asyncio
import json
import logging
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from gearcore_hub.config import load_config
from gearcore_hub.credentials import CredentialStore
from gearcore_hub.main import _silence_logger, build_parser, cmd_status
from gearcore_hub.main import main as cli_main


def test_update_superpowers_parser():
    parser = build_parser()
    args = parser.parse_args(["update-superpowers"])
    assert args.command == "update-superpowers"
    assert args.dry_run is False

    args = parser.parse_args(["update-superpowers", "--dry-run"])
    assert args.dry_run is True


def test_add_mcp_command_flag_does_not_clobber_subcommand():
    # Regression: --command shared dest="command" with the subparser action,
    # so `add-mcp --command uvx` dispatched to "uvx" (no-op help) instead.
    parser = build_parser()
    args = parser.parse_args(
        [
            "add-mcp",
            "--id",
            "jira",
            "--type",
            "stdio",
            "--command",
            "uvx",
            "--args",
            "mcp-atlassian",
            "--env",
            "A=B",
        ]
    )
    assert args.command == "add-mcp"
    assert args.mcp_command == "uvx"
    assert args.args == ["mcp-atlassian"]


def test_profile_set_parser_accepts_repeatable_capability_options():
    args = build_parser().parse_args(
        [
            "profile-set",
            "operator",
            "--mcp-include", "hive-dispatcher",
            "--mcp-include", "safe",
            "--mcp-deny", "unsafe",
            "--mcp-protect", "hive-dispatcher",
            "--skill-include", "hive-dispatcher",
            "--skill-deny", "unsafe-skill",
            "--skill-protect", "hive-dispatcher",
            "--core-skill", "hive-dispatcher",
            "--constrained",
            "--default",
        ]
    )

    assert args.command == "profile-set"
    assert args.name == "operator"
    assert args.mcp_include == ["hive-dispatcher", "safe"]
    assert args.mcp_deny == ["unsafe"]
    assert args.mcp_protect == ["hive-dispatcher"]
    assert args.skill_include == ["hive-dispatcher"]
    assert args.skill_deny == ["unsafe-skill"]
    assert args.skill_protect == ["hive-dispatcher"]
    assert args.core_skill == ["hive-dispatcher"]
    assert args.constrained is True
    assert args.make_default is True


@pytest.mark.parametrize(
    "unsafe_id",
    [
        "missing\nINJECTED_LOG_LINE",
        "missing\rINJECTED_STATUS_LINE",
        "missing\tINJECTED_FIELD",
        "missing\x1b[31mINJECTED_COLOR",
        "missing\x00INJECTED_NUL",
        "missing\u200bINJECTED_FORMAT",
        "missing\u2028INJECTED_LINE_SEPARATOR",
        "missing\u2029INJECTED_PARAGRAPH_SEPARATOR",
    ],
)
def test_call_rejects_control_character_id_without_cli_or_log_injection(
    tmp_path, monkeypatch, capsys, caplog, unsafe_id
):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "version: 2\n"
        "registry:\n"
        "  mcp_servers:\n"
        "    - id: safe\n"
        "      type: stdio\n"
        "      command: safe-command\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "gearcore",
            "--config",
            str(config_path),
            "call",
            unsafe_id,
            "health",
            "{}",
        ],
    )

    with pytest.raises(SystemExit) as exc_info:
        cli_main()

    captured = capsys.readouterr()
    rendered = captured.out + captured.err + caplog.text
    assert exc_info.value.code == 1
    assert rendered.strip() == "error: capability_denied"
    assert "INJECTED" not in rendered


def test_profile_set_cli_is_global_only_and_does_not_print_config_path(
    tmp_path, monkeypatch, capsys
):
    config_path = tmp_path / "secret-config-path.yaml"
    config_path.write_text("version: 2\n", encoding="utf-8")
    monkeypatch.setattr(
        sys,
        "argv",
        ["gearcore", "--config", str(config_path), "profile-set", "operator"],
    )

    cli_main()

    captured = capsys.readouterr()
    assert captured.out == "Profile 'operator' updated\n"
    assert str(config_path) not in captured.out + captured.err

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "gearcore",
            "--config",
            str(config_path),
            "--project",
            str(tmp_path),
            "profile-set",
            "operator",
        ],
    )
    with pytest.raises(SystemExit) as exit_info:
        cli_main()
    assert exit_info.value.code == 1
    assert "global-only" in capsys.readouterr().err


def test_profile_set_cli_rejects_symlink_config_without_following_it(
    tmp_path, monkeypatch, capsys
):
    real = tmp_path / "real.yaml"
    real.write_text("version: 2\n", encoding="utf-8")
    link = tmp_path / "config-link.yaml"
    link.symlink_to(real)
    monkeypatch.setattr(
        sys,
        "argv",
        ["gearcore", "--config", str(link), "profile-set", "operator"],
    )

    with pytest.raises(SystemExit) as exit_info:
        cli_main()

    assert exit_info.value.code == 1
    assert real.read_text(encoding="utf-8") == "version: 2\n"
    assert str(tmp_path) not in capsys.readouterr().err


def test_launch_policy_flags_are_available_to_runtime_commands():
    parser = build_parser()

    for command in ("serve", "status", "list", "list-skills", "request-skill", "call"):
        argv = [
            "--config",
            "/safe/config.yaml",
            "--profile",
            "hive-worker",
            "--context-envelope",
            "/safe/envelope.json",
            "--envelope-public-key",
            "/safe/public-key.json",
            command,
        ]
        if command == "request-skill":
            argv.append("worker")
        elif command == "call":
            argv.extend(("hive-gateway", "submit", "{}"))

        args = parser.parse_args(argv)

        assert args.config == "/safe/config.yaml"
        assert args.profile == "hive-worker"
        assert args.context_envelope == "/safe/envelope.json"
        assert args.envelope_public_key == "/safe/public-key.json"


@pytest.mark.parametrize(
    "launch_args",
    [
        ["--context-envelope", "", "--envelope-public-key", ""],
        ["--context-envelope", "   ", "--envelope-public-key", "   "],
        ["--context-envelope", ""],
        ["--envelope-public-key", ""],
        ["--context-envelope", "   "],
        ["--envelope-public-key", "   "],
        ["--context-envelope", "secret-missing-envelope"],
        ["--envelope-public-key", "secret-missing-key"],
    ],
)
def test_explicit_empty_envelope_cli_inputs_never_fall_back_to_operator(
    tmp_path, monkeypatch, capsys, launch_args
):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """\
version: 3
profiles:
  default: operator
  entries:
    operator: {}
    hive-worker:
      constrained: true
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["gearcore", "--config", str(config_path), *launch_args, "status"],
    )

    cli_main()

    output = capsys.readouterr().out
    assert "invalid_launch_envelope" in output
    assert "Profile: operator" not in output
    for value in launch_args[1::2]:
        if value.strip():
            assert value not in output


@pytest.mark.parametrize(
    "launch_args",
    [
        ["--context-envelope", ""],
        ["--context-envelope", "", "--envelope-public-key", ""],
        [
            "--context-envelope",
            "missing-envelope",
            "--envelope-public-key",
            "missing-key",
        ],
    ],
)
def test_invalid_envelope_is_rejected_before_explicit_project_resolution(
    tmp_path, monkeypatch, capsys, launch_args
):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """\
version: 3
profiles:
  default: operator
  entries:
    operator: {}
    hive-worker:
      constrained: true
""",
        encoding="utf-8",
    )
    guarded_project = Path("secret-pathological-project")
    original_resolve = Path.resolve

    def guarded_resolve(path, *args, **kwargs):
        if path == guarded_project:
            raise AssertionError("explicit project resolved before envelope validation")
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", guarded_resolve)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "gearcore",
            "--config",
            str(config_path),
            "--project",
            str(guarded_project),
            *launch_args,
            "status",
        ],
    )

    cli_main()

    output = capsys.readouterr().out
    assert "invalid_launch_envelope" in output
    assert "secret-pathological-project" not in output


def test_status_prints_vendor_manifest(capsys):
    manifest = MagicMock()
    manifest.vendored_commit = "abcdef1234567890"
    manifest.vendored_at = "2026-07-05"
    manifest.source = "https://github.com/obra/superpowers.git"
    manifest.source_ref = "main"

    config = load_config(global_config_path=Path("/nonexistent"))

    with patch("gearcore_hub.vendor.load_vendor_manifest", return_value=manifest), \
         patch("gearcore_hub.vendor.get_upstream_commit", return_value="abcdef1234567890"):
        cmd_status(config)

    captured = capsys.readouterr()
    assert "superpowers" in captured.out
    assert "abcdef1234567890"[:12] in captured.out


def test_status_prints_update_hint_when_upstream_different(capsys):
    manifest = MagicMock()
    manifest.vendored_commit = "abcdef1234567890"
    manifest.vendored_at = "2026-07-05"
    manifest.source = "https://github.com/obra/superpowers.git"
    manifest.source_ref = "main"

    config = load_config(global_config_path=Path("/nonexistent"))

    with patch("gearcore_hub.vendor.load_vendor_manifest", return_value=manifest), \
         patch("gearcore_hub.vendor.get_upstream_commit", return_value="fedcba0987654321"):
        cmd_status(config)

    captured = capsys.readouterr()
    assert "update available" in captured.out
    assert "fedcba098765" in captured.out


def test_status_has_stable_capability_fields_and_redacts_backend_details(
    tmp_path, capsys, caplog
):
    skills = tmp_path / "skills"
    bundle = skills / "worker-skill"
    bundle.mkdir(parents=True)
    (bundle / "SKILL.md").write_text("secret-instruction-sentinel")
    (skills / "broken-secret-sentinel").symlink_to(
        tmp_path / "secret-broken-target"
    )
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"""\
version: 3
registry:
  skills_dirs: [{skills}]
  mcp_servers:
    - id: worker
      type: http
      url: https://secret-url-sentinel.invalid
      auth:
        credential_ref: secret-credential-sentinel
        http_scheme: bearer
profiles:
  default: hive-worker
  entries:
    hive-worker:
      constrained: true
      scope:
        mcp_servers:
          include: [worker]
          deny: [operator-only]
          protected: [worker]
        skills:
          include: [worker-skill]
          deny: [operator-skill]
          protected: [worker-skill]
""",
        encoding="utf-8",
    )
    config = load_config(
        project=tmp_path / "isolated-project",
        global_config_path=config_path,
    )
    credential_root = tmp_path / "status-credentials"
    credential_root.mkdir(mode=0o700)
    credential_file = credential_root / "secret-credential-sentinel"
    credential_file.write_text("secret-value-sentinel", encoding="utf-8")
    credential_file.chmod(0o600)

    with patch("gearcore_hub.vendor.load_vendor_manifest", return_value=None):
        cmd_status(config, credential_store=CredentialStore(credential_root))

    output = capsys.readouterr().out
    for line in (
        "profile: hive-worker",
        "source: default",
        "constrained: true",
        "active_mcp: worker",
        "denied_mcp: operator-only",
        "protected_mcp: worker",
        "active_skills: worker-skill",
        "denied_skills: operator-skill",
        "protected_skills: worker-skill",
        "diagnostics: skill_registry_unavailable",
    ):
        assert line in output
    for sentinel in (
        "secret-url-sentinel",
        "secret-credential-sentinel",
        "secret-value-sentinel",
        "secret-instruction-sentinel",
        str(tmp_path),
    ):
        assert sentinel not in output + caplog.text


def test_diagnostic_status_is_stable(capsys):
    config = load_config(
        global_config_path=Path("/nonexistent"),
        context_envelope="",
    )
    cmd_status(config)

    output = capsys.readouterr().out
    assert "profile: unavailable" in output
    assert "source: invalid-envelope" in output
    assert "constrained: true" in output
    assert "active_mcp: none" in output
    assert "active_skills: none" in output
    assert "diagnostics: invalid_launch_envelope" in output


@pytest.mark.parametrize("root_kind", ["file", "broken", "unreadable"])
def test_status_sanitizes_configured_skill_root_failures(
    tmp_path, capsys, caplog, monkeypatch, root_kind
):
    secret_root = tmp_path / "secret-skill-root-sentinel"
    if root_kind == "file":
        secret_root.write_text("not a directory", encoding="utf-8")
    elif root_kind == "broken":
        secret_root.symlink_to(tmp_path / "secret-missing-target")
    else:
        secret_root.mkdir()

    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"""\
version: 3
registry:
  skills_dirs: [{secret_root}]
profiles:
  default: operator
  entries:
    operator:
      scope:
        skills:
          include: [safe-skill]
          deny: [denied-skill]
          protected: [protected-skill]
""",
        encoding="utf-8",
    )
    config = load_config(
        project=tmp_path / "isolated-project",
        global_config_path=config_path,
    )
    if root_kind == "unreadable":
        original_iterdir = Path.iterdir

        def guarded_iterdir(path):
            if path == secret_root:
                raise OSError(f"cannot read {secret_root}")
            return original_iterdir(path)

        monkeypatch.setattr(Path, "iterdir", guarded_iterdir)

    with patch("gearcore_hub.vendor.load_vendor_manifest", return_value=None):
        cmd_status(config)

    captured = capsys.readouterr()
    surfaces = captured.out + captured.err + caplog.text
    assert "diagnostics: skill_registry_unavailable" in captured.out
    assert "active_skills: none" in captured.out
    assert "denied_skills: denied-skill" in captured.out
    assert "protected_skills: protected-skill" in captured.out
    assert str(tmp_path) not in surfaces
    assert "secret-skill-root-sentinel" not in surfaces


@pytest.mark.parametrize(
    "control_flow",
    [KeyboardInterrupt(), SystemExit(7), asyncio.CancelledError()],
)
def test_status_does_not_swallow_base_control_flow(tmp_path, control_flow):
    config = load_config(
        project=tmp_path / "isolated-project",
        global_config_path=tmp_path / "missing.yaml",
    )

    with (
        patch("gearcore_hub.main.SkillManager", side_effect=control_flow),
        pytest.raises(type(control_flow)) as raised,
    ):
        cmd_status(config)

    if isinstance(control_flow, SystemExit):
        assert raised.value.code == 7


def test_status_escapes_hostile_legacy_scalars_and_visible_server_ids(
    tmp_path, capsys
):
    hostile = "evil\nsource: forged,delimiter\x1b[31m"
    visible_server_id = "evil source: forged,delimiter"
    skills = tmp_path / "skills"
    bundle = skills / "bundle"
    bundle.mkdir(parents=True)
    (bundle / "SKILL.md").write_text("safe", encoding="utf-8")
    (bundle / "manifest.json").write_text(
        '{"name": "evil\\nsource: forged,delimiter\\u001b[31m"}',
        encoding="utf-8",
    )
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "version": 3,
                "registry": {
                    "skills_dirs": [str(skills)],
                    "mcp_servers": [
                        {
                            "id": visible_server_id,
                            "type": "stdio",
                            "command": "safe",
                        }
                    ],
                },
                "profiles": {
                    "default": hostile,
                    "entries": {
                        hostile: {
                            "scope": {
                                "mcp_servers": {"include": [visible_server_id]},
                                "skills": {"include": [hostile]},
                            }
                        }
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    config = load_config(
        project=tmp_path / "isolated-project",
        global_config_path=config_path,
    )

    with patch("gearcore_hub.vendor.load_vendor_manifest", return_value=None):
        cmd_status(config)

    output = capsys.readouterr().out
    assert "\x1b" not in output
    assert sum(line.startswith("source: ") for line in output.splitlines()) == 1
    assert "source: forged" not in output.splitlines()
    assert "\\nsource: forged,delimiter\\u001b[31m" in output


@pytest.mark.parametrize("credential_state", ["missing", "empty", "unsafe"])
def test_status_excludes_servers_with_unavailable_credentials(
    tmp_path, capsys, credential_state
):
    credential_root = tmp_path / "credentials"
    credential_root.mkdir(mode=0o700)
    credential = credential_root / "private-auth-ref"
    if credential_state == "empty":
        credential.write_text("", encoding="utf-8")
        credential.chmod(0o600)
    elif credential_state == "unsafe":
        credential.write_text("secret-value-sentinel", encoding="utf-8")
        credential.chmod(0o644)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """\
version: 3
registry:
  mcp_servers:
    - id: authenticated
      type: stdio
      command: safe
      auth:
        credential_ref: private-auth-ref
        stdio_environment: SAFE_AUTH
    - id: public
      type: stdio
      command: safe
profiles:
  default: operator
  entries:
    operator: {}
""",
        encoding="utf-8",
    )
    config = load_config(
        project=tmp_path / "isolated-project",
        global_config_path=config_path,
    )

    with patch("gearcore_hub.vendor.load_vendor_manifest", return_value=None):
        cmd_status(config, credential_store=CredentialStore(credential_root))

    output = capsys.readouterr().out
    assert "active_mcp: public" in output
    assert "diagnostics: credential_unavailable" in output
    assert "private-auth-ref" not in output
    assert "secret-value-sentinel" not in output


@pytest.mark.parametrize("failure", ["malformed", "unreadable"])
def test_status_excludes_unavailable_protected_skill(
    tmp_path, capsys, caplog, monkeypatch, failure
):
    skills = tmp_path / "secret-skills-path"
    bundle = skills / "protected"
    bundle.mkdir(parents=True)
    skill_file = bundle / "SKILL.md"
    skill_file.write_text("secret-instruction", encoding="utf-8")
    manifest = bundle / "manifest.json"
    if failure == "malformed":
        manifest.write_text("{malformed", encoding="utf-8")
    else:
        original_read_text = Path.read_text

        def guarded_read_text(path, *args, **kwargs):
            if path == skill_file:
                raise OSError(f"cannot read {skill_file}")
            return original_read_text(path, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", guarded_read_text)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"""\
version: 3
registry:
  skills_dirs: [{skills}]
profiles:
  default: operator
  entries:
    operator:
      scope:
        skills:
          include: [protected]
          protected: [protected]
""",
        encoding="utf-8",
    )
    config = load_config(
        project=tmp_path / "isolated-project",
        global_config_path=config_path,
    )

    with patch("gearcore_hub.vendor.load_vendor_manifest", return_value=None):
        cmd_status(config)

    captured = capsys.readouterr()
    surfaces = captured.out + captured.err + caplog.text
    assert "active_skills: none" in captured.out
    assert "diagnostics: protected_skill_unavailable" in captured.out
    assert str(tmp_path) not in surfaces


def test_status_rejects_duplicate_global_bindings_for_protected_manifest_id(
    tmp_path, capsys, caplog
):
    skills = tmp_path / "secret-skills-path"
    for directory in ("first-bundle", "second-bundle"):
        bundle = skills / directory
        bundle.mkdir(parents=True)
        (bundle / "SKILL.md").write_text("secret-instruction", encoding="utf-8")
        (bundle / "manifest.json").write_text(
            json.dumps({"name": "protected-id"}), encoding="utf-8"
        )
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "version": 3,
                "registry": {"skills_dirs": [str(skills)]},
                "profiles": {
                    "default": "operator",
                    "entries": {
                        "operator": {
                            "scope": {
                                "skills": {
                                    "include": ["protected-id"],
                                    "protected": ["protected-id"],
                                }
                            }
                        }
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    config = load_config(
        project=tmp_path / "isolated-project",
        global_config_path=config_path,
    )

    with patch("gearcore_hub.vendor.load_vendor_manifest", return_value=None):
        cmd_status(config)

    captured = capsys.readouterr()
    surfaces = captured.out + captured.err + caplog.text
    assert "active_skills: none" in captured.out
    assert "diagnostics: protected_skill_unavailable" in captured.out
    assert str(tmp_path) not in surfaces


def test_logger_suppression_is_overlap_safe_and_restores_after_exception():
    logger = logging.getLogger("gearcore.test-overlap")
    original_disabled = logger.disabled
    original_filters = list(logger.filters)
    outer_ready = threading.Event()
    inner_ready = threading.Event()
    outer_exit = threading.Event()
    inner_exit = threading.Event()

    def outer():
        with _silence_logger(logger.name):
            outer_ready.set()
            assert inner_ready.wait(timeout=5)
        outer_exit.set()
        inner_exit.wait(timeout=5)

    def inner():
        assert outer_ready.wait(timeout=5)
        with _silence_logger(logger.name):
            inner_ready.set()
            assert outer_exit.wait(timeout=5)
            assert any(not item.filter(logging.LogRecord(
                logger.name, logging.INFO, __file__, 1, "safe", (), None
            )) for item in logger.filters)
        inner_exit.set()

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(outer)
        second = executor.submit(inner)
        first.result(timeout=10)
        second.result(timeout=10)
    assert logger.disabled is original_disabled
    assert logger.filters == original_filters

    with pytest.raises(RuntimeError), _silence_logger(logger.name):
        raise RuntimeError("safe")
    assert logger.filters == original_filters
