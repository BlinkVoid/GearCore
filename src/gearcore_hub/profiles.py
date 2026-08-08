"""Capability profile schema models."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

_POLICY_MODEL_CONFIG = ConfigDict(extra="forbid", frozen=True)


class DisclosureConfig(BaseModel):
    model_config = _POLICY_MODEL_CONFIG

    strategy: str = "manual"  # manual | semantic
    activation_threshold: float = 0.85
    core_skills: tuple[str, ...] = Field(default_factory=tuple)


class CapabilityList(BaseModel):
    model_config = _POLICY_MODEL_CONFIG

    include: tuple[str, ...] | None = None
    deny: tuple[str, ...] = Field(default_factory=tuple)
    protected: tuple[str, ...] = Field(default_factory=tuple)


class CapabilityScope(BaseModel):
    model_config = _POLICY_MODEL_CONFIG

    mcp_servers: CapabilityList = Field(default_factory=CapabilityList)
    skills: CapabilityList = Field(default_factory=CapabilityList)


class ProfileConfig(BaseModel):
    model_config = _POLICY_MODEL_CONFIG

    constrained: bool = False
    scope: CapabilityScope = Field(default_factory=CapabilityScope)
    disclosure: DisclosureConfig = Field(default_factory=DisclosureConfig)


class ProfilesConfig(BaseModel):
    model_config = _POLICY_MODEL_CONFIG

    default: str
    entries: dict[str, ProfileConfig]

    @model_validator(mode="after")
    def validate_default_profile(self) -> Self:
        if self.default not in self.entries:
            raise ValueError(
                f"default profile {self.default!r} is missing from profiles.entries"
            )
        return self


class ProjectProfilesConfig(BaseModel):
    """Project overlays keyed by a globally selected profile name."""

    model_config = _POLICY_MODEL_CONFIG

    entries: dict[str, ProfileConfig] = Field(default_factory=dict)

    @model_validator(mode="after")
    def reject_project_protection(self) -> Self:
        for name, profile in self.entries.items():
            protected = (
                profile.scope.mcp_servers.protected
                + profile.scope.skills.protected
            )
            if protected:
                raise ValueError(
                    f"project profile {name!r} cannot declare protected capabilities"
                )
        return self


@dataclass(frozen=True, slots=True)
class ResolvedCapabilities:
    """Immutable result of resolving one class of named capabilities."""

    active: tuple[str, ...]
    denied: tuple[str, ...]
    protected: tuple[str, ...]
    diagnostics: tuple[str, ...]


def _ordered_unique(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))


def resolve_capabilities(
    global_capabilities: Iterable[str],
    global_policy: CapabilityList,
    *,
    project_capabilities: Iterable[str] = (),
    project_include: Iterable[str] | None = None,
    project_deny: Iterable[str] = (),
) -> ResolvedCapabilities:
    """Resolve capability IDs without consulting process or filesystem state.

    Protected IDs are pinned to the global capability set. Project scope may
    narrow other globals and add project-local IDs, then all non-protected
    denies are applied as the final step.
    """

    global_ids = _ordered_unique(global_capabilities)
    project_ids = _ordered_unique(project_capabilities)
    protected = _ordered_unique(global_policy.protected)
    protected_set = set(protected)

    if global_policy.include is None:
        active = list(global_ids)
    else:
        included = set(global_policy.include)
        active = [
            capability_id
            for capability_id in global_ids
            if capability_id in included
        ]

    active_set = set(active)
    for capability_id in global_ids:
        if capability_id in protected_set and capability_id not in active_set:
            active.append(capability_id)
            active_set.add(capability_id)

    diagnostics: tuple[str, ...] = ()
    project_include_tuple = (
        None if project_include is None else _ordered_unique(project_include)
    )
    project_deny_tuple = _ordered_unique(project_deny)
    attempted_override = bool(protected_set.intersection(project_ids))
    attempted_override = attempted_override or bool(
        protected_set.intersection(project_deny_tuple)
    )
    if project_include_tuple is not None:
        attempted_override = attempted_override or bool(
            protected_set.difference(project_include_tuple)
        )
        project_include_set = set(project_include_tuple)
        active = [
            capability_id
            for capability_id in active
            if capability_id in protected_set
            or capability_id in project_include_set
        ]
        active_set = set(active)

    if attempted_override:
        diagnostics = ("protected_capability_override",)

    for capability_id in project_ids:
        if capability_id not in protected_set and capability_id not in active_set:
            active.append(capability_id)
            active_set.add(capability_id)

    denied = _ordered_unique((*global_policy.deny, *project_deny_tuple))
    denied = tuple(
        capability_id
        for capability_id in denied
        if capability_id not in protected_set
    )
    denied_set = set(denied)
    active = [
        capability_id for capability_id in active if capability_id not in denied_set
    ]

    return ResolvedCapabilities(
        active=tuple(active),
        denied=denied,
        protected=protected,
        diagnostics=diagnostics,
    )
