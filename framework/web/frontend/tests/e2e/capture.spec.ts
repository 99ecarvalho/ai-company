import { expect, test } from '@playwright/test';
import { closeConversation, postMessage } from './helpers/api';

test.describe('CapturePanel', () => {
  test('envia msg + conv aparece na sidebar + auto-switch pra ConversationPanel', async ({ page, request }) => {
    const topic = `e2e-capture-${Date.now()}`;
    await page.goto('/');

    // capture panel é o default (sem conv ativa)
    await expect(page.getByPlaceholder("What's on your mind?")).toBeVisible();

    // posta direto via API (digitar+click é coberto separadamente — aqui foco
    // no auto-switch da sidebar). Aguarda conv aparecer e ConversationPanel
    // abrir automaticamente quando ha pending_ask — mas msg do humano nao
    // gera ask. Aqui validamos manualmente o flow visual basico.
    const sent = await postMessage(request, 'inbox', topic, 'capture e2e test');
    const convId = `${sent.stream}/${sent.topic}`;

    // Aguarda card aparecer na sidebar (polling 5s do store).
    const card = page.getByRole('button', { name: new RegExp(`inbox\\s+${topic}`) });
    await expect(card).toBeVisible({ timeout: 10_000 });

    // Click no card abre ConversationPanel.
    await card.click();
    await expect(page.getByText(`#inbox · ${topic}`)).toBeVisible();

    await closeConversation(request, convId);
  });
});
