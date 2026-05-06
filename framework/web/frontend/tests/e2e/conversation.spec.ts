import { expect, test } from '@playwright/test';
import { closeConversation, seedRun } from './helpers/api';

test.describe('ConversationPanel', () => {
  test('abre conv com run completo, mostra msg humana + reply do bot mock', async ({ page, request }) => {
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

    // mensagem humana
    await expect(page.getByText('e2e conv prompt')).toBeVisible();

    // reply do bot mock (CLAUDE_MOCK ativa fallback "[mock reply] ..." se
    // CLAUDE_MOCK_REPLY nao setado). Snippet do bot eh suficiente.
    await expect(
      page.getByText(reply.content.slice(0, 30), { exact: false }).first()
    ).toBeVisible();

    await closeConversation(request, convId);
  });

  test('botao Close fecha conv e volta pra capture panel', async ({ page, request }) => {
    const topic = `e2e-close-${Date.now()}`;
    const { convId } = await seedRun(request, 'inbox', topic, 'close test');
    await page.goto('/');
    await page.getByRole('button', { name: new RegExp(`inbox\\s+${topic}`) }).click();

    // Header da conv tem botao "Close" — usa main pra escopar (sidebar tem
    // "Close all conversations" aria-label).
    const closeBtn = page.locator('main').getByRole('button', { name: 'Close', exact: true });
    await closeBtn.click();

    // CapturePanel volta visivel — placeholder do textarea principal
    await expect(page.locator('textarea[placeholder*="mind"]')).toBeVisible();

    // Idempotente — limpeza extra via API caso UI deixou state aberto
    await closeConversation(request, convId).catch(() => undefined);
  });
});
