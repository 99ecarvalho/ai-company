import { expect, test } from '@playwright/test';
import { promises as fs } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { closeConversation, postMessage } from './helpers/api';

const __dirname = dirname(fileURLToPath(import.meta.url));
const PROJECT_ROOT = resolve(__dirname, '../../../../..');
const SRC_PNG = resolve(PROJECT_ROOT, 'framework/web/static/icon-192.png');
const DEST_PNG = resolve(PROJECT_ROOT, 'instance/company/e2e-image-test.png');

test.describe('Image preview inline', () => {
  test.beforeAll(async () => {
    await fs.copyFile(SRC_PNG, DEST_PNG);
  });

  test.afterAll(async () => {
    await fs.unlink(DEST_PNG).catch(() => undefined);
  });

  test('markdown ![](path) renderiza <img>; bare path tem fileLink + thumbnail', async ({ page, request }) => {
    const topic = `e2e-img-${Date.now()}`;
    const sent = await postMessage(
      request, 'inbox', topic,
      'Top:\n![logo](company/e2e-image-test.png)\n\nLink: company/e2e-image-test.png'
    );
    const convId = `${sent.stream}/${sent.topic}`;

    await page.goto('/');
    await page.getByRole('button', { name: new RegExp(`inbox\\s+${topic}`) }).click();

    // <img> com classe inlineImage e src apontando pro endpoint files/read
    const images = page.locator('img.inlineImage');
    await expect(images.first()).toBeVisible({ timeout: 10_000 });
    expect(await images.count()).toBeGreaterThanOrEqual(2);

    // src normalizado
    const src0 = await images.first().getAttribute('src');
    expect(src0).toContain('/api/files/read?path=company%2Fe2e-image-test.png');

    // Thumb (segundo <img>) tem class inlineImageThumb
    const thumb = page.locator('img.inlineImageThumb').first();
    await expect(thumb).toBeVisible();

    // fileLink ao bare path
    const link = page.locator('a.fileLink', { hasText: 'company/e2e-image-test.png' });
    await expect(link).toBeVisible();

    await closeConversation(request, convId);
  });
});
