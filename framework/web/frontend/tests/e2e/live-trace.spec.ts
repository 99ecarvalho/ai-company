import { expect, test } from '@playwright/test';
import { closeConversation, seedRun } from './helpers/api';

test.describe('LiveTrace strip', () => {
  test('shows last event (run_end) + expands event list', async ({ page, request }) => {
    const topic = `e2e-live-${Date.now()}`;
    const { convId } = await seedRun(request, 'inbox', topic, 'live trace test');

    await page.goto('/');
    await page.getByRole('button', { name: new RegExp(`inbox\\s+${topic}`) }).click();

    // the strip is the first <button> in main containing kind text
    const strip = page
      .locator('main button')
      .filter({ hasText: /run_end|run_start|thinking/i })
      .first();
    await expect(strip).toBeVisible({ timeout: 10_000 });

    // expand
    await strip.click();

    // event list visible after expanding
    const items = page.locator('main ul li');
    await expect(items.first()).toBeVisible();
    expect(await items.count()).toBeGreaterThanOrEqual(2);

    await closeConversation(request, convId);
  });
});
