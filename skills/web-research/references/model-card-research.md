# Model-Card Research Protocol

Required branch of web research whenever the subject is a machine-learning model, checkpoint, adapter, or hosted endpoint, regardless of modality. Apply this protocol before drawing conclusions or making recommendations.

## What counts as a model card

A model card is the primary publisher's model-specific artifact. It does **not** need to be literally titled "Model Card". It may be:

- a Hugging Face or ModelScope repository README,
- a CivitAI creator version page,
- a vendor release page,
- a provider endpoint card for a hosted API.

## Source hierarchy

Search by the exact organization/model/version, then inspect repository files, history, and revisions, and follow links to the publisher's own sources. Prefer immutable revision URLs when available, and always record the retrieval date.

1. Exact creator/revision card for that exact model and version.
2. Official code, templates, paper, and license published by the same party.
3. Exact derivative or quantization card (the specific artifact actually in use).
4. Hosted provider endpoint documentation.
5. Community mirror or secondary source.

## Boundary rules

- The upstream/base card is not proof of a fine-tune, quantization, local bytes, LoRA, or a hosted runtime.
- A derivative or quantized checkpoint differs from its base; base-model evidence does not transfer automatically.
- Provider docs describe an API surface, not hidden weights.
- A mirror is not original authority.
- Local hashes prove bytes, not published behavior.
- Conflicts and gaps stay explicit; never silently resolve them.

## Output contract (reusable capture checklist)

Record every field the evidence supports; mark the rest as unavailable.

| Field | Requirement |
| --- | --- |
| Canonical identity | Organization, model id, exact version/revision |
| Card URL | Canonical card location |
| Provenance | Immutable revision URL, or retrieval date if none exists |
| Artifacts | Names and hashes if published, or hashes of locally bound bytes |
| Lineage | Base model, fine-tune/variant relationship |
| Task/modality | Intended task and modality coverage |
| Input contract | Prompt template, grammar, or required input format |
| Runtime | Components, defaults, and dependencies |
| License/usage | License terms and usage restrictions |
| Evaluations | Benchmarks and reported scores, with who ran them |
| Limitations | Safety, bias, and known failure modes |
| Conflicts/gaps | Disagreements between sources and missing data, left explicit |

### LLM and vision-language (VLM)

Tokenizer; chat template and system prompt; tool calling (function calling); context length; decoding defaults; quantization and runtime compatibility.

### Diffusion, image, video, audio

Text/image encoders; VAE, decoder, or vocoder; sampler/scheduler; steps; CFG/guidance scale; resolution, aspect, frame, and audio limits; prompt grammar including quality tags, trigger tokens, negative baseline, and reference roles (IP-adapter/control references).

### Embedding and reranking

Pooling strategy; output dimensions; max input length; query/document prefixes; normalization; scoring/calibration.

## Missing, incomplete, or inaccessible cards

Record the status and the unavailable fields. Do not infer or invent values, and block any claim that requires the absent evidence. A model may have only a creator page or an incomplete mirror rather than a classic card; that is a finding, not a failure of the procedure.

## Status vocabulary

Assign exactly one status to the best source found. Statuses classify the **source**, not model quality.

| Status | Meaning |
| --- | --- |
| `official_model_card` | Publisher's card for the exact model |
| `official_variant_card` | Publisher's card for an official variant of the exact model |
| `derivative_model_card` | Card for a derivative/quantization of the model |
| `provider_endpoint_card` | Hosted provider's endpoint documentation |
| `creator_version_page` | Creator's version page without a full card (e.g. CivitAI) |
| `mirror_card_incomplete` | Community mirror with incomplete information |
| `no_card_found` | No usable card source located |

## Procedure

1. Identify the exact subject: organization, model id, version/revision.
2. Search for the exact creator/revision card first (Hugging Face/ModelScope README, CivitAI version page, vendor release page, or provider endpoint card).
3. Inspect repository files, history, and revisions; follow links to official code, templates, paper, and license.
4. If the exact card is absent, walk down the source hierarchy and assign a status from the vocabulary.
5. Capture the output-contract fields the evidence supports; prefer immutable revision URLs and record the retrieval date.
6. Record conflicts and gaps explicitly; mark unavailable fields; do not infer or invent.
7. Only then draw conclusions or recommendations.

```text
  Identify exact subject (org / model / version)
        |
        v
  [gate] Exact creator/revision card found? --no--> Walk hierarchy downward
        | yes                                     (derivative card, provider
        v                                          docs, community mirror)
  Inspect files / history / revision                     |
  and follow linked official sources                     |
        |                                                |
        +-------------------+----------------------------+
                            v
  [gate] Enough evidence for the required fields? --no--> Assign status
        | yes                                        (mirror_card_incomplete
        v                                             or no_card_found;
  Capture output-contract fields                      stop blocked claims)
  for the actual modality                                      |
        |                                                      |
        v                                                      |
  [gate] Conflicts or gaps across sources? --yes--> Record explicitly,
        | no                                        never silently resolve
        +-----------------------+--------------------------+
                                v
        Conclusions and recommendations AFTER the protocol
```
