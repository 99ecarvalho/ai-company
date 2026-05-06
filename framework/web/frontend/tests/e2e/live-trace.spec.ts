import { expect, test } from '@playwright/test';
import { closeConversation, seedRun } from './helpers/api';

test.describe('LiveTrace strip', () => {
  test('mostra ultimo evento (run_end) + expande lista de eventos', async ({ page, request }) => {
    const topic = `e2e-live-${Date.now()}`;
    const { convId } = await seedRun(request, 'inbox', topic, 'live trace test');

    await page.goto('/');
    await page.getByRole('button', { name: new RegExp(`inbox\\s+${topic}`) }).click();

    // strip eh o primeiro <button> em main que contem texto de kind
    const strip = page
      .locator('main button')
      .filter({ hasText: /run_end|run_start|thinking/i })
      .first();
    await expect(strip).toBeVisible({ timeout: 10_000 });

    // expande
    await strip.click();

    // lista de eventos visivel apos expansao
    const items = page.locator('main ul li');
    await expect(items.first()).toBeVisible();
    expect(await items.count()).toBeGreaterThanOrEqual(2);

    await closeConversation(request, convId);
  });
});
