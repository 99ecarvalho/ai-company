import { expect, test, type APIRequestContext } from '@playwright/test';

const AGENT = 'inbox';

async function seedFact(
  request: APIRequestContext,
  agent: string,
  key: string,
  value: string,
  tags: string[] = []
): Promise<void> {
  const r = await request.post(`/api/memory/${agent}`, {
    data: { key, value, tags }
  });
  expect(r.ok(), await r.text()).toBeTruthy();
}

async function deleteFact(
  request: APIRequestContext,
  agent: string,
  key: string
): Promise<void> {
  // Best-effort cleanup — doesn't fail if already removed.
  await request.delete(`/api/memory/${agent}/${encodeURIComponent(key)}`);
}

test.describe('MemoryOverlay', () => {
  test('opens overlay without errors; form structure is present', async ({ page }) => {
    await page.goto('/');
    await page.getByRole('button', { name: 'Memory' }).click();

    const dialog = page.getByRole('dialog', { name: 'Memory' });
    await expect(dialog).toBeVisible();

    // Add fact form
    await expect(dialog.getByText('Add fact')).toBeVisible();
    await expect(dialog.getByPlaceholder('key')).toBeVisible();
    await expect(dialog.getByPlaceholder('value')).toBeVisible();
    await expect(dialog.getByPlaceholder('tags (comma-separated)')).toBeVisible();
  });

  test('add fact via UI shows up in the list', async ({ page, request }) => {
    // Seed a pre-existing fact to make sure the agent shows up in the dropdown.
    // (agents come from GET /api/memory/agents, which only lists agents with facts).
    const seedKey = `e2e-seed-${Date.now()}`;
    await seedFact(request, AGENT, seedKey, 'seed value', ['e2e']);

    await page.goto('/');
    await page.getByRole('button', { name: 'Memory' }).click();
    const dialog = page.getByRole('dialog', { name: 'Memory' });
    await expect(dialog).toBeVisible();

    // Make sure the agent is selected
    await dialog.getByLabel('Agent').selectOption(AGENT);

    // Add fact via UI
    const newKey = `e2e-add-${Date.now()}`;
    await dialog.getByPlaceholder('key').fill(newKey);
    await dialog.getByPlaceholder('value').fill('ui-added value');
    await dialog.getByPlaceholder('tags (comma-separated)').fill('e2e,ui');
    await dialog.getByRole('button', { name: 'Save' }).click();

    // Shows up in the list (waits until the card renders)
    const added = dialog.locator(`[data-fact-key="${newKey}"]`);
    await expect(added).toBeVisible();
    await expect(added).toContainText('ui-added value');
    await expect(added).toContainText('e2e, ui');

    // Cleanup
    await deleteFact(request, AGENT, newKey);
    await deleteFact(request, AGENT, seedKey);
  });

  test('edit fact via UI updates value', async ({ page, request }) => {
    const key = `e2e-edit-${Date.now()}`;
    await seedFact(request, AGENT, key, 'original value', ['orig']);

    await page.goto('/');
    await page.getByRole('button', { name: 'Memory' }).click();
    const dialog = page.getByRole('dialog', { name: 'Memory' });
    await expect(dialog).toBeVisible();
    await dialog.getByLabel('Agent').selectOption(AGENT);

    // Find the card and click Edit
    const card = dialog.locator(`[data-fact-key="${key}"]`);
    await expect(card).toBeVisible();
    await card.getByLabel('Edit fact').click();

    // Edit and save
    const editValue = card.getByLabel('Edit value');
    await expect(editValue).toBeVisible();
    await editValue.fill('edited value');
    await card.getByLabel('Edit tags').fill('edited');
    await card.getByRole('button', { name: 'Save' }).click();

    // Wait for it to return to view mode with the new content
    await expect(card).toContainText('edited value');
    await expect(card).toContainText('edited');
    await expect(card).not.toContainText('original value');

    await deleteFact(request, AGENT, key);
  });

  test('edit cancel discards changes', async ({ page, request }) => {
    const key = `e2e-cancel-${Date.now()}`;
    await seedFact(request, AGENT, key, 'keep me', []);

    await page.goto('/');
    await page.getByRole('button', { name: 'Memory' }).click();
    const dialog = page.getByRole('dialog', { name: 'Memory' });
    await dialog.getByLabel('Agent').selectOption(AGENT);

    const card = dialog.locator(`[data-fact-key="${key}"]`);
    await card.getByLabel('Edit fact').click();
    await card.getByLabel('Edit value').fill('should not persist');
    await card.getByRole('button', { name: 'Cancel' }).click();

    // Back to view mode, original value preserved
    await expect(card).toContainText('keep me');
    await expect(card).not.toContainText('should not persist');

    await deleteFact(request, AGENT, key);
  });

  test('delete fact via UI removes it from the list', async ({ page, request }) => {
    const key = `e2e-del-${Date.now()}`;
    await seedFact(request, AGENT, key, 'to be deleted', []);

    await page.goto('/');
    await page.getByRole('button', { name: 'Memory' }).click();
    const dialog = page.getByRole('dialog', { name: 'Memory' });
    await dialog.getByLabel('Agent').selectOption(AGENT);

    const card = dialog.locator(`[data-fact-key="${key}"]`);
    await expect(card).toBeVisible();

    // confirm() → accepted via dialog handler
    page.once('dialog', (d) => d.accept());
    await card.getByLabel('Delete fact').click();

    await expect(card).toHaveCount(0);
  });

  test('tag filter narrows the list', async ({ page, request }) => {
    const stamp = Date.now();
    const keyA = `e2e-tagA-${stamp}`;
    const keyB = `e2e-tagB-${stamp}`;
    const uniqTag = `e2e-only-${stamp}`;

    await seedFact(request, AGENT, keyA, 'has special tag', [uniqTag]);
    await seedFact(request, AGENT, keyB, 'does not', ['other']);

    await page.goto('/');
    await page.getByRole('button', { name: 'Memory' }).click();
    const dialog = page.getByRole('dialog', { name: 'Memory' });
    await dialog.getByLabel('Agent').selectOption(AGENT);

    // Both visible before the filter
    await expect(dialog.locator(`[data-fact-key="${keyA}"]`)).toBeVisible();
    await expect(dialog.locator(`[data-fact-key="${keyB}"]`)).toBeVisible();

    // Apply the unique tag filter
    await dialog.getByLabel('Filter by tag').fill(uniqTag);
    // debounce 300ms
    await expect(dialog.locator(`[data-fact-key="${keyA}"]`)).toBeVisible({ timeout: 2000 });
    await expect(dialog.locator(`[data-fact-key="${keyB}"]`)).toHaveCount(0);

    await deleteFact(request, AGENT, keyA);
    await deleteFact(request, AGENT, keyB);
  });

  test('full-text search finds by value content', async ({ page, request }) => {
    const stamp = Date.now();
    const needle = `uniqueneedle${stamp}`;
    const hitKey = `e2e-search-hit-${stamp}`;
    const missKey = `e2e-search-miss-${stamp}`;

    await seedFact(request, AGENT, hitKey, `contains ${needle} inside`, []);
    await seedFact(request, AGENT, missKey, 'unrelated content', []);

    await page.goto('/');
    await page.getByRole('button', { name: 'Memory' }).click();
    const dialog = page.getByRole('dialog', { name: 'Memory' });
    await dialog.getByLabel('Agent').selectOption(AGENT);

    await dialog.getByLabel('Search facts').fill(needle);
    await expect(dialog.locator(`[data-fact-key="${hitKey}"]`)).toBeVisible({ timeout: 2000 });
    await expect(dialog.locator(`[data-fact-key="${missKey}"]`)).toHaveCount(0);

    await deleteFact(request, AGENT, hitKey);
    await deleteFact(request, AGENT, missKey);
  });

  test('edit on nonexistent key returns 404 via API', async ({ request }) => {
    const r = await request.put(`/api/memory/${AGENT}/ghost-key-never-existed`, {
      data: { value: 'x' }
    });
    expect(r.status()).toBe(404);
  });
});
