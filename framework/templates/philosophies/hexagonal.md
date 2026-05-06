# Hexagonal / Clean Architecture

> **summary:** Separacao ports/adapters; nucleo de dominio isolado de I/O.

_(Placeholder — preencher com seu uso real. Veja `_README.md`.)_

## Princípios chave

- _(5-10 bullets nao-negociaveis desta filosofia)_

## Estrutura de tasks

```yaml
# Sugestao de campos extras pra esta filosofia. Extensoes custom vao pro
# JSONB `tasks.metadata_extra` via complete_phase — get_task_state devolve
# junto no retorno. Dados core da task (status, current_step, workflow,
# baseline, worktrees) ja sao geridos pelo framework.
```

## Vocabulário

| Termo | Significado nesta filosofia |
|---|---|
| _(ex: spec)_ | _(o que conta como spec valida)_ |

## Agentes arquetípicos

Papeis sugeridos pra empresa que adota esta filosofia (entram como
proposta no wizard de onboarding):

- **_(slug)_** — _(role em 1 linha)_

## Cadência sugerida

```yaml
# Sugestoes de cron pro instance/schedule.yaml
# - id: ...
#   cron: "..."
#   action: ...
```
