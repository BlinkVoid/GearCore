"""Tests for versioned capability profile configuration."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from gearcore_hub.config import EffectiveConfig, GlobalConfig, ProjectConfig

ROOT = Path("/tmp/profile-project")


def v2_global() -> GlobalConfig:
    return GlobalConfig(
        version=2,
        registry={
            "mcp_servers": [
                {"id": "fs", "type": "stdio", "command": "npx"},
                {"id": "web", "type": "sse", "url": "http://localhost"},
            ]
        },
    )


def v2_project(include: list[str]) -> ProjectConfig:
    return ProjectConfig(
        version=2,
        context={"name": "hive-worker"},
        scope={"mcp_servers": {"include": include}},
    )


def v3_global(default: str = "operator") -> GlobalConfig:
    return GlobalConfig(
        version=3,
        profiles={
            "default": default,
            "entries": {
                "operator": {
                    "scope": {
                        "mcp_servers": {
                            "include": ["fs", "hive-dispatcher"],
                            "deny": ["legacy-server"],
                            "protected": ["hive-dispatcher"],
                        },
                        "skills": {
                            "include": ["chrono-core", "hive-dispatcher"],
                            "deny": ["legacy-skill"],
                            "protected": ["hive-dispatcher"],
                        },
                    },
                    "disclosure": {"core_skills": ["chrono-core"]},
                },
                "hive-worker": {"constrained": True},
            },
        },
    )


def test_v2_maps_to_implicit_default_without_changing_allowlist():
    effective = EffectiveConfig(v2_global(), v2_project(include=["fs"]), ROOT)

    assert effective.profile_name == "default"
    assert [server.id for server in effective.mcp_servers] == ["fs"]


def test_v3_selects_operator_without_cwd_authority():
    effective = EffectiveConfig(v3_global(default="operator"), v2_project([]), ROOT)

    assert effective.profile_name == "operator"
    assert effective.profile_source == "default"
    assert effective.disclosure.core_skills == ("chrono-core",)


def test_v3_parses_include_deny_and_protected_lists():
    profiles = v3_global().profiles
    assert profiles is not None
    operator = profiles.entries["operator"]

    assert operator.scope.mcp_servers.include == ("fs", "hive-dispatcher")
    assert operator.scope.mcp_servers.deny == ("legacy-server",)
    assert operator.scope.mcp_servers.protected == ("hive-dispatcher",)
    assert operator.scope.skills.include == ("chrono-core", "hive-dispatcher")
    assert operator.scope.skills.deny == ("legacy-skill",)
    assert operator.scope.skills.protected == ("hive-dispatcher",)


@pytest.mark.parametrize(
    ("profile_data", "typo"),
    [
        (
            {"default": "operator", "entries": {"operator": {}}, "fallback": "operator"},
            "fallback",
        ),
        (
            {"default": "operator", "entries": {"operator": {"constrainted": True}}},
            "constrainted",
        ),
        (
            {
                "default": "operator",
                "entries": {"operator": {"scope": {"skils": {"include": []}}}},
            },
            "skils",
        ),
        (
            {
                "default": "operator",
                "entries": {
                    "operator": {
                        "scope": {"mcp_servers": {"denny": ["unsafe"]}}
                    }
                },
            },
            "denny",
        ),
        (
            {
                "default": "operator",
                "entries": {
                    "operator": {"disclosure": {"core_skillz": ["chrono-core"]}}
                },
            },
            "core_skillz",
        ),
    ],
)
def test_v3_rejects_unknown_policy_keys(profile_data: dict, typo: str):
    with pytest.raises(ValidationError, match=typo):
        GlobalConfig(version=3, profiles=profile_data)


def test_v2_keeps_legacy_extra_key_compatibility():
    global_cfg = GlobalConfig(version=2, disclosure={"core_skillz": ["ignored"]})
    project_cfg = ProjectConfig(version=2, scope={"skils": {"include": []}})

    assert global_cfg.disclosure.core_skills == []
    assert project_cfg.skill_allowlist is None


@pytest.mark.parametrize("attribute", ["profile_name", "profile_source", "profile"])
def test_effective_profile_selection_is_read_only(attribute: str):
    effective = EffectiveConfig(v3_global(), None, None)

    with pytest.raises(AttributeError):
        setattr(effective, attribute, "replacement")


def test_effective_profile_fields_are_read_only():
    effective = EffectiveConfig(v3_global(), None, None)

    with pytest.raises(ValidationError, match="frozen"):
        effective.profile.constrained = True


def test_effective_profile_is_an_isolated_policy_snapshot():
    global_cfg = v3_global()
    effective = EffectiveConfig(global_cfg, None, None)
    assert global_cfg.profiles is not None

    assert effective.profile is not global_cfg.profiles.entries["operator"]


@pytest.mark.parametrize(
    "collection_name",
    ["mcp_include", "mcp_deny", "mcp_protected", "skill_include", "core_skills"],
)
def test_effective_profile_collections_cannot_be_appended(collection_name: str):
    effective = EffectiveConfig(v3_global(), None, None)
    profile = effective.profile
    collections = {
        "mcp_include": profile.scope.mcp_servers.include,
        "mcp_deny": profile.scope.mcp_servers.deny,
        "mcp_protected": profile.scope.mcp_servers.protected,
        "skill_include": profile.scope.skills.include,
        "core_skills": profile.disclosure.core_skills,
    }
    collection = collections[collection_name]
    assert collection is not None

    with pytest.raises((AttributeError, TypeError)):
        collection.append("unexpected")


def test_v3_rejects_unknown_default_profile():
    with pytest.raises(ValidationError, match="default profile.*missing"):
        GlobalConfig(
            version=3,
            profiles={
                "default": "missing",
                "entries": {"operator": {}},
            },
        )


def test_v3_requires_profiles_configuration():
    with pytest.raises(ValidationError, match="profiles"):
        GlobalConfig(version=3)


@pytest.mark.parametrize("version", [1, 4])
def test_global_config_rejects_unsupported_versions(version: int):
    with pytest.raises(ValidationError, match="version"):
        GlobalConfig(version=version)


@pytest.mark.parametrize("version", [1, 4])
def test_project_config_rejects_unsupported_versions(version: int):
    with pytest.raises(ValidationError, match="version"):
        ProjectConfig(version=version)
