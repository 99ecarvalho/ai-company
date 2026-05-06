import { expect, test, type APIRequestContext } from '@playwright/test';
import { closeConversation, postMessage } from './helpers/api';

// Posta um live_event crua via /api/telemetry/live-event (mesma rota usada
// pelo claude_runner). Nao exige agente rodando.
async function postLiveEvent(
  request: APIRequestContext,
  params: {
    stream: string;
    topic: string;
    agent: string;
    kind: string;
    summary?: string;
    data?: Record<string, unknown>;
  }
) {
  const r = await request.post('/api/telemetry/live-event', {
    data: params,
  });
  expect(r.ok(), await r.text()).toBeTruthy();
}

test.describe('LiveEventLine friendly names + sidebar hierarchy', () => {
  test('tool_use of ask_agent shows friendly label instead of raw JSON', async ({ page, request }) => {
    const ts = Date.now();
    const askerAgent = 'product-owner';
    const targetAgent = 'database-engineer';
    const parentTopic = `e2e-friendly-${ts}`;

    await postMessage(request, askerAgent, parentTopic, 'seed');
    const parentConvId = `${askerAgent}/${parentTopic}`;

    await postLiveEvent(request, {
      stream: askerAgent,
      topic: parentTopic,
      agent: askerAgent,
      kind: 'tool_use',
      summary: 'mcp__agent_framework__ask_agent',
      data: {
        tool: 'mcp__agent_framework__ask_agent',
        input: JSON.stringify({
          target_agent: targetAgent,
          question: 'how does the pricing table index work',
        }),
      },
    });

    await page.goto('/');
    await page.getByRole('button', { name: new RegExp(`${askerAgent}\\s+${parentTopic}`) }).click();

    // Friendly label: `Ask <target>: <question>` instead of the raw tool name.
    const panel = page.locator('main');
    await expect(panel).toContainText(`Ask ${targetAgent}:`, { timeout: 10_000 });
    await expect(panel).toContainText('how does the pricing', { timeout: 5_000 });

    // No inline nested-conversation anymore.
    await expect(panel.locator('[data-testid="nested-conversation"]')).toHaveCount(0);

    await closeConversation(request, parentConvId);
  });

  test('ask_agent child appears nested under parent in sidebar', async ({ page, request }) => {
    const ts = Date.now();
    const askerAgent = 'product-owner';
    const targetAgent = 'database-engineer';
    const parentTopic = `e2e-nest-${ts}`;
    const childTopic = `__ask-from-${askerAgent}-e2e${ts}`;

    await postMessage(request, askerAgent, parentTopic, 'parent seed');
    const parentConvId = `${askerAgent}/${parentTopic}`;

    // Create the child conv first so the correlation query has something to
    // match against when the tool_use event lands.
    await postMessage(request, targetAgent, childTopic, 'child ask seed');
    const childConvId = `${targetAgent}/${childTopic}`;

    await postLiveEvent(request, {
      stream: askerAgent,
      topic: parentTopic,
      agent: askerAgent,
      kind: 'tool_use',
      summary: 'mcp__agent_framework__ask_agent',
      data: {
        tool: 'mcp__agent_framework__ask_agent',
        input: JSON.stringify({ target_agent: targetAgent, question: 'nest test' }),
      },
    });

    await page.goto('/');
    // Poll: parent comes back with a chevron for children after refresh (5s).
    // Since the child is an `__ask-from-*` conv without a pending_ask, it
    // counts as "resolved" (ask concluído) — parent shows a chevron that
    // exposes a "1 resolved" bucket when expanded.
    const parentCard = page.locator(`[data-id="${parentConvId}"]`);
    await expect(parentCard).toBeVisible({ timeout: 10_000 });

    // The treeNode wrapper around the parent gets a chevron button when the
    // parent has any children (active or resolved).
    const parentWrapper = page.locator('.relative').filter({ has: parentCard }).first();
    const chevron = parentWrapper.locator('button[aria-label="Expand children"]');
    await expect(chevron).toBeVisible({ timeout: 15_000 });

    // Expand → the "1 resolved" bucket appears.
    await chevron.click();
    await expect(page.getByRole('button', { name: /1 resolved/i })).toBeVisible({
      timeout: 5_000
    });

    await closeConversation(request, parentConvId);
    await closeConversation(request, childConvId);
  });

  test('tool_use expands with IN/OUT panels when paired with tool_result', async ({ page, request }) => {
    const ts = Date.now();
    const agent = 'product-owner';
    const topic = `e2e-inout-${ts}`;

    await postMessage(request, agent, topic, 'seed');
    const convId = `${agent}/${topic}`;

    // tool_use followed by a tool_result — panel pairs them by proximity.
    await postLiveEvent(request, {
      stream: agent,
      topic,
      agent,
      kind: 'tool_use',
      summary: 'Read',
      data: {
        tool: 'Read',
        input: JSON.stringify({ file_path: '/workspace/hello.txt' }),
      },
    });
    await postLiveEvent(request, {
      stream: agent,
      topic,
      agent,
      kind: 'tool_result',
      summary: 'ok',
      data: {
        is_error: false,
        content: 'Hello, world! This is the file content.',
      },
    });

    await page.goto('/');
    await page.getByRole('button', { name: new RegExp(`${agent}\\s+${topic}`) }).click();

    const panel = page.locator('main');
    // Friendly label for Read uses the basename.
    const toolLine = panel.getByRole('button', { name: /Read hello\.txt/i });
    await expect(toolLine).toBeVisible({ timeout: 10_000 });

    await toolLine.click();
    await expect(panel.getByText('IN', { exact: true })).toBeVisible({ timeout: 5_000 });
    await expect(panel.getByText('OUT', { exact: true })).toBeVisible();
    await expect(panel).toContainText('/workspace/hello.txt');
    await expect(panel).toContainText('Hello, world!');

    await closeConversation(request, convId);
  });
});
