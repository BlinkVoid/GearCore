# GearCore Capability Profiles and HIVE Role Isolation

**Status:** approved design; implementation not started
**Date:** 2026-08-08
**GearCore branch:** `hive/gearcore-capability-profiles-20260808`
**Integrates with:** HIVE dispatcher and worker launch paths

## 1. Problem

GearCore currently resolves one static global/project MCP allowlist. HIVE needs
two incompatible capability views:

- a human-started GenAI CLI is an operator and should have HIVE dispatcher
  capabilities by default, inside or outside the HIVE repository;
- a HIVE-launched GenAI CLI is a worker and must have gateway participation
  capabilities without dispatcher capabilities.

Working directory cannot distinguish them. A worker can legitimately run inside
HIVE or a HIVE Honeycomb worktree, while a human may launch Codex, Claude, Kimi,
OpenCode, or another GearCore-enabled CLI from any unrelated directory.

Today HIVE solves parts of this separately for each CLI: generated MCP configs,
Claude allowlists, isolated homes, and prompt text saying not to orchestrate.
The comments correctly identify dispatcher inheritance as an infinite-spawn
hazard, but prompt obedience and per-client filtering are not an authority
boundary. This design moves capability selection into GearCore and adds a
server-side HIVE check so tool visibility is not the only defence.

## 2. Decision

Add generic **capability profiles** to GearCore.

The absence of a constrained launch envelope means `operator`. This is the
global default for a human-started CLI. An operator profile can include HIVE's
dispatcher MCP and its level-0 dispatcher skill regardless of current project.

HIVE launches every worker with an Ed25519-signed, short-lived worker envelope
and a worker-specific GearCore context. That constrained context exposes the
HIVE gateway worker skill and denies the dispatcher server and skill.

HIVE's dispatcher independently requires per-client operator authentication
unavailable in worker sandboxes. A hidden tool remains unusable if accidentally
disclosed; a worker also cannot regain dispatcher authority by deleting an
environment variable, starting another GearCore process, changing directories,
editing its prompt, or connecting to an already-running network transport.

GearCore contains no hardcoded HIVE role logic. Profiles, constrained envelopes,
explicit denies, and contextual skill disclosure are generic features usable by
other projects.

## 3. Trust model

### 3.1 Operator default

The local OS account is the operator boundary. A GenAI CLI launched directly by
the human, with no valid constrained envelope, resolves GearCore's configured
default profile: `operator`.

This is deliberate fail-open-to-operator behaviour for the human's own process,
not a claim that absence of metadata proves a human. Safety comes from keeping
untrusted HIVE workers inside a process/filesystem boundary where the operator
credential and global GearCore configuration are absent.

### 3.2 Worker downgrade

A HIVE worker is an untrusted child process. WorkerSpawner creates:

- an Ed25519-signed capability envelope naming `profile=hive-worker`, launch ID,
  execution/task identity when known, issue time, expiry, and nonce;
- a generated GearCore context containing only the worker profile's permitted
  servers and skills;
- a provider-neutral filesystem and process namespace that cannot read the
  operator GearCore config, dispatcher credential, or operator state.

The envelope can narrow capability only. A CLI argument or project config may
select an equally or more restrictive profile; neither may broaden a valid
enforced profile.

### 3.3 Server-side enforcement

The HIVE dispatcher MCP requires an operator credential for every client
session and tool call. GearCore supplies it only for the operator profile from
a local credential reference. Worker contexts contain neither the credential
value nor the credential source.

The dispatcher refuses missing, invalid, expired, or worker-role credentials
before pool creation or execution admission. This makes accidental tool
disclosure noisy and harmless instead of recursive.

## 4. GearCore configuration model

Configuration version advances from 2 to 3. Version 2 remains supported and
maps to one implicit `default` profile with current behaviour.

Example global configuration:

~~~yaml
version: 3

