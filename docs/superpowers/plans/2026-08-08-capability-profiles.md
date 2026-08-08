# GearCore Capability Profiles Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add generic, cwd-independent operator/worker capability profiles, protected global capabilities, signed constrained contexts, and secret-safe authenticated MCP transports to GearCore.

**Architecture:** Parse version-3 policy into immutable profile models, resolve one effective profile before any command or backend starts, and keep version-2 behavior unchanged when no version-3 global policy exists. Separate envelope verification, credential loading, and transport construction into focused modules; `EffectiveConfig` is the only capability view consumed by skill and process managers.

**Tech Stack:** Python 3.13+, Pydantic 2, PyYAML, `cryptography` Ed25519, MCP Python SDK 1.26, pytest/pytest-asyncio.

**Spec:** `docs/superpowers/specs/2026-08-08-capability-profiles-design.md` at `db93b28`.

---

## File map

- Create `src/gearcore_hub/profiles.py`: profile scope models, protected-entry resolution, and no-broader comparison.
- Create `src/gearcore_hub/envelope.py`: canonical envelope parsing and Ed25519 verification.
- Create `src/gearcore_hub/credentials.py`: safe file-backed credential lookup and transport auth materialization.
- Modify `src/gearcore_hub/config.py`: v2/v3 parsing and immutable effective configuration.
- Modify `src/gearcore_hub/skill_manager.py`: deny/protected visibility and collision handling.
- Modify `src/gearcore_hub/process_manager.py`: stdio/SSE/streamable-HTTP authenticated sessions.
- Modify `src/gearcore_hub/main.py`: profile/context CLI, diagnostics, and status output.
- Modify `src/gearcore_hub/registry.py`: idempotent profile mutation used by integrations.
- Modify `pyproject.toml`, `CONFIG_SCHEMA.md`, `README.md`: dependency and public contract.
- Create `tests/test_profiles.py`, `tests/test_envelope.py`, `tests/test_credentials.py`, `tests/test_process_auth.py`, `tests/test_profile_conformance.py`.
- Modify `tests/test_config.py`, `tests/test_skill_manager.py`, `tests/test_cli_parser.py`, `tests/test_registry.py`.

### Task 1: Version-3 profile schema and v2 compatibility

**Files:**
- Create: `src/gearcore_hub/profiles.py`
- Modify: `src/gearcore_hub/config.py`
- Create: `tests/test_profiles.py`
- Modify: `tests/test_config.py`

- [ ] **Step 1: Write failing schema and compatibility tests**

Cover: v2 implicit `default`; v3 configured `operator`; unknown default rejection; `include`, `deny`, and `protected` parsing; wholly-v2 project narrowing unchanged.

```python
def test_v2_maps_to_implicit_default_without_changing_allowlist():
    effective = EffectiveConfig(v2_global(), v2_project(include=["fs"]), ROOT)
    assert effective.profile_name == "default"
    assert [server.id for server in effective.mcp_servers] == ["fs"]

def test_v3_selects_operator_without_cwd_authority():
    effective = resolve_config(v3_global(default="operator"), project=v2_project([]))
    assert effective.profile_name == "operator"
    assert effective.profile_source == "default"
```

- [ ] **Step 2: Run tests and verify RED**

Run: `uv run pytest tests/test_profiles.py tests/test_config.py -q`
Expected: FAIL because profile models and metadata do not exist.

- [ ] **Step 3: Implement focused profile models**

Implement these public shapes in `profiles.py`:

```python
class CapabilityList(BaseModel):
    include: list[str] | None = None
    deny: list[str] = Field(default_factory=list)
    protected: list[str] = Field(default_factory=list)

class CapabilityScope(BaseModel):
    mcp_servers: CapabilityList = Field(default_factory=CapabilityList)
    skills: CapabilityList = Field(default_factory=CapabilityList)

class ProfileConfig(BaseModel):
    constrained: bool = False
    scope: CapabilityScope = Field(default_factory=CapabilityScope)
    disclosure: DisclosureConfig = Field(default_factory=DisclosureConfig)

class ProfilesConfig(BaseModel):
    default: str
    entries: dict[str, ProfileConfig]
```

Add `GlobalConfig.profiles`, reject unsupported versions, and construct the v2 implicit profile only for version 2. Preserve existing `EffectiveConfig` constructor compatibility for current callers.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run: `uv run pytest tests/test_profiles.py tests/test_config.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/gearcore_hub/profiles.py src/gearcore_hub/config.py tests/test_profiles.py tests/test_config.py
git commit -m "feat: add versioned capability profile schema"
```

