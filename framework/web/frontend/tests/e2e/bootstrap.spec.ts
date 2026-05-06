import { expect, test } from '@playwright/test';

test.describe('bootstrap', () => {
  test('GET /health returns ok with db=true', async ({ request }) => {
    const r = await request.get('/health');
    expect(r.ok()).toBeTruthy();
    const body = await r.json();
    expect(body.status).toBe('ok');
    expect(body.db).toBe(true);
  });

  test('GET / with dev bypass does not redirect to /login', async ({ page }) => {
    await page.goto('/');
    await expect(page).toHaveURL('/');
    await expect(page).toHaveTitle(/Agents/);
    await expect(page.locator('aside')).toBeVisible();
  });

  test('rail + bottom-nav expose primary navigation; drawer holds secondary actions', async ({ page }) => {
    await page.goto('/');

    // Rail/bottom-nav primary actions (visible without opening anything).
    await expect(page.getByRole('button', { name: 'New capture' })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Conv', exact: true })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Backlog', exact: true })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Telemetry', exact: true })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Memory', exact: true })).toBeVisible();

    // Open drawer.
    await page.getByRole('button', { name: 'More', exact: true }).click();
    const drawer = page.getByRole('dialog', { name: 'Menu' });
    await expect(drawer).toBeVisible();

    // Secondary actions live inside the drawer.
    await expect(drawer.getByRole('button', { name: 'Settings' })).toBeVisible();
    await expect(drawer.getByRole('button', { name: 'Search messages' })).toBeVisible();
    await expect(drawer.getByRole('button', { name: 'Files' })).toBeVisible();
    await expect(drawer.getByRole('button', { name: 'Hire agent' })).toBeVisible();
    await expect(drawer.getByRole('button', { name: 'Activity log' })).toBeVisible();
    await expect(drawer.getByRole('button', { name: 'Toggle theme' })).toBeVisible();
    await expect(drawer.getByRole('button', { name: 'Close all conversations' })).toBeVisible();
  });

  test('theme toggle flips data-theme on html and persists', async ({ page }) => {
    // Normalize state (other runs may have left localStorage at 'light').
    await page.goto('/');
    await page.evaluate(() => localStorage.setItem('agf-theme', 'dark'));
    await page.reload();
    await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark');

    // Theme toggle moved into the drawer.
    await page.getByRole('button', { name: 'More', exact: true }).click();
    const drawer = page.getByRole('dialog', { name: 'Menu' });
    await drawer.getByRole('button', { name: 'Toggle theme' }).click();
    await expect(page.locator('html')).toHaveAttribute('data-theme', 'light');

    const stored = await page.evaluate(() => localStorage.getItem('agf-theme'));
    expect(stored).toBe('light');

    // Reload keeps light (inline script applies before JS boot).
    await page.reload();
    await expect(page.locator('html')).toHaveAttribute('data-theme', 'light');

    // Reset to dark so we don't leak into the next runs.
    await page.evaluate(() => localStorage.setItem('agf-theme', 'dark'));
  });
});