profiles:
  default: operator
  entries:
    operator:
      scope:
        mcp_servers:
          include: [hive-dispatcher, filesystem, chrono-core]
          protected: [hive-dispatcher]
        skills:
          include: [hive-dispatcher, chrono-core, memory]
          protected: [hive-dispatcher]
      disclosure:
        core_skills: [hive-dispatcher, chrono-core]

    hive-worker:
      constrained: true
      scope:
        mcp_servers:
          include: [hive-gateway]
          deny: [hive-dispatcher]
        skills:
          include: [hive-worker]
          deny: [hive-dispatcher]
      disclosure:
        core_skills: [hive-worker]

registry:
  mcp_servers:
    - id: hive-dispatcher
      type: stdio
      command: uv
      args: [--project, /absolute/path/to/HIVE, run, --extra, server,
             python, -m, hive.server.server]
      auth:
        credential_ref: hive-dispatcher-operator
        stdio_environment: HIVE_DISPATCHER_CREDENTIAL
~~~

### 4.1 Profile fields

Each profile may define:

- `constrained`: whether a signed launch envelope may enforce it;
- MCP `include` and `deny` lists;
- skill `include` and `deny` lists;
- global-only `protected` MCP and skill IDs that a project cannot hide, deny,
  shadow, or redefine;
- profile-specific disclosure/core skills;
- optional project-context overlays.

For non-protected entries, an explicit deny always wins over includes,
project-local definitions, skill activation, and inherited defaults. Denied
servers are not started, listed, or callable. Denied skills are not visible or
loadable, including project-local skills with the same name.

Only the global configuration may declare or change a protected entry. A
project deny, restrictive allowlist, or same-ID definition cannot affect it.
GearCore ignores the attempted override, preserves the global binding, and
reports a stable `protected_capability_override` diagnostic. Disabling or
replacing such a capability requires an intentional global configuration
change or uninstall. This is generic policy pinning, not HIVE-specific logic.

Profiles do not inherit by default. A later slice may add explicit inheritance
if real duplication justifies it; implicit inheritance is too easy to turn into
authority leakage.

### 4.2 Resolution order

1. Load global and optional project configuration.
2. Validate configuration version and all named profiles.
3. Validate any launch envelope.
4. If a valid constrained envelope exists, select that profile and record it as
   enforced. Otherwise select the configured default profile.
5. Resolve and pin protected global definitions for the selected profile.
6. Apply global profile scope.
7. Apply the project's overlay for that same profile, if present. Version-3
   overlays add context and may narrow non-protected entries; they cannot alter
   a protected binding. A version-3 global protected binding also takes
   precedence over a version-2 project allowlist, deny, or same-ID definition.
   A wholly version-2 global/project configuration has no protected entries and
   retains its current narrowing semantics unchanged.
8. Apply non-protected denies last.
9. Produce an immutable effective configuration containing the profile name,
   source (`default` or `envelope`), and enforcement metadata.

Project auto-detection supplies context, never authority. Nested project roots
may change project overlays but cannot replace an enforced profile.

### 4.3 CLI surface

- `gearcore --profile <name>` selects a profile only when no stricter envelope
  is enforced.
- `gearcore --context-envelope <path>` is intended for trusted launchers.
- `gearcore status` reports effective profile, selection source, constrained
  status, and active/denied servers without secret material.
- `gearcore call` and `gearcore serve` use the same resolved profile.
- Attempts to broaden an enforced profile exit non-zero with a stable error.

`--profile` is convenience, not authentication. It cannot confer access to a
credential the process cannot read.

## 5. Envelope format and verification

The envelope is canonical JSON:

~~~json
{
  "version": 1,
  "profile": "hive-worker",
  "issuer": "hive-worker-spawner",
  "launch_id": "launch-...",
  "execution_id": "...",
  "task_id": "...",
  "issued_at": 0,
  "expires_at": 0,
  "nonce": "...",
  "signature": "base64url..."
}
~~~

HIVE signs canonical JSON with an Ed25519 private key held in operator state.
GearCore verifies the detached signature with the corresponding public key,
which the launcher mounts with the envelope as read-only files. The private key
is never mounted into the worker. Verification covers version, profile, issuer,
launch identity, issue/expiry times, and nonce. The implementation uses one
reviewed Ed25519 library rather than a custom cryptographic construction.