### Task 2: Protected and denied MCP/skill resolution

**Files:**
- Modify: `src/gearcore_hub/profiles.py`
- Modify: `src/gearcore_hub/config.py`
- Modify: `src/gearcore_hub/skill_manager.py`
- Modify: `tests/test_profiles.py`
- Modify: `tests/test_skill_manager.py`

- [ ] **Step 1: Write failing adversarial resolution tests**

Test a v3 global profile protecting both `hive-dispatcher` server and skill against: a restrictive v2 project include, project deny, same-ID server definition, and same-name local skill. Also prove non-protected deny wins and a worker profile cannot see denied capabilities.

```python
def test_protected_global_survives_v2_project_collision(tmp_path):
    cfg = load_fixture(global_v3=OPERATOR, project_v2=COLLIDING_PROJECT)
    assert cfg.server("hive-dispatcher").command == "/trusted/hive"
    manager = SkillManager(cfg)
    assert manager.get_skill("hive-dispatcher").is_project_local is False
    assert "protected_capability_override" in cfg.diagnostic_codes
```

- [ ] **Step 2: Run tests and verify RED**

Run: `uv run pytest tests/test_profiles.py tests/test_skill_manager.py -q`
Expected: FAIL because current project definitions shadow globals and local skills overwrite globals.

- [ ] **Step 3: Implement deterministic resolution**

Add a pure resolver returning `ResolvedCapabilities(active, denied, protected, diagnostics)`. Resolve protected global bindings first, apply project context only to non-protected IDs, then deny non-protected IDs last. In `SkillManager._load_bundle`, ignore a project-local bundle whose name is protected and preserve the already-loaded global bundle. Include protected globals in visibility even when a v2 project allowlist omits them.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run: `uv run pytest tests/test_profiles.py tests/test_skill_manager.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/gearcore_hub/profiles.py src/gearcore_hub/config.py src/gearcore_hub/skill_manager.py tests/test_profiles.py tests/test_skill_manager.py
git commit -m "feat: enforce protected and denied capabilities"
```

### Task 3: Ed25519 constrained launch envelopes

**Files:**
- Create: `src/gearcore_hub/envelope.py`
- Modify: `src/gearcore_hub/config.py`
- Modify: `src/gearcore_hub/main.py`
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Create: `tests/test_envelope.py`
- Modify: `tests/test_cli_parser.py`

- [ ] **Step 1: Add `cryptography` and write failing verification tests**

Test canonical signing, tamper rejection, expiry, unknown issuer/profile, invalid base64, missing file, and a valid `hive-worker` selection. Test that an explicitly supplied invalid envelope yields diagnostic-only configuration and never falls back to `operator`.

```python
def test_invalid_supplied_envelope_never_falls_back_to_operator(keys, clock):
    result = resolve_launch(global_cfg=OPERATOR_CFG, envelope=TAMPERED,
                            public_key=keys.public, now=clock)
    assert result.diagnostic_only is True
    assert result.mcp_servers == []
    assert result.profile_source == "invalid-envelope"
```

- [ ] **Step 2: Run tests and verify RED**

Run: `uv sync --extra dev && uv run pytest tests/test_envelope.py tests/test_cli_parser.py -q`
Expected: FAIL because envelope support and CLI flags do not exist.

- [ ] **Step 3: Implement canonical verification and launch selection**

Use `json.dumps(payload_without_signature, sort_keys=True, separators=(",", ":"))`. Decode URL-safe base64 with strict padding normalization and call `Ed25519PublicKey.verify`. Add `--config`, `--profile`, `--context-envelope`, and `--envelope-public-key`. A valid envelope is authoritative; a requested alternate profile is allowed only when `candidate_capabilities <= enforced_capabilities`. Invalid explicit input returns a stable diagnostic-only result.

- [ ] **Step 4: Expose diagnostic-only behavior consistently**

`status` reports the stable diagnostic. `call` exits non-zero. `serve` exposes only built-in `list_skills`, `request_skill`, and `capability_diagnostic`; it starts no backend.

- [ ] **Step 5: Run focused tests and verify GREEN**

Run: `uv run pytest tests/test_envelope.py tests/test_cli_parser.py tests/test_profiles.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml uv.lock src/gearcore_hub/envelope.py src/gearcore_hub/config.py src/gearcore_hub/main.py tests/test_envelope.py tests/test_cli_parser.py tests/test_profiles.py
git commit -m "feat: verify constrained launch envelopes"
```

