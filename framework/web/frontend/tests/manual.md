# Manual E2E test steps

> Precursor specs for the versioned Playwright suite. Each block below
> maps 1 flow validated manually via Playwright MCP. When it becomes a
> real spec, each `## F-N` becomes a `test('...', async ({ page }) => {...})`.
>
> **Common prerequisites:**
> - Stack up: `make up` (8 containers healthy)
> - `WEB_AUTH_DEV_BYPASS=1` in `.env` (skips login)
> - At least 1 conv with messages in `inbox` (send one manually
>   if the DB was reset)

---

## F-1 — Live trace SSE

**Pre:** a conv with a full run (e.g. `inbox/livetrace-test`).

**Steps:**
1. `goto('/')`
2. Snapshot must include the `inbox livetrace-test` button in the conv list.
3. `click('button[name="inbox livetrace-test ..."]')`
4. Wait for `[role="button"][title^="Hide live trace"]` or similar
   (the LiveTrace strip appears after loading `/live/recent` + opening SSE).
5. The strip must show the last event — text contains `RUN_END turns=`
   or `tool_use` or `thinking`.
6. `click(LiveTrace strip)` → expands.
7. Event list visible with at least 3 items: `RUN_START`, `THINKING`
   or `TOOL_USE`, `RUN_END` (desc order by `id`).
8. Each `<li>` has a lucide icon + uppercase kind + truncated summary +
   timestamp.

**Key asserts:**
- Network tab has `GET /api/conversations/<id>/live/recent?limit=50` (200).
- Network tab has EventSource `/api/conversations/<id>/live/stream`
  connected.
- Console without errors.

**Helper setup for a DB reset:**
```bash
curl -s -X POST http://localhost:9090/api/post-message \
  -H 'content-type: application/json' \
  -d '{"stream":"inbox","topic":"livetrace-test","content":"OK?"}'
# wait ~30s for the agent to reply
```

---

## F-2 — Global FTS search

**Pre:** at least 2 messages containing the string "task" (case-insensitive).

**Steps:**
1. `goto('/')`
2. `click('button[aria-label="Search messages"]')` (magnifier icon in the
   sidebar footer).
3. The `Search messages` dialog must open. Initial text:
   `"Type at least 2 characters."`
4. `fill('input[placeholder*="Search across"]', 'task')`.
5. Wait 300ms (debounce).
6. The result list appears — at least 1 `<button>` with:
   - header: `inbox` + topic + `<sender>` + timestamp
   - body: snippet with `<mark>task</mark>` highlighted (warn color).
7. `click(first result)` → dialog closes + ConversationPanel opens on the
   matching conv.

**Edge cases:**
- 1 char → "Type at least 2 characters."
- query with no match → `No matches for "..."`.

---

## F-7 — uPlot timeseries in Telemetry

**Pre:** at least 2 runs in different buckets (>5min apart).

**Steps:**
1. `goto('/')`
2. `click('button[aria-label="Telemetry"]')` (sidebar footer).
3. The Telemetry dialog opens. Snapshot must have:
   - 4 KPI cards (`runs`, `total cost`, `total time`, `output tokens`)
   - heading `Time series (<bucket> bucket)`
   - chart canvas (uPlot) with 3 series: runs (blue), cost USD (red),
     duration ms (yellow)
   - `By agent` table
   - `Latest runs` table
4. Switch the window selector to `7d` → chart re-renders with a larger bucket.
5. Check the interactive legend below the chart (colored squares).

**Key asserts:**
- `GET /api/telemetry/timeseries?window=24h` returns 200 with `points: [...]`.
- Network: 3 calls in parallel (`summary`, `recent`, `timeseries`).

---

## F-8 — Image preview inline

**Pre:** file `instance/company/test-image.png` (a copy of any png).

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
3. The conv opens. The `admin` message must show:
   - `<img>` right at the top (markdown image syntax) — class `inlineImage`,
     src `/api/files/read?path=company/test-image.png`, max-h 320px.
   - Text "Link:" + `<a class="fileLink">company/test-image.png</a>` (orange underlined).
   - `<img class="inlineImage inlineImageThumb">` right after the anchor —
     thumbnail max-h 180px with pointer cursor.
4. `click(thumbnail)` → opens `FileViewerOverlay` with the image at a larger
   size (the global `.fileLink` handler propagates via the parent).

**Cleanup:**
```bash
rm instance/company/test-image.png
```

---

## Next steps to turn these into Playwright specs

- Add `@playwright/test` to devDependencies in `frontend/package.json`.
- `playwright.config.ts` with `baseURL: http://localhost:9090`,
  optional `webServer` to bring the stack up if it isn't already.
- Shared helpers in `tests/helpers.ts`:
  - `seedConversation(stream, topic, content)` via direct fetch.
  - `waitForLiveEvent(convId, kind)` listening to EventSource in a Promise.
  - `screenshotMatches(name)` if you want visual diff.
- 1 `.spec.ts` per feature above.
- `make test-e2e` in the Makefile running `pnpm exec playwright test` in the
  frontend directory.

Each spec must **be self-contained** — create its data via the API beforehand,
clean up afterwards (direct DELETE on the conv).
