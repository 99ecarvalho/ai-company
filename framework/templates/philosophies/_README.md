# Philosophy templates

Each `.md` file here is a **template** (seed) for an operating philosophy.
They are not canonical — they are starting points for writing your own.
Copy what fits into `instance/company/philosophy.md` (the active one) or
mix several via the onboarding wizard (Phase B).

Each template follows the same skeleton so claude_runner can inject it
consistently into the system prompt:

- **Key principles** — 5-10 non-negotiable bullets
- **Task structure** — extensions to `tasks.metadata_extra` + cycles
- **Vocabulary** — glossary of terms (e.g. "spec" vs "ticket" vs "PRD")
- **Archetypal agents** — suggested roles (feeds wizard B)
- **Suggested cadence** — entries for `instance/schedule.yaml`

Current templates (placeholders — fill with real usage):

- `tdd.md` — Test-Driven Development
- `sdd.md` — Spec-Driven Development
- `context-management.md` — what-goes-into-prompt discipline
- `bdd.md` — Behavior-Driven Development
- `ddd.md` — Domain-Driven Design (bounded contexts → agents)
- `tbd.md` — Trunk-Based Development
- `hexagonal.md` — Hexagonal / Clean Architecture
- `custom.md` — empty skeleton to write from scratch
