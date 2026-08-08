"""Canonical verification for constrained GearCore launch envelopes."""

from __future__ import annotations

import base64
import binascii
import json
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Self

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from gearcore_hub.profiles import CapabilityList, ProfileConfig

INVALID_ENVELOPE_DIAGNOSTIC = "invalid_launch_envelope"
ENVELOPE_EXPANSION_DIAGNOSTIC = "envelope_authority_expansion"

_BASE64URL_RE = re.compile(r"^[A-Za-z0-9_-]+={0,2}$")


class EnvelopeValidationError(ValueError):
    """A definition-safe launch-envelope validation failure."""

    def __init__(self) -> None:
        super().__init__(INVALID_ENVELOPE_DIAGNOSTIC)


class _LaunchEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    version: int
    profile: str = Field(min_length=1)
    issuer: str = Field(min_length=1)
    launch_id: str = Field(min_length=1)
    execution_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    issued_at: int
    expires_at: int
    nonce: str = Field(min_length=1)
    signature: str = Field(min_length=1)

    @field_validator(
        "profile",
        "issuer",
        "launch_id",
        "execution_id",
        "task_id",
        "nonce",
    )
    @classmethod
    def reject_blank_identity(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("blank envelope identity")
        return value

    @model_validator(mode="after")
    def validate_version_and_time_order(self) -> Self:
        if self.version != 1 or self.expires_at <= self.issued_at:
            raise ValueError("invalid envelope metadata")
        return self


class _PublicKeyDocument(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    version: int
    issuer: str = Field(min_length=1)
    public_key: str = Field(min_length=1)

    @field_validator("issuer")
    @classmethod
    def reject_blank_issuer(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("blank public-key issuer")
        return value

    @model_validator(mode="after")
    def validate_version(self) -> Self:
        if self.version != 1:
            raise ValueError("invalid public-key document version")
        return self


@dataclass(frozen=True, slots=True)
class VerifiedLaunchEnvelope:
    """Verified envelope metadata safe to retain in effective configuration."""

    profile: str
    issuer: str
    launch_id: str
    execution_id: str
    task_id: str
    issued_at: int
    expires_at: int
    nonce: str


def canonical_envelope_bytes(payload_without_signature: Mapping[str, Any]) -> bytes:
    """Return the exact canonical bytes covered by an envelope signature."""

    return json.dumps(
        dict(payload_without_signature), sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _decode_base64url(value: str, *, expected_length: int) -> bytes:
    """Strictly decode padded or unpadded URL-safe base64."""

    if not _BASE64URL_RE.fullmatch(value):
        raise ValueError("invalid base64url")

    unpadded = value.rstrip("=")
    supplied_padding = len(value) - len(unpadded)
    if len(unpadded) % 4 == 1:
        raise ValueError("invalid base64url length")
    required_padding = (-len(unpadded)) % 4
    if supplied_padding not in (0, required_padding):
        raise ValueError("invalid base64url padding")

    try:
        decoded = base64.b64decode(
            unpadded + ("=" * required_padding),
            altchars=b"-_",
            validate=True,
        )
    except (binascii.Error, ValueError) as exc:
        raise ValueError("invalid base64url") from exc

    canonical = base64.urlsafe_b64encode(decoded).decode("ascii").rstrip("=")
    if canonical != unpadded or len(decoded) != expected_length:
        raise ValueError("invalid base64url payload")
    return decoded


def _read_json_object(path: Path) -> dict[str, Any]:
    parsed = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(parsed, dict):
        raise ValueError("expected JSON object")
    return parsed


def verify_envelope_file(
    envelope_path: Path,
    public_key_path: Path,
    profiles: Mapping[str, ProfileConfig],
    *,
    now: int | None = None,
) -> VerifiedLaunchEnvelope:
    """Verify a signed envelope, raising only a stable public diagnostic."""

    try:
        raw_envelope = _read_json_object(envelope_path)
        envelope = _LaunchEnvelope.model_validate(raw_envelope)
        key_document = _PublicKeyDocument.model_validate(
            _read_json_object(public_key_path)
        )

        if envelope.issuer != key_document.issuer:
            raise ValueError("unknown issuer")
        profile = profiles.get(envelope.profile)
        if profile is None or not profile.constrained:
            raise ValueError("unknown or unconstrained profile")

        verification_time = int(time.time()) if now is None else now
        if envelope.issued_at > verification_time:
            raise ValueError("envelope issued in the future")
        if envelope.expires_at <= verification_time:
            raise ValueError("expired envelope")

        public_key = Ed25519PublicKey.from_public_bytes(
            _decode_base64url(key_document.public_key, expected_length=32)
        )
        signature = _decode_base64url(envelope.signature, expected_length=64)
        signed_payload = {
            key: value for key, value in raw_envelope.items() if key != "signature"
        }
        public_key.verify(signature, canonical_envelope_bytes(signed_payload))
    except (OSError, UnicodeError, json.JSONDecodeError, InvalidSignature, ValueError):
        raise EnvelopeValidationError() from None

    return VerifiedLaunchEnvelope(
        profile=envelope.profile,
        issuer=envelope.issuer,
        launch_id=envelope.launch_id,
        execution_id=envelope.execution_id,
        task_id=envelope.task_id,
        issued_at=envelope.issued_at,
        expires_at=envelope.expires_at,
        nonce=envelope.nonce,
    )


def _capability_policy_is_subset(
    candidate: CapabilityList, enforced: CapabilityList
) -> bool:
    # Protection changes alter which concrete global/project binding wins even
    # when the visible capability ID is unchanged. Treat only exact protection
    # parity as a subset; this is deliberately conservative.
    if set(candidate.protected) != set(enforced.protected):
        return False

    candidate_denied = set(candidate.deny).difference(candidate.protected)
    enforced_denied = set(enforced.deny).difference(enforced.protected)
    candidate_allowed = (
        None
        if candidate.include is None
        else set(candidate.include).union(candidate.protected).difference(candidate_denied)
    )
    enforced_allowed = (
        None
        if enforced.include is None
        else set(enforced.include).union(enforced.protected).difference(enforced_denied)
    )

    if enforced_allowed is None:
        if candidate_allowed is None:
            return candidate_denied.issuperset(enforced_denied)
        return candidate_allowed.isdisjoint(enforced_denied)
    if candidate_allowed is None:
        return False
    return candidate_allowed.issubset(enforced_allowed)


def _apply_project_overlay(
    policy: CapabilityList, overlay: CapabilityList | None
) -> CapabilityList:
    if overlay is None:
        return policy

    include = policy.include
    if overlay.include is not None:
        if include is None:
            include = overlay.include
        else:
            overlay_include = set(overlay.include)
            include = tuple(
                capability_id
                for capability_id in include
                if capability_id in overlay_include
            )
    return CapabilityList(
        include=include,
        deny=tuple(dict.fromkeys((*policy.deny, *overlay.deny))),
        protected=policy.protected,
    )


def profile_is_subset(
    candidate: ProfileConfig,
    enforced: ProfileConfig,
    *,
    candidate_overlay: ProfileConfig | None = None,
    enforced_overlay: ProfileConfig | None = None,
) -> bool:
    """Return whether a profile can only narrow an envelope-enforced profile."""

    return _capability_policy_is_subset(
        _apply_project_overlay(
            candidate.scope.mcp_servers,
            None
            if candidate_overlay is None
            else candidate_overlay.scope.mcp_servers,
        ),
        _apply_project_overlay(
            enforced.scope.mcp_servers,
            None
            if enforced_overlay is None
            else enforced_overlay.scope.mcp_servers,
        ),
    ) and _capability_policy_is_subset(
        _apply_project_overlay(
            candidate.scope.skills,
            None if candidate_overlay is None else candidate_overlay.scope.skills,
        ),
        _apply_project_overlay(
            enforced.scope.skills,
            None if enforced_overlay is None else enforced_overlay.scope.skills,
        ),
    )
