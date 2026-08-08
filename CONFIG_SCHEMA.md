# GearCore configuration schema (v2 and v3)

GearCore reads a global registry and, when selected explicitly or found by walking
up from the current directory, one project configuration. Version 2 remains the
legacy compatibility format. Version 3 adds global capability profiles,
protected global bindings, constrained launch envelopes, and credential
references.

## File locations

| Layer | Default path | Purpose |
|---|---|---|
| Global | `~/.config/gearcore/config.yaml` | Registry, profiles, disclosure, and conflict resolution |
| Credentials | `~/.config/gearcore/credentials/<credential_ref>` | File-backed MCP credentials |
| Project | `<project>/.gearcore/config.yaml` | Project context and narrowing rules |
| Project skills | `<project>/.gearcore/skills/` | Project-local skill bundles |

Use `--config PATH` to select another global configuration and `--project PATH`
to select a project root. The launch-policy flags must precede the subcommand:

```bash
gearcore --config /etc/gearcore/config.yaml --profile operator status
```

## Security boundary

Profiles select policy and provide defense in depth. They decide which registered
capabilities GearCore exposes; they do not contain an already-hostile child
process. An authenticated MCP server plus the launcher's process, filesystem, and
network containment are the hostile-process boundary.

The intended integration is:

- a human-started, GearCore-enabled GenAI CLI selects the default `operator`
  profile and can receive the protected HIVE dispatcher;
- a HIVE-started worker receives a signed constrained `hive-worker` envelope and
  must not receive the dispatcher.

This GearCore release supplies that policy and authentication dependency. It does
not claim that a HIVE installation has already issued envelopes, authenticated
its dispatcher, or enabled containment.

## Version 2 compatibility

A wholly version-2 setup behaves as before and is reported as the implicit
`default` profile. Project allowlists filter global entries, project MCP
definitions can replace same-ID global definitions, project-local skills are
included, project disclosure replaces global disclosure, and legacy project
`deny` keys are ignored.

```yaml
version: 2

registry:
  mcp_servers:
    - id: filesystem
      type: stdio
      command: npx
      args: [-y, "@modelcontextprotocol/server-filesystem", /srv/project]
      enabled: true
  skills_dirs:
    - ~/.config/gearcore/skills

disclosure:
  strategy: manual
  activation_threshold: 0.85
  core_skills: [chrono-core]

resolution:
  auto_deduplicate: true
  categories:
    file_io:
      preferred: filesystem
      strategy: namespace
      namespace_prefix: fs_
```

A version-2 project remains:

```yaml
version: 2
context:
  name: example
  description: Example project context
scope:
  mcp_servers:
    include: [filesystem]
  skills:
    include: [chrono-core]
disclosure:
  core_skills: [chrono-core]
registry:
  mcp_servers: []
```

Running `gearcore --config GLOBAL --project PROJECT status` reports
`profile: default`, `source: default`, and no version-3 diagnostic solely because
the files use version 2.

## Version 3 global configuration

Migration keeps `registry` and `resolution`, changes `version` to `3`, and adds
`profiles.default` plus `profiles.entries`. Move profile-specific disclosure into
each entry. The default operator is selected independently of the current working
directory.

```yaml
version: 3

registry:
  mcp_servers:
    - id: hive-dispatcher
      type: stdio
      command: /opt/hive/bin/dispatcher-mcp
      args: [serve]
      auth:
        credential_ref: hive-dispatcher-operator
        stdio_environment: HIVE_DISPATCHER_CREDENTIAL
      enabled: true
    - id: hive-gateway
      type: http
      url: http://127.0.0.1:8765/mcp
      auth:
        credential_ref: hive-worker-gateway
        http_scheme: bearer
      enabled: true
  skills_dirs:
    - ~/.config/gearcore/skills

profiles:
  default: operator
  entries:
    operator:
      constrained: false
      scope:
        mcp_servers:
          include: [hive-dispatcher, hive-gateway]
          deny: []
          protected: [hive-dispatcher]
        skills:
          include: [chrono-core, hive-dispatcher]
          deny: []
          protected: [hive-dispatcher]
      disclosure:
        strategy: manual
        activation_threshold: 0.85
        core_skills: [chrono-core, hive-dispatcher]
    hive-worker:
      constrained: true
      scope:
        mcp_servers:
          include: [hive-gateway]
          deny: [hive-dispatcher]
        skills:
          include: [hive-worker, chrono-core]
          deny: [hive-dispatcher]
      disclosure:
        core_skills: [hive-worker, chrono-core]
```

### Profile fields

