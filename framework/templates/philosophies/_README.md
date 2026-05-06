# Philosophy templates

Cada arquivo `.md` aqui eh um **template** (semente) de filosofia
operacional. Nao sao canonicos — sao pontos de partida pra escrever a
sua. Copia o que servir pra `instance/company/philosophy.md` (ativa) ou
mistura varios via wizard de onboarding (Fase B).

Cada template segue o mesmo esqueleto pra que o claude_runner injete
de forma consistente no system prompt:

- **Princípios chave** — 5-10 bullets nao-negociaveis
- **Estrutura de tasks** — extensoes de `tasks.metadata_extra` + ciclos
- **Vocabulário** — glossario de termos (ex: "spec" vs "ticket" vs "PRD")
- **Agentes arquetípicos** — papeis sugeridos (alimenta wizard B)
- **Cadência sugerida** — entradas pra `instance/schedule.yaml`

Templates atuais (placeholders — preencher conforme uso real):

- `tdd.md` — Test-Driven Development
- `sdd.md` — Spec-Driven Development
- `context-management.md` — disciplina de what-goes-into-prompt
- `bdd.md` — Behavior-Driven Development
- `ddd.md` — Domain-Driven Design (bounded contexts → agents)
- `tbd.md` — Trunk-Based Development
- `hexagonal.md` — Hexagonal / Clean Architecture
- `custom.md` — esqueleto vazio pra escrever do zero
