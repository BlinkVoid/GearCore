"""Capability profile schema models."""

from __future__ import annotations

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