| Field | Type | Default | Meaning |
|---|---|---|---|
| `profiles.default` | string | required | Global profile selected when neither an envelope nor `--profile` selects another |
| `profiles.entries.<name>.constrained` | boolean | `false` | Marks a profile eligible for a signed launch envelope |
| `scope.<kind>.include` | list or omitted | omitted | Allow all registered global IDs when omitted; allow only listed IDs when present; `[]` allows none |
| `scope.<kind>.deny` | list | `[]` | Remove listed non-protected IDs after includes and project narrowing |
| `scope.<kind>.protected` | list | `[]` | Pin these IDs to their trusted global MCP/skill binding |
| `disclosure.core_skills` | list | `[]` | Level-0 skills revealed inline and activated without a request |

`<kind>` is `mcp_servers` or `skills`. Deny is applied last, except that a
protected global cannot be denied or replaced. A protected global also survives
a restrictive v2 project allowlist, a v3 project overlay, a same-ID project MCP
definition, and a same-name project-local skill. GearCore records
`protected_capability_override` when a project attempts such an override.

Protecting `hive-dispatcher` through `profile-set` requires both a protected,
enabled global MCP definition and a protected trusted global skill bundle of that
name. Core skills must also resolve to trusted global bundles.

## Version 3 project overlay

A v3 project can narrow the already-selected global profile by defining a
same-name entry. It cannot contain `profiles.default`, select another profile, or
declare `protected`. Only its `scope` include/deny rules affect the selected
profile; protection and default authority remain global.

```yaml
version: 3
context:
  name: example-worker-project
profiles:
  entries:
    hive-worker:
      scope:
        mcp_servers:
          include: [hive-gateway]
          deny: [optional-worker-tool]
        skills:
          include: [hive-worker, chrono-core]
          deny: [optional-worker-skill]
```

Project-local MCP definitions and skills may be added only when the effective
policy allows them. Constrained or envelope-enforced launches do not gain a new
project-local capability outside their allowed set. A project collision never
replaces a protected global binding.

## MCP registry and authenticated transports

Each `registry.mcp_servers` item supports:

| Field | Required | Meaning |
|---|---|---|
| `id` | yes | Unique server identifier |
| `type` | no | `stdio` (default), `sse`, or `http` |
| `command` | stdio | Child command |
| `args` | no | Child arguments |
| `env` | no | Non-secret child environment values |
| `url` | sse/http | Endpoint URL |
| `auth` | no | Reference-only authentication configuration |
| `enabled` | no | Defaults to `true` |

Use exactly one transport-specific authentication setting:

```yaml
# stdio: the referenced value is injected only into this child environment
- id: private-stdio
  type: stdio
  command: /opt/service/bin/mcp-server
  args: [serve]
  auth:
    credential_ref: private-stdio
    stdio_environment: SERVICE_MCP_CREDENTIAL

# SSE: Authorization: Bearer ... is sent on the connection
- id: private-sse
  type: sse
  url: https://mcp.example.invalid/sse
  auth:
    credential_ref: private-network
    http_scheme: bearer

# Streamable HTTP: uses the streamable HTTP client, not SSE
- id: private-http
  type: http
  url: https://mcp.example.invalid/mcp
  auth:
    credential_ref: private-network
    http_scheme: bearer
```

For stdio, `stdio_environment` must be a non-empty environment-variable name and
`http_scheme` must be omitted. For SSE/HTTP, `http_scheme` must be exactly
`bearer` and `stdio_environment` must be omitted. A missing or unsafe credential
fails closed: the backend does not start, `call` exits non-zero, and `status`
omits it from `active_mcp` and reports `credential_unavailable`.

### Credential store

`credential_ref` is a single opaque filename, not a path. The default lookup is
`~/.config/gearcore/credentials/<credential_ref>`. The store must be a real
directory owned by the current user and must not be group/other writable. Each
credential must be a regular, non-symlink file owned by the current user with no
group/other permission bits. A typical setup is a `0700` directory and `0600`
files.

```bash
mkdir -p ~/.config/gearcore/credentials
chmod 700 ~/.config/gearcore/credentials
chmod 600 ~/.config/gearcore/credentials/private-network
```

Never place plaintext tokens in YAML, command arguments, configured `env`,
endpoint URLs, query strings, CLI flags, or the ambient parent process
environment. YAML and CLI contain only `credential_ref`; the referenced file
holds the value. The sole stdio exception is GearCore itself materializing that
referenced value at backend start into the named private child environment. It
does not mutate `os.environ` or the retained configuration, and it clears its
temporary parameter/mapping references after the child is created. GearCore
rejects recognized plaintext authentication routes and never prints credential
values in status or diagnostics.

## Signed constrained launch envelopes

Both envelope flags are required and must precede the subcommand:

```bash
gearcore \
  --config /etc/gearcore/config.yaml \
  --context-envelope /run/hive/launch-envelope.json \
  --envelope-public-key /etc/hive/worker-envelope-public-key.json \
  status
```

The public-key document is a JSON object with no extra fields:

```json
{"version":1,"issuer":"hive-worker-launcher","public_key":"<base64url Ed25519 public key>"}
```

An envelope is a JSON object with these exact fields:

```json
{
  "version": 1,
  "profile": "hive-worker",
  "issuer": "hive-worker-launcher",
  "launch_id": "<launch id>",
  "execution_id": "<execution id>",
  "task_id": "<task id>",
  "issued_at": 1800000000,
  "expires_at": 1800000060,
  "nonce": "<unique nonce>",
  "signature": "<base64url Ed25519 signature>"
}
```

The signature covers every field except `signature`, serialized as UTF-8 JSON
with keys sorted and separators exactly `(',', ':')` (no spaces):

```python
json.dumps(payload_without_signature, sort_keys=True, separators=(",", ":"))
```

The issuer must match the key document, time bounds must be valid, and `profile`
must name a configured `constrained: true` profile. A valid envelope is
authoritative and sets `source: envelope` plus `enforced_profile`. Supplying
`--profile ALTERNATE` alongside it is allowed only when the alternate's effective
MCP and skill policy and bindings are no broader than the envelope profile. This
is a conservative subset check; protection must match exactly.

If either explicit file is missing, malformed, expired, mismatched, or invalid,
GearCore never falls back to the default operator. It enters diagnostic-only mode:
`status` reports `invalid_launch_envelope` (or
`envelope_authority_expansion` for an unsafe alternate), `call` exits non-zero,
and `serve` starts no backend and exposes only `list_skills`, `request_skill`, and
`capability_diagnostic`.

## `profile-set`

`profile-set` creates or replaces one global profile and upgrades a valid v2
global document to v3 atomically. It is idempotent: repeating the same command
prints `unchanged`. `--project` is rejected; protection is global-only.

```text
gearcore [--config PATH] profile-set NAME
  [--mcp-include ID]... [--mcp-deny ID]... [--mcp-protect ID]...
  [--skill-include NAME]... [--skill-deny NAME]... [--skill-protect NAME]...
  [--core-skill NAME]... [--constrained] [--default]
```

Options are repeatable and replace the named profile's policy. `--default`
selects this profile as the global default; without it, an existing default is
preserved (the first profile becomes the default during v2 migration).
Contradictory include/deny/protect/core rules, duplicate values, missing protected
global definitions, missing trusted global skill bundles, or project-scoped use
fail without changing the file.

```bash
gearcore --config /etc/gearcore/config.yaml profile-set operator \
  --mcp-include hive-dispatcher \
  --mcp-include hive-gateway \
  --mcp-protect hive-dispatcher \
  --skill-include hive-dispatcher \
  --skill-include chrono-core \
  --skill-protect hive-dispatcher \
  --core-skill chrono-core \
  --core-skill hive-dispatcher \
  --default

gearcore --config /etc/gearcore/config.yaml profile-set hive-worker \
  --mcp-include hive-gateway \
  --mcp-deny hive-dispatcher \
  --skill-include hive-worker \
  --skill-include chrono-core \
  --skill-deny hive-dispatcher \
  --core-skill hive-worker \
  --core-skill chrono-core \
  --constrained
```

The referenced global registry and skill directories must already define the
protected/core entries before these commands run.

## Status contract

`gearcore status` emits stable, gate-oriented fields before its human-readable
details:

```text
profile: operator
source: default
enforced_profile: none
constrained: false
active_mcp: hive-dispatcher,hive-gateway
denied_mcp: none
protected_mcp: hive-dispatcher
active_skills: chrono-core,hive-dispatcher
denied_skills: none
protected_skills: hive-dispatcher
diagnostics: none
```

ID lists are sorted, comma-separated, and use `none` when empty. `source` is
`default`, `envelope`, or `invalid-envelope` as applicable. A plain `--profile`
selection without an envelope currently retains `source: default`; the selected
name is authoritative in the separate `profile` field.
Diagnostics are stable codes, including `protected_capability_override`,
`invalid_launch_envelope`, `envelope_authority_expansion`,
`credential_unavailable`, `skill_registry_unavailable`, and
`protected_skill_unavailable`. Status does not print commands, URLs, credential
references, credential values, skill instructions, or filesystem paths.

## Legacy resolution fields

`resolution.categories` remains global in v2 and v3. Each category supports
`preferred`, `strategy` (`suppress_others`, `namespace`, or `unify`),
`namespace_prefix`, and `unified_name`. Project configuration cannot override
global conflict-resolution authority.