### Task 4: Safe credential references

**Files:**
- Create: `src/gearcore_hub/credentials.py`
- Modify: `src/gearcore_hub/config.py`
- Create: `tests/test_credentials.py`
- Modify: `tests/test_config.py`

- [ ] **Step 1: Write failing credential-safety tests**

Cover regular owner-only file success; symlink, directory, foreign owner, group/other mode, path traversal, missing credential, plaintext-in-YAML, and wrong auth/transport combinations. Assert token text never appears in model dumps, repr, status, logs, or exceptions.

- [ ] **Step 2: Run tests and verify RED**

Run: `uv run pytest tests/test_credentials.py tests/test_config.py -q`
Expected: FAIL because auth models and credential store do not exist.

- [ ] **Step 3: Implement the store and auth schema**

```python
class McpAuthConfig(BaseModel):
    credential_ref: str
    stdio_environment: str = ""
    http_scheme: Literal["bearer", ""] = ""

class CredentialStore:
    def read(self, credential_id: str) -> SecretStr:
        # reject separators; lstat; require regular non-symlink, current uid,
        # mode & 0o077 == 0; read once; reject empty
```

Default the store to `~/.config/gearcore/credentials`; permit an injected root for tests. Keep secret values out of `McpServerConfig` and `EffectiveConfig`.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run: `uv run pytest tests/test_credentials.py tests/test_config.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/gearcore_hub/credentials.py src/gearcore_hub/config.py tests/test_credentials.py tests/test_config.py
git commit -m "feat: load MCP credentials from safe references"
```

### Task 5: Authenticated stdio, SSE, and streamable HTTP

**Files:**
- Modify: `src/gearcore_hub/process_manager.py`
- Modify: `src/gearcore_hub/main.py`
- Create: `tests/test_process_auth.py`

- [ ] **Step 1: Write failing transport-construction tests**

Monkeypatch MCP client context managers and prove: stdio merges one secret into the child environment; SSE sends `Authorization: Bearer ...`; `http` uses `streamablehttp_client`, not `sse_client`; missing credentials prevent startup; no secret reaches logger calls.

- [ ] **Step 2: Run tests and verify RED**

Run: `uv run pytest tests/test_process_auth.py -q`
Expected: FAIL because HTTP is currently routed through SSE and no auth resolver exists.

- [ ] **Step 3: Implement one authenticated transport factory**

Pass `McpServerConfig` plus `CredentialStore` into `SharedMCPServer`. Materialize auth only inside `start()`. For stdio build the child environment without mutating `os.environ`; for SSE pass headers to `sse_client`; for HTTP use `streamablehttp_client` and ignore its optional session-id callback value when constructing `ClientSession`.

- [ ] **Step 4: Make hub and one-shot calls share the factory**

Remove direct `SharedMCPServer(...)` reconstruction from `cmd_call`; use `ProcessManager.build_server()` so `call` and `serve` cannot diverge.

- [ ] **Step 5: Run focused tests and verify GREEN**

Run: `uv run pytest tests/test_process_auth.py tests/test_cli_parser.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/gearcore_hub/process_manager.py src/gearcore_hub/main.py tests/test_process_auth.py tests/test_cli_parser.py
git commit -m "feat: authenticate MCP client transports"
```

### Task 6: Idempotent profile registry and operator-facing status

**Files:**
- Modify: `src/gearcore_hub/registry.py`
- Modify: `src/gearcore_hub/main.py`
- Modify: `tests/test_registry.py`
- Modify: `tests/test_cli_parser.py`

- [ ] **Step 1: Write failing idempotency and redaction tests**

Add tests for `gearcore profile-set operator ...`, repeated identical application, protected MCP/skill pairing, constrained `hive-worker`, and a conflicting protected replacement. Status must show profile/source/constrained/active/denied/diagnostics but no credential value.

- [ ] **Step 2: Run tests and verify RED**

Run: `uv run pytest tests/test_registry.py tests/test_cli_parser.py -q`
Expected: FAIL because profile mutation and status fields do not exist.

- [ ] **Step 3: Implement atomic v3 mutation**

Add `set_profile(...)` using the registry module's existing YAML mutation pattern and same-directory atomic replace. The command accepts repeatable include/deny/protect options for MCPs and skills, `--core-skill`, `--constrained`, and `--default`. Reject protecting an ID without a matching global definition/bundle and reject project-scoped protection.

- [ ] **Step 4: Implement status metadata**

