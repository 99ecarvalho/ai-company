import { expect, test } from '@playwright/test';
import { closeConversation, seedRun } from './helpers/api';

test.describe('ConversationPanel', () => {
  test('opens conv with a full run, shows human msg + mock bot reply', async ({ page, request }) => {
    const topic = `e2e-conv-${Date.now()}`;
    const { convId, reply } = await seedRun(
      request, 'inbox', topic, 'e2e conv prompt'
    );

    await page.goto('/');
    const card = page.getByRole('button', { name: new RegExp(`inbox\\s+${topic}`) });
    await expect(card).toBeVisible({ timeout: 10_000 });
    await card.click();

    // header
    await expect(page.getByText(`#inbox · ${topic}`)).toBeVisible();

    // human message
    await expect(page.getByText('e2e conv prompt')).toBeVisible();

    // mock bot reply (CLAUDE_MOCK enables the "[mock reply] ..." fallback if
    // CLAUDE_MOCK_REPLY is not set). A snippet of the bot reply is enough.
    await expect(
      page.getByText(reply.content.slice(0, 30), { exact: false }).first()
    ).toBeVisible();

    await closeConversation(request, convId);
  });

  test('Close button closes conv and returns to capture panel', async ({ page, request }) => {
    const topic = `e2e-close-${Date.now()}`;
    const { convId } = await seedRun(request, 'inbox', topic, 'close test');
    await page.goto('/');
    await page.getByRole('button', { name: new RegExp(`inbox\\s+${topic}`) }).click();

    // The conv header has a "Close" button — use main to scope it (the sidebar has
    // a "Close all conversations" aria-label).
    const closeBtn = page.locator('main').getByRole('button', { name: 'Close', exact: true });
    await closeBtn.click();

    // CapturePanel is visible again — placeholder of the main textarea
    await expect(page.locator('textarea[placeholder*="mind"]')).toBeVisible();

    // Idempotent — extra cleanup via API in case the UI left state open
    await closeConversation(request, convId).catch(() => undefined);
  });
});
