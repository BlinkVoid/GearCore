"""Semantic tests: model-card research is a required web-research branch.

These tests assert structure and concept coverage, not prose wording, so
rephrasing the skill does not break them as long as meaning is preserved.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

SKILL_DIR = Path(__file__).parents[1] / "skills" / "web-research"
REFERENCE_NAME = "model-card-research.md"

STATUS_VOCABULARY = [
    "official_model_card",
    "official_variant_card",
    "derivative_model_card",
    "provider_endpoint_card",
    "creator_version_page",
    "mirror_card_incomplete",
    "no_card_found",
]

COMMON_FIELD_MARKERS = [
    "version",
    "url",
    "retrieval",
    "hash",
    "lineage",
    "modality",
    "license",
    "benchmark",
    "limitation",
]

MODALITY_GROUPS = {
    "llm": [
        "tokenizer",
        "chat template",
        "tool calling",
        "context length",
        "quantization",
    ],
    "diffusion": [
        "vae",
        "sampler",
        "cfg",
        "resolution",
        "trigger",
    ],
    "embedding": [
        "pooling",
        "dimensions",
        "prefix",
        "normalization",
    ],
}

BOUNDARY_MARKERS = [
    "fine-tune",
    "quantiz",
    "weights",
    "mirror",
]


def _skill_text() -> str:
    return (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")


def _reference_text() -> str:
    return (SKILL_DIR / "references" / REFERENCE_NAME).read_text(encoding="utf-8")


def _normalized(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower())


def test_skill_routes_ml_model_research_to_reference():
    skill = _skill_text()
    assert REFERENCE_NAME in skill
    skill_lower = _normalized(skill)
    assert "model" in skill_lower
    assert re.search(r"llm|diffusion|embedding", skill_lower), (
        "routing must name modalities so any-model coverage is explicit"
    )
    # The protocol must apply before conclusions/recommendations.
    routing_idx = skill_lower.find(REFERENCE_NAME.lower())
    assert routing_idx != -1
    tail = skill_lower[routing_idx:]
    assert "before" in tail, (
        "routing must state the protocol precedes conclusions/recommendations"
    )


def test_reference_relative_link_resolves():
    skill = _skill_text()
    matches = re.findall(r"\((references/[^)]+\.md)\)", skill)
    assert matches, "SKILL.md should link the reference by relative path"
    for relative in matches:
        assert (SKILL_DIR / relative).is_file(), relative


def test_reference_defines_model_card_broadly():
    reference = _normalized(_reference_text())
    for marker in [
        "model card",
        "readme",
        "modelscope",
        "civitai",
        "endpoint",
        "not",
    ]:
        assert marker in reference, f"definition should cover {marker!r}"


def test_reference_source_hierarchy_and_search_strategy():
    raw = _reference_text()
    reference = _normalized(raw)
    assert "revision" in reference
    assert "history" in reference or "files" in reference
    section = re.search(
        r"#+\s*source hierarchy(.*?)(?=\n#+\s)", raw, re.DOTALL | re.IGNORECASE
    )
    assert section, "must have a dedicated 'Source hierarchy' section"
    text = _normalized(section.group(1))
    positions = []
    for tier in ["creator", "official", "derivative", "provider", "mirror"]:
        idx = text.find(tier)
        assert idx != -1, f"hierarchy tier missing: {tier}"
        positions.append(idx)
    assert positions == sorted(positions), (
        "source hierarchy tiers must appear in priority order"
    )
    assert "quant" in text, "derivative/quantization tier must be explicit"


def test_reference_boundary_rules():
    reference = _normalized(_reference_text())
    for marker in BOUNDARY_MARKERS:
        assert marker in reference, f"boundary rule should mention {marker!r}"
    assert (
        "not proof" in reference
        or "does not prove" in reference
        or "not evidence" in reference
    )
    assert "hash" in reference and "behavior" in reference, (
        "local hashes prove bytes, not published behavior"
    )
    assert "conflict" in reference and "gap" in reference


def test_reference_common_capture_fields():
    reference = _normalized(_reference_text())
    for marker in COMMON_FIELD_MARKERS:
        assert marker in reference, f"common capture field missing: {marker!r}"


def test_reference_covers_each_modality_group():
    reference = _normalized(_reference_text())
    for group, markers in MODALITY_GROUPS.items():
        for marker in markers:
            assert marker in reference, f"{group} fields missing: {marker!r}"
    assert "reranker" in reference or "rerank" in reference
    assert "negative" in reference  # diffusion negative baseline
    assert "audio" in reference and "video" in reference and "image" in reference


def test_reference_missing_card_rule_blocks_invention():
    reference = _normalized(_reference_text())
    assert "missing" in reference
    assert "infer" in reference, "must forbid inference from absent evidence"
    assert "status" in reference


def test_reference_has_ascii_flow_with_gates_and_failure_edges():
    reference = _reference_text()
    code_blocks = re.findall(r"```[a-z]*\n(.*?)```", reference, re.DOTALL)
    assert code_blocks, "reference must include an ASCII flow in a code block"
    flow = max(code_blocks, key=len)
    assert "->" in flow or "|" in flow, "flow must be plain ASCII"
    failure_markers = [
        m for m in ("no_card_found", "missing", "fail", "stop") if m in flow.lower()
    ]
    assert failure_markers, "flow must show failure edges, not only success"
    assert flow.isascii(), "diagrams must be plain ASCII"


def test_reference_status_vocabulary_complete():
    reference = _reference_text()
    for status in STATUS_VOCABULARY:
        assert status in reference, f"status missing: {status}"
    reference_lower = _normalized(reference)
    assert "source" in reference_lower, (
        "statuses must be explained as classifying the source, not model quality"
    )


def test_reference_has_reusable_output_contract():
    reference = _reference_text()
    assert re.search(r"^\|.*\|$", reference, re.MULTILINE), (
        "output contract should be a reusable table"
    )


def test_manifest_remains_valid_json_with_research_trigger():
    manifest = json.loads((SKILL_DIR / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["name"] == "web-research"
    triggers = " ".join(manifest["activation"]["triggers"]).lower()
    assert "research" in triggers or "model" in triggers
