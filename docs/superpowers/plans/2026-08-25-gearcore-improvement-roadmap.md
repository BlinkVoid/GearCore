# GearCore Improvement Roadmap

> **For agentic workers:** This is a prioritized roadmap, not an execution-ready TDD plan.
> Each workstream gets its own execution-ready plan (via superpowers:writing-plans)
> when approved. Steps use checkbox syntax for tracking at roadmap level.

**Goal:** Clear WIP debt, harden backend resilience, and reach a tagged public release.

**Architecture:** No architectural changes required. All workstreams fit the existing
hub design (`ProcessManager` + progressive disclosure + sync). One behavioral change
(parallel backend startup) is localized to `_start_backends` in `main.py`.

**Tech Stack:** Python 3.13+, uv, pytest, mypy, ruff, MCP SDK 1.26+

## Global Constraints

- Python 3.13+ floor unchanged
- No new runtime dependencies unless discussed first
- Every workstream ends with green: `uv run pytest`, `uv run ruff check src tests`, `uv run mypy src`
- Follow repo doc discipline: functional changes update ARCHITECTURE.md / specs in same commit

---

## Assessment Reconciliation (evidence base)

| Claim | Source | Verdict |
|---|---|---|
| 94 tests pass | Other AI | True for committed tree only |
| 103 tests pass | This session | True including untracked onboard tests (delta ≈ test_onboard.py) |
| OAuth backends can hang hub startup | Other AI | **Partially stale** — `main.py:194-204` already isolates failures (15s timeout each, exceptions caught) |
| Cleanup errors left behind | Other AI | Mostly handled (`process_manager.py:119-138` suppresses known anyio noise); no test proves it |
| Token savings unmeasured | Both | Confirmed — README illustration is illustrative, not measured |
| mypy/ruff failures | This session | Real but confined to in-flight workstreams (onboard.py:474, test_update.py imports) |

---

## Workstream A — Land the two in-flight features (highest priority)

Unblocks everything else; removes all lint/type failures.

### Task A1: Finish and commit the onboard command
- [ ] Fix `mypy` errors at `src/gearcore_hub/onboard.py:474` (`str` vs `Path` args to `_dirs_equal`)
- [ ] Run full suite: `uv run pytest -q`, `uv run mypy src`, `uv run ruff check src tests`
- [ ] Commit `src/gearcore_hub/onboard.py` + `tests/test_onboard.py` with existing regression coverage
- Closes: act_549a2efe948840a6

### Task A2: Complete `gearcore update` subcommand (Tasks 3–6 of existing plan)
- [ ] Task 3: implement `update_mcp_server` + `update_all_mcp_servers` in `src/gearcore_hub/update.py`
- [ ] Task 4: skill bundle update logic (`.<name>.gearcore-update.json` sidecars)
- [ ] Task 5: wire CLI parser/dispatch in `main.py`
- [ ] Task 6: README/CHANGELOG/self-skill docs; mark spec Implemented
- [ ] Fix import sorting in `tests/unit/test_update.py` (ruff I001)
- Closes: act_146039ddbe4b47b3 … act_8a89b2572b484b39

### Task A3: Branch hygiene
- [ ] Merge `level0-skill-reveal` → `main` (already review-approved with fix)
- [ ] Delete merged branches `add-opencode-sync`, `fix-add-mcp-dashed-args`
- [ ] Remove broken symlink `builder_insights -> ~/workspace/MetaFactory/skills/builder_insights`

---

## Workstream B — Backend resilience hardening (from external review)

### Task B1: Regression test for isolated backend failure
- [ ] Test: config with one OAuth-required (hanging) stdio backend + one healthy backend
- [ ] Assert: hub starts within timeout, healthy backend's tools callable,
        failed backend returns "offline" error text, shutdown produces no unhandled cleanup errors
- Note: exercises existing behavior in `main.py:_start_backends`; test may pass immediately — that is fine, it locks the contract in

### Task B2: Parallelize backend startup
- [ ] Change `_start_backends` from sequential loop to `asyncio.gather(..., return_exceptions=True)`
- [ ] Per-server 15s timeout preserved; worst-case startup becomes max(15s) not sum(15s×N)
- [ ] Update ARCHITECTURE.md (startup data flow change)

### Task B3: Surface failed backends in `gearcore status`
- [ ] Track start failures on `ProcessManager` (e.g. `failed: dict[str, str]`)
- [ ] `cmd_status` prints `[FAILED] <id> — <reason>` alongside running servers

---

## Workstream C — Release readiness

### Task C1: README repair
- [ ] Fix `geracore` → `gearcore` typos (README quick-start examples)
- [ ] Replace placeholder install URL `github.com/yourusername/gearcore` with real repo URL or local-source instructions
- [ ] Qualify token-savings diagram as illustrative until measured, or measure it (see C3)

### Task C2: Cross-client e2e matrix
- [ ] Run `gearcore sync` + skill discovery check against Claude, Codex, Kimi, OpenCode
- [ ] Document results in README (support table) before claiming cross-client support
- Closes: act_33af2016ce604af9

### Task C3 (optional): Measure the token claim
- [ ] Script: count tokens of raw MCP configs vs gearcore self-skill for a representative setup
- [ ] Put real number in README or drop the illustration

---

## Workstream D — Small quality items (opportunistic)

- [ ] Cache `git ls-remote` result in `status` (e.g. 10-min TTL file) — closes act_ff709f29564d41c0
- [ ] Refresh vendored superpowers (`update available: b36e0829c6d0`)
- [ ] Self-hook installer discussion (act_302e6144e2c94dd1) — defer to design discussion, not this roadmap

---

## Suggested order

A (debt) → B1 (lock contract) → C1+C3 (public claims) → B2+B3 → D → tag release.

Estimated effort: A ≈ 1 session, B ≈ 1 session, C ≈ 0.5–1 session, D ≈ 0.5 session.