The envelope constrains the launcher-created GearCore process and prevents
configuration mistakes from broadening it. It is not the sole hostile-process
boundary: an untrusted worker can execute its own client, so filesystem secret
isolation plus dispatcher authentication remains authoritative even if the
worker ignores GearCore entirely.

Envelope replay after expiry is rejected. Reuse within its lifetime does not
grant more authority than the same constrained profile, but HIVE records nonce
and launch identity for audit. The first implementation need not maintain a
central nonce revocation database; short expiry and server-side dispatcher
credentials are the hard boundary.

Malformed or invalid envelopes fail closed: GearCore starts with no MCP servers
and exposes only a diagnostic skill. It never falls back to operator when an
envelope was explicitly supplied but failed validation.

## 6. Credential references

GearCore adds transport-aware credential references to MCP server definitions
rather than placing secrets directly in YAML:

~~~yaml
auth:
  credential_ref: credential-id
  stdio_environment: ENVIRONMENT_VARIABLE_NAME
  http_scheme: bearer
~~~

A server uses only the field appropriate to its configured transport. Stdio
credentials are carried in the private child environment; HTTP and SSE use the
authorization scheme. Schema validation rejects plaintext credential values
and an authentication mapping incompatible with the selected transport.

The initial credential provider is a file-backed store under
`~/.config/gearcore/credentials/`, with each file required to be a regular file
owned by the current user and inaccessible to group/other. GearCore reads the
value only when establishing an authenticated client session. For stdio it is
injected into the private child session environment; for HTTP/SSE it is sent as
an authorization header on every request and is never placed in the server
process environment. It is never included in status, logs, synced skills,
generated worker contexts, URLs, or process command arguments.

Missing or unsafe credential material prevents that server from starting. It
does not silently start without authentication.

Worker sandboxes do not mount the operator credential directory. A copied
global config therefore still cannot produce an authenticated dispatcher.

## 7. HIVE integration

### 7.1 Operator installation

HIVE provides an idempotent install command that:

1. registers the absolute HIVE dispatcher command globally in GearCore;
2. installs a global `hive-dispatcher` skill whose manifest exposes the
   dispatcher tool set;
3. adds that skill to the operator profile's protected level-0 skills, paired
   with the protected dispatcher MCP binding;
4. creates or verifies the operator credential file without printing it;
5. writes only the corresponding credential digest/identifier into HIVE
   operator state under `~/.local/state/hive/operator/`, outside the source
   repository;
6. validates a real authenticated read-only dispatcher call.

This makes HIVE dispatcher capabilities available to any human-started,
GearCore-enabled GenAI CLI from any directory.

### 7.2 Human dispatcher launch

`scripts/start.py` configures the chosen GenAI CLI with GearCore as the single
HIVE capability entrypoint. During a transition release it may retain the
direct `hive-dispatcher` MCP for rollback, but the final gate removes duplicate
direct wiring after every supported CLI proves GearCore operator access.

Human launches carry no worker envelope and therefore resolve the operator
profile. HIVE project configuration may add project skills, but the protected
global binding means restrictive project allowlists and project-local name
collisions cannot hide or replace the operator dispatcher.

### 7.3 Worker launch

WorkerSpawner writes the signed envelope and a generated version-3 GearCore
context inside the launch runtime directory. Codex, Claude, Kimi, Gemini, Kiro,
and OpenCode use that same context contract even though their native MCP config
formats differ.

Each generated client config starts GearCore with the envelope explicitly. The
only direct HIVE MCP allowed beside GearCore during transition is
`hive-gateway`; the dispatcher is never written into worker configs.

Worker filesystem containment is provider-neutral. On Linux every supported
CLI is launched through the same Bubblewrap policy, not only Codex. It exposes
the selected task worktree, the minimum CLI/runtime files, the generated worker
context, the read-only envelope and public verification key, and that
provider's explicitly allowlisted model-auth files. It creates a separate PID
namespace, filters the
environment, and hides the user's home, global GearCore config, operator
credentials, dispatcher CLI homes, other process state, and HIVE operator
state. Operator secrets live outside every source checkout, including
`data/hive-state`, so mounting HIVE code cannot mount authority.

