import { expect, test } from '@playwright/test';
import { closeConversation, postMessage } from './helpers/api';

test.describe('SearchOverlay', () => {
  test('FTS search returns match with <mark> highlight + click opens conv', async ({ page, request }) => {
    const needle = `e2e-search-needle-${Date.now()}`;
    const topic = `e2e-search-${Date.now()}`;
    const sent = await postMessage(request, 'inbox', topic, `prefix ${needle} suffix`);
    const convId = `${sent.stream}/${sent.topic}`;

    await page.goto('/');
    await page.getByRole('button', { name: 'Search messages' }).click();

    const dialog = page.getByRole('dialog', { name: 'Search messages' });
    await expect(dialog).toBeVisible();
    await expect(dialog.getByText('Type at least 2 characters.')).toBeVisible();

    await dialog.getByRole('searchbox').fill(needle);

    // results appear after debounce
    const result = dialog.locator('button', { hasText: needle }).first();
    await expect(result).toBeVisible({ timeout: 5_000 });

    // at least 1 <mark> in the snippet (FTS splits the needle into separate words)
    await expect(result.locator('mark').first()).toBeVisible();

    // click opens the conv
    await result.click();
    await expect(dialog).toBeHidden();
    await expect(page.getByText(`#inbox · ${topic}`)).toBeVisible();

    await closeConversation(request, convId);
  });

  test('query <2 chars shows hint + no results', async ({ page }) => {
    await page.goto('/');
    await page.getByRole('button', { name: 'Search messages' }).click();
    const dialog = page.getByRole('dialog', { name: 'Search messages' });
    await dialog.getByRole('searchbox').fill('z');
    await expect(dialog.getByText('Type at least 2 characters.')).toBeVisible();
  });
});
