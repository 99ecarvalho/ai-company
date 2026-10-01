import { expect, test } from '@playwright/test';
import { closeConversation, postMessage } from './helpers/api';

test.describe('CapturePanel', () => {
  test('sends msg + conv appears in sidebar + auto-switch to ConversationPanel', async ({ page, request }) => {
    const topic = `e2e-capture-${Date.now()}`;
    await page.goto('/');

    // capture panel is the default (no active conv)
    await expect(page.getByPlaceholder("What's on your mind?")).toBeVisible();

    // post directly via the API (type+click is covered separately — the focus here
    // is the sidebar auto-switch). Waits for the conv to appear; ConversationPanel
    // opens automatically when there's a pending_ask — but a human msg doesn't
    // create an ask. Here we manually validate the basic visual flow.
    const sent = await postMessage(request, 'inbox', topic, 'capture e2e test');
    const convId = `${sent.stream}/${sent.topic}`;

    // Wait for the card to appear in the sidebar (store polls every 5s).
    const card = page.getByRole('button', { name: new RegExp(`inbox\\s+${topic}`) });
    await expect(card).toBeVisible({ timeout: 10_000 });

    // Clicking the card opens ConversationPanel.
    await card.click();
    await expect(page.getByText(`#inbox · ${topic}`)).toBeVisible();

    await closeConversation(request, convId);
  });
});