Each provider has a reviewed auth-mount manifest; directory-wide auth/home
mounts are forbidden. The preflight proves every requested source exists, is
the expected file type, and does not overlap an operator-secret path. Linux
CLI/provider combinations without this common containment contract are not
supported as autonomous workers and fail closed. On macOS and Windows,
autonomous worker launch remains disabled until an equivalent reviewed
filesystem/process adapter exists. Human operator launches are unaffected.

### 7.4 Dispatcher authentication

HIVE stores a salted verifier for the operator bearer credential in operator
state; the raw credential remains only in GearCore's protected credential
store. Authentication is transport-specific:

- for stdio, GearCore starts the dispatcher as its private child and supplies
  the credential in the child environment. Startup validates it, binds the
  authenticated state to that one stdio channel, and every tool passes through
  the session guard;
- for streamable HTTP/SSE, the dispatcher server receives no raw credential.
  Every initialization, message, and tool request requires
  `Authorization: Bearer <credential>`, validation occurs before MCP dispatch,
  and the MCP session ID is bound to the authenticated client. Missing or
  mismatched authentication cannot create or reuse a session.

The long-lived manager SSE process therefore never holds ambient operator
authority on behalf of arbitrary loopback clients. Sharing the host network is
not sufficient to call it. `hive-ops dashboard` and other operator UI clients
read the same GearCore credential reference and send authenticated headers;
browser-facing controls use a launcher-owned authenticated proxy/session and
never expose the bearer in a URL.

Read-only startup diagnostics may report `unauthenticated` without revealing
credential details. All dispatcher tools, including status/read tools, require
an authenticated session; the unauthenticated diagnostic is not an MCP tool.

The direct-wiring rollback uses a small stdio credential-injecting launcher
that reads the credential file at runtime, then execs the dispatcher. Native
client config never contains the bearer. Remote direct wiring is not a rollback
path; operator SSE clients must retain request authentication.

Authentication is separate from production activation authority. Possessing
the operator credential permits dispatcher use; existing human sign-off gates
still govern activation, campaign, cutover, and destructive operations.

## 8. Recursion and escalation invariants

1. Default operator access is available only where operator credentials are
   readable, and the dispatcher validates each client transport.
2. A valid worker envelope can narrow but never broaden capabilities.
3. An invalid supplied envelope never falls back to operator.
4. Project/cwd detection never selects an authority profile.
5. Every supported worker provider uses a fail-closed filesystem/process
   sandbox and cannot read operator GearCore config, credentials, or state.
6. Dispatcher authentication is checked server-side, not inferred from tool
   visibility or prompts.
7. A worker starting a child GearCore without its envelope still lacks global
   config and operator credentials; dispatcher startup/calls and connections to
   an existing manager endpoint fail closed.
8. A dispatcher-spawned worker cannot call spawn/dispatch tools and therefore
   cannot recursively create workers.
9. Every supported GenAI CLI receives equivalent effective HIVE capabilities
   for the same profile.

## 9. Failure behaviour

- Unknown profile: configuration error; no fallback.
- Invalid constrained envelope: diagnostic-only GearCore, exit failure for
  direct calls.
- Denied MCP requested: stable `capability_denied` error naming profile/server,
  not credential details.
- Missing operator credential: dispatcher server remains unavailable and status
  names the missing credential ID.
- Dispatcher unreachable: operator retains GearCore and other skills; HIVE
  status is offline, never substituted with gateway worker tools.
- Expired worker envelope during a long task: existing GearCore process may
  finish already-started calls, but a restart requires a refreshed envelope.
  Dispatcher access remains impossible either way.
- Unsupported client: fail the HIVE launch preflight rather than start a worker
  with unverified capability isolation.
- Unsupported platform/provider isolation: fail the autonomous worker launch;
  never fall back to a same-UID unsandboxed process.

## 10. Test strategy

All behaviour is implemented test-first.

### 10.1 GearCore unit tests

