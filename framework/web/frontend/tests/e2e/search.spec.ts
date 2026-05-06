import { expect, test } from '@playwright/test';
import { closeConversation, postMessage } from './helpers/api';

test.describe('SearchOverlay', () => {
  test('busca FTS retorna match com <mark> highlight + click abre conv', async ({ page, request }) => {
    const needle = `e2e-search-needle-${Date.now()}`;
    const topic = `e2e-search-${Date.now()}`;
    const sent = await postMessage(request, 'inbox', topic, `prefacio ${needle} sufixo`);
    const convId = `${sent.stream}/${sent.topic}`;

    await page.goto('/');
    await page.getByRole('button', { name: 'Search messages' }).click();

    const dialog = page.getByRole('dialog', { name: 'Search messages' });
    await expect(dialog).toBeVisible();
    await expect(dialog.getByText('Type at least 2 characters.')).toBeVisible();

    await dialog.getByRole('searchbox').fill(needle);

    // resultados aparecem após debounce
    const result = dialog.locator('button', { hasText: needle }).first();
    await expect(result).toBeVisible({ timeout: 5_000 });

    // pelo menos 1 <mark> no snippet (FTS quebra needle em palavras separadas)
    await expect(result.locator('mark').first()).toBeVisible();

    // click abre a conv
    await result.click();
    await expect(dialog).toBeHidden();
    await expect(page.getByText(`#inbox · ${topic}`)).toBeVisible();

    await closeConversation(request, convId);
  });

  test('query <2 chars mostra hint + sem resultados', async ({ page }) => {
    await page.goto('/');
    await page.getByRole('button', { name: 'Search messages' }).click();
    const dialog = page.getByRole('dialog', { name: 'Search messages' });
    await dialog.getByRole('searchbox').fill('z');
    await expect(dialog.getByText('Type at least 2 characters.')).toBeVisible();
  });
});
