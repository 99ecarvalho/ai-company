import { expect, test } from '@playwright/test';

test.describe('FilesOverlay', () => {
  test('opens overlay via drawer and lists company/ entries', async ({ page }) => {
    await page.goto('/');
    // Files now lives in the drawer ("More" → "Files").
    await page.getByRole('button', { name: 'More', exact: true }).click();
    const drawer = page.getByRole('dialog', { name: 'Menu' });
    await drawer.getByRole('button', { name: 'Files' }).click();

    const dialog = page.getByRole('dialog', { name: 'Files' });
    await expect(dialog).toBeVisible();

    // initial path = company
    await expect(dialog.locator('div', { hasText: /^company$/ }).first()).toBeVisible();

    // Up + Refresh buttons
    await expect(dialog.getByRole('button', { name: /Up/ })).toBeVisible();
    await expect(dialog.getByRole('button', { name: 'Refresh' })).toBeVisible();
  });
});