- version-2 compatibility maps to the implicit default profile;
- operator default inside and outside a project;
- project overlays affect context but not authority;
- valid constrained envelope selects `hive-worker`;
- invalid/expired/unknown envelopes fail closed;
- denies override global/project includes and project-local definitions;
- protected global MCP and skill entries survive restrictive version-2 and
  version-3 project allowlists, denies, and same-ID project-local collisions
  while emitting a diagnostic;
- `call`, `serve`, `status`, skill listing, and skill requests share one
  effective profile;
- credential files require safe ownership/type/mode and never appear in output;
- nested project/worktree auto-detection does not change an enforced profile.

### 10.2 GearCore integration tests

- a fake operator server starts with credential injection;
- the worker profile neither starts nor lists that server;
- Ed25519 envelopes verify with the public key while modified/new envelopes
  cannot be forged without the private key;
- a worker process unsets its environment and starts a child GearCore: the
  child cannot authenticate the operator server;
- each supported transport applies the same profile filtering;
- synchronized self-skills do not embed credentials or worker envelopes.

### 10.3 HIVE tests

- write a failing real-boundary test before changing spawner logic: a generated
  worker runtime can use gateway tools but cannot list, start, or call dispatcher
  tools;
- human-started GearCore can make an authenticated read-only dispatcher call
  from HIVE and from an unrelated temporary project;
- Codex, Claude, Kimi, Gemini, Kiro, and OpenCode generated configs select the
  worker envelope consistently;
- removing the envelope, copying operator config, changing cwd to HIVE, and
  directly starting `hive.server.server` all fail to grant worker dispatcher
  authority;
- every supported worker harness fails to read operator credentials/state or
  inspect operator processes, and cannot authenticate to an already-running
  stdio, SSE, or streamable-HTTP dispatcher;
- provider auth manifests expose only the files each CLI requires and reject a
  directory or path overlapping operator state;
- dispatcher-auth mutations prove missing checks fail their intended tests;
- existing worker filesystem-containment and recursive-spawn regressions remain
  green.

### 10.4 Live gate

1. Start one human-selected GenAI CLI outside HIVE with GearCore; verify HIVE
   dispatcher tools and one authenticated read-only call, even when that
   version-2 project has a restrictive allowlist plus colliding local
   `hive-dispatcher` server and skill definitions.
2. Start the same CLI inside HIVE; verify the same operator profile.
3. Spawn one HIVE worker in a HIVE worktree; verify gateway access and explicit
   dispatcher denial.
4. Ask that worker to start another GearCore process and attempt dispatcher
   discovery/call, and to connect directly to the running manager endpoint;
   verify denial and zero new workers.
5. Repeat profile equivalence for every enabled CLI harness without dispatching
   product work.

## 11. Rollout

1. Promote GearCore version-3 profiles with version-2 compatibility.
2. Register the global HIVE operator profile and dispatcher skill.
3. Move operator state outside source worktrees and add per-client HIVE
   dispatcher authentication while retaining authenticated stdio direct wiring.
4. Add Ed25519 worker envelopes, generated constrained contexts, and the common
   Linux containment wrapper for every supported worker CLI.
5. Run deterministic and live recursion-denial gates.
6. Remove direct dispatcher MCP wiring from GenAI client-specific launchers.
7. Make GearCore the documented HIVE capability entrypoint.

Rollback before step 6 selects the existing direct dispatcher wiring for human
launches and retains current worker allowlists. Rollback after step 6 restores
the direct human wiring only; it never removes dispatcher server authentication
or worker isolation.

## 12. Acceptance criteria

- Any human-started GearCore-enabled GenAI CLI receives authenticated HIVE
  dispatcher capability from any directory.
- Any HIVE-launched worker receives gateway capability and cannot authenticate,
  list, or call dispatcher capability.
- Cwd, prompt, provider, and client identity do not determine authority.
- The same generic GearCore profile mechanism supports non-HIVE roles.
- Dispatcher recursion denial is enforced by configuration, filesystem
  containment, credentials, and the server—not model obedience.
- Existing version-2 GearCore configurations continue to work unchanged.
- No credential, token, or envelope secret appears in logs, status, skills,
  Git, or worker-visible configuration.
