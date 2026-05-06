# Manual E2E test steps

> Specs precursoras pra suite Playwright versionada. Cada bloco abaixo
> mapeia 1 fluxo validado manualmente via Playwright MCP. Quando virar
> spec real, cada `## F-N` vira um `test('...', async ({ page }) => {...})`.
>
> **Pré-requisitos comuns:**
> - Stack up: `make up` (8 containers healthy)
> - `WEB_AUTH_DEV_BYPASS=1` no `.env` (skipa login)
> - Pelo menos 1 conv com mensagens em `inbox` (envia uma manualmente
>   se DB foi resetado)

---

## F-1 — Live trace SSE

**Pré:** uma conv com run completo (ex: `inbox/livetrace-test`).

**Steps:**
1. `goto('/')`
2. Snapshot deve incluir botão `inbox livetrace-test` na lista de convs.
3. `click('button[name="inbox livetrace-test ..."]')`
4. Aguardar `[role="button"][title^="Hide live trace"]` ou similar
   (LiveTrace strip aparece após carregar `/live/recent` + abrir SSE).
5. Strip deve mostrar último evento — texto contém `RUN_END turns=`
   ou `tool_use` ou `thinking`.
6. `click(LiveTrace strip)` → expande.
7. Lista de eventos visível com 3 itens mínimo: `RUN_START`, `THINKING`
   ou `TOOL_USE`, `RUN_END` (ordem desc por `id`).
8. Cada `<li>` tem ícone lucide + kind uppercase + summary truncada +
   timestamp.

**Asserts cruciais:**
- Network tab tem `GET /api/conversations/<id>/live/recent?limit=50` (200).
- Network tab tem EventSource `/api/conversations/<id>/live/stream`
  conectado.
- Console sem errors.

**Setup auxiliar pra reset de DB:**
```bash
curl -s -X POST http://localhost:9090/api/post-message \
  -H 'content-type: application/json' \
  -d '{"stream":"inbox","topic":"livetrace-test","content":"OK?"}'
# aguardar ~30s pra agente responder
```

---

## F-2 — Search global FTS

**Pré:** pelo menos 2 mensagens contendo a string "tarefa" (case-insensitive).

**Steps:**
1. `goto('/')`
2. `click('button[aria-label="Search messages"]')` (ícone lupa no footer
   da sidebar).
3. Dialog `Search messages` deve abrir. Texto inicial:
   `"Type at least 2 characters."`
4. `fill('input[placeholder*="Search across"]', 'tarefa')`.
5. Aguardar 300ms (debounce).
6. Lista de resultados aparece — pelo menos 1 `<button>` com:
   - header: `inbox` + topic + `<sender>` + timestamp
   - corpo: snippet com `<mark>tarefa</mark>` highlighted (cor warn).
7. `click(primeiro resultado)` → dialog fecha + ConversationPanel abre na
   conv correspondente.

**Edge cases:**
- 1 char → "Type at least 2 characters."
- query sem match → `No matches for "..."`.

---

## F-7 — uPlot timeseries em Telemetry

**Pré:** pelo menos 2 runs em buckets diferentes (>5min apart).

**Steps:**
1. `goto('/')`
2. `click('button[aria-label="Telemetry"]')` (footer sidebar).
3. Dialog Telemetry abre. Snapshot deve ter:
   - 4 KPI cards (`runs`, `total cost`, `total time`, `output tokens`)
   - heading `Time series (<bucket> bucket)`
   - chart canvas (uPlot) com 3 séries: runs (azul), cost USD (vermelho),
     duration ms (amarelo)
   - tabela `By agent`
   - tabela `Latest runs`
4. Trocar window selector pra `7d` → chart re-renderiza com bucket maior.
5. Verificar legenda interativa abaixo do chart (quadrados coloridos).

**Asserts cruciais:**
- `GET /api/telemetry/timeseries?window=24h` retorna 200 com `points: [...]`.
- Network: 3 chamadas em paralelo (`summary`, `recent`, `timeseries`).

---

## F-8 — Image preview inline

**Pré:** arquivo `instance/company/test-image.png` (cópia de qualquer png).

**Setup:**
```bash
cp framework/web/static/icon-192.png instance/company/test-image.png
curl -s -X POST http://localhost:9090/api/post-message \
  -H 'content-type: application/json' \
  -d '{"stream":"inbox","topic":"image-preview-test","content":"![logo](company/test-image.png)\n\nLink: company/test-image.png"}'
```

**Steps:**
1. `goto('/')`
2. `click('button[name="inbox image-preview-test ..."]')`
3. Conv abre. Mensagem do `admin` deve mostrar:
   - `<img>` direto no topo (markdown image syntax) — class `inlineImage`,
     src `/api/files/read?path=company/test-image.png`, max-h 320px.
   - Texto "Link:" + `<a class="fileLink">company/test-image.png</a>` (laranja sublinhado).
   - `<img class="inlineImage inlineImageThumb">` logo após o anchor —
     thumbnail max-h 180px com cursor pointer.
4. `click(thumbnail)` → abre `FileViewerOverlay` com a imagem em tamanho
   maior (handler global de `.fileLink` propaga via parent).

**Cleanup:**
```bash
rm instance/company/test-image.png
```

---

## Próximos passos pra virar spec Playwright

- Adicionar `@playwright/test` em devDependencies do `frontend/package.json`.
- `playwright.config.ts` com `baseURL: http://localhost:9090`,
  `webServer` opcional pra subir stack se ainda não estiver.
- Helpers compartilhados em `tests/helpers.ts`:
  - `seedConversation(stream, topic, content)` via fetch direto.
  - `waitForLiveEvent(convId, kind)` listening EventSource numa Promise.
  - `screenshotMatches(name)` se quiser visual diff.
- 1 `.spec.ts` por feature acima.
- `make test-e2e` no Makefile rodando `pnpm exec playwright test` no
  diretório frontend.

Cada spec deve **ser self-contained** — criar seus dados via API antes,
limpar depois (DELETE direto na conv).