Print stable fields suitable for live gates: `profile`, `source`, `constrained`, active/denied/protected IDs, and diagnostic codes. Never dump `auth` material beyond credential IDs.

- [ ] **Step 5: Run focused tests and verify GREEN**

Run: `uv run pytest tests/test_registry.py tests/test_cli_parser.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/gearcore_hub/registry.py src/gearcore_hub/main.py tests/test_registry.py tests/test_cli_parser.py
git commit -m "feat: configure and report capability profiles"
```

### Task 7: Cross-command and cross-transport profile conformance

**Files:**
- Create: `tests/test_profile_conformance.py`
- Modify: `src/gearcore_hub/main.py`
- Modify: `src/gearcore_hub/process_manager.py`
- Modify: `src/gearcore_hub/skill_manager.py`

- [ ] **Step 1: Write one parameterized constrained-profile harness**

Create a version-3 `hive-worker` fixture that includes a gateway, explicitly
denies dispatcher MCP/skill, and still contains hostile dispatcher definitions
for `stdio`, `sse`, and `http`. Parameterize the same fixture across `status`,
`list-skills`, `request-skill`, `call`, and `serve`.

```python
@pytest.mark.parametrize("transport", ["stdio", "sse", "http"])
@pytest.mark.parametrize(
    "surface", ["status", "list-skills", "request-skill", "call", "serve"]
)
def test_constrained_profile_denial_is_identical_everywhere(
    transport, surface, worker_context, backend_spy
):
    result = exercise_surface(surface, worker_context.for_transport(transport))
    assert "hive-dispatcher" not in result.visible_capabilities
    assert result.denial_code == expected_denial(surface)
    assert backend_spy.starts("hive-dispatcher") == 0
```

For `serve`, drive the in-process MCP handlers through a client session and
assert dispatcher tools are absent before and after skill requests. For CLI
commands, invoke `main()` with patched argv and capture exit/status output.

- [ ] **Step 2: Run the conformance matrix and verify RED**

Run: `uv run pytest tests/test_profile_conformance.py -q`
Expected: at least one command/transport path bypasses the single effective
profile until the shared resolver wiring is complete.

- [ ] **Step 3: Remove command-specific resolution paths**

Build `EffectiveConfig` exactly once in `main()` and pass it unchanged to every
surface. Require `ProcessManager`, `SkillManager`, one-shot calls, and hub serve
handlers to consume only that object; no command may reload global/project
configuration or construct a server from raw registry data.

- [ ] **Step 4: Run the conformance matrix and verify GREEN**

Run: `uv run pytest tests/test_profile_conformance.py -q`
Expected: PASS for all 15 profile/transport/surface combinations, with zero
dispatcher process starts.

- [ ] **Step 5: Commit**

```bash
git add tests/test_profile_conformance.py src/gearcore_hub/main.py src/gearcore_hub/process_manager.py src/gearcore_hub/skill_manager.py
git commit -m "test: enforce capability profiles across every surface"
```

### Task 8: Documentation, compatibility, and release gate

**Files:**
- Modify: `CONFIG_SCHEMA.md`
- Modify: `README.md`
- Modify: `src/gearcore_hub/self_skill/SKILL.md`
- Modify: `src/gearcore_hub/self_skill/manifest.json`

- [ ] **Step 1: Document v3 and migration examples**

Document v2 preservation, v3 profiles, protected precedence over v2/v3 projects, envelope CLI, credential permissions, diagnostic failures, and authenticated transport syntax. State that profiles are policy selection while server authentication remains the hostile-process boundary.

- [ ] **Step 2: Run static and full verification**

Run:

```bash
uv run ruff check src tests
uv run mypy src/gearcore_hub
uv run pytest -q
git diff --check
```

Expected: all checks pass; test count is at least the 70-test baseline plus new coverage.

- [ ] **Step 3: Run explicit v2 smoke test**

Run GearCore from a neutral temporary directory with a version-2 global and restrictive project config. Expected: the output matches legacy filtering and contains `profile: default` without v3 diagnostics.

- [ ] **Step 4: Commit**

```bash
git add CONFIG_SCHEMA.md README.md src/gearcore_hub/self_skill/SKILL.md src/gearcore_hub/self_skill/manifest.json
git commit -m "docs: publish capability profile contract"
```

- [ ] **Step 5: Push the implementation branch and record the locked GearCore commit**

Run: `git push origin hive/gearcore-capability-profiles-20260808`
Expected: the remote branch advances cleanly. Record the resulting SHA as the dependency input to the HIVE integration plan; do not point HIVE at an uncommitted GearCore tree.
