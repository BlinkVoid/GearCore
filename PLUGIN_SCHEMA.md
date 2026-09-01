# GearCore: Plugin Schema (v1)

GearCore recognizes **Codex-compatible plugin roots** — directories containing a
`.codex-plugin/plugin.json` manifest. When `gearcore onboard` is pointed at such
a directory, it registers the **whole plugin** (skills plus all sibling support
components), not just the discovered skill bundles.

> **Safety boundary:** GearCore preserves commands, orchestration, scripts,
> configs, tests, and docs as inert files. It does **not** auto-execute
> arbitrary plugin content — plugin registration is linkage and copying only.

---

## Directory Layout

```
my-plugin/
├── .codex-plugin/
│   └── plugin.json     # Required — plugin manifest (see below)
├── skills/             # Default skills path (configurable via "skills")
│   └── <skill-name>/
│       └── SKILL.md
├── commands/           # Optional — preserved as-is
├── orchestration/      # Optional — preserved as-is
├── scripts/            # Optional — preserved as-is
├── config/             # Optional — preserved as-is
├── configs/            # Optional — preserved as-is
├── tests/              # Optional — preserved as-is
└── docs/               # Optional — preserved as-is
```

---

## plugin.json

```json
{
  "name": "my-plugin",
  "skills": "./skills"
}
```

### Fields

| Field    | Type   | Required | Description                                                                 |
|----------|--------|----------|-----------------------------------------------------------------------------|
| `name`   | string | yes      | Plugin identifier. Must match the exact grammar `[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*`. Used as the registered directory name. |
| `skills` | string | no       | Relative path to the skills directory inside the plugin root. Default `./skills`. Must remain lexically and after resolution strictly inside the plugin root. |

Unknown manifest fields are preserved with the rest of the plugin content but
are not automatically executed, interpreted, or registered as MCP backends.
A malformed manifest (bad JSON, missing/unsafe `name`, or a `skills` path whose
resolved target escapes the plugin root) aborts onboarding with a `ValueError`
— no partial registration is performed.

---

## Onboarding

```bash
gearcore onboard /path/to/my-plugin                    # global scope, symlink
gearcore onboard /path/to/my-plugin --scope project    # project scope
gearcore onboard /path/to/my-plugin --copy-skills      # copy instead of symlink
gearcore onboard /path/to/my-plugin --dry-run          # preview plan
```

### Registration targets

| Scope   | Plugin registered at                      | Skills registered at                    |
|---------|-------------------------------------------|-----------------------------------------|
| Global  | `~/.config/gearcore/plugins/<name>`       | `~/.config/gearcore/skills/<skill>`     |
| Project | `<project>/.gearcore/plugins/<name>`      | `<project>/.gearcore/skills/<skill>`    |

- **Default (symlink):** the plugins directory entry is a symlink to the
  original plugin root. Discovered skills are registered as symlinks that point
  **through the installed plugin root**
  (`…/plugins/<name>/skills/<skill>`), never directly at the original skills
  leaf.
- **`--copy-skills`:** the whole plugin root is copied into the plugins
  directory (all sibling content preserved, including symlinks as symlinks),
  and skills are linked into the copy. Legacy behavior for non-plugin cores
  (copying each skill bundle into the skills dir) is unchanged.

### Preservation

Onboarding never rewrites plugin content. All top-level support components
found among `commands`, `orchestration`, `scripts`, `config`, `configs`,
`tests`, and `docs` are preserved as-is and reported by `--dry-run`.

### Preflight and idempotency

Registration is atomic: the plan is fully validated before any mutation.

- A conflicting destination (existing plugin or skill entry that is not
  equivalent to the source, including **broken symlinks**) aborts onboarding
  with no mutations.
- Re-onboarding an equivalent plugin root or equivalent skill link is a
  no-op (`skip`).

The declared `skills` path is checked after filesystem resolution as well as
lexically. An in-root path that is a symlink to a directory outside the plugin
root is rejected; an ordinary missing path inside the root remains valid.

---

## Removal

```bash
gearcore remove plugin <name> [--scope global|project]
```

Removes only the registered plugin path and the skill symlinks that resolve
inside it. Skill entries that are real directories, and any symlink target
outside the registered plugin, are never deleted.

---

## Skill instructions

`gearcore request-skill <name>` (and the MCP `request_skill` tool) appends a
*Skill bundle location* section to every skill's instructions, listing the
absolute **registered** path and the **resolved bundle root**, with guidance
that relative resources referenced by the skill (scripts, templates, docs)
resolve from the bundle root. This applies to plugin-backed and ordinary
skills alike.
