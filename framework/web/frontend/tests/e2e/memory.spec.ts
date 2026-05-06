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
  // Cleanup best-effort — nao falha se ja foi removido.
  await request.delete(`/api/memory/${agent}/${encodeURIComponent(key)}`);
}

test.describe('MemoryOverlay', () => {
  test('abre overlay sem erros; estrutura de form esta presente', async ({ page }) => {
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

  test('add fact via UI aparece na lista', async ({ page, request }) => {
    // Seed um fact pre-existente pra garantir que o agent aparece no dropdown.
    // (agents vem via GET /api/memory/agents que so lista quem tem facts).
    const seedKey = `e2e-seed-${Date.now()}`;
    await seedFact(request, AGENT, seedKey, 'seed value', ['e2e']);

    await page.goto('/');
    await page.getByRole('button', { name: 'Memory' }).click();
    const dialog = page.getByRole('dialog', { name: 'Memory' });
    await expect(dialog).toBeVisible();

    // Garante agent selecionado
    await dialog.getByLabel('Agent').selectOption(AGENT);

    // Adiciona fact via UI
    const newKey = `e2e-add-${Date.now()}`;
    await dialog.getByPlaceholder('key').fill(newKey);
    await dialog.getByPlaceholder('value').fill('ui-added value');
    await dialog.getByPlaceholder('tags (comma-separated)').fill('e2e,ui');
    await dialog.getByRole('button', { name: 'Save' }).click();

    // Aparece na lista (espera ate card renderizar)
    const added = dialog.locator(`[data-fact-key="${newKey}"]`);
    await expect(added).toBeVisible();
    await expect(added).toContainText('ui-added value');
    await expect(added).toContainText('e2e, ui');

    // Cleanup
    await deleteFact(request, AGENT, newKey);
    await deleteFact(request, AGENT, seedKey);
  });

  test('edit fact via UI atualiza valor', async ({ page, request }) => {
    const key = `e2e-edit-${Date.now()}`;
    await seedFact(request, AGENT, key, 'original value', ['orig']);

    await page.goto('/');
    await page.getByRole('button', { name: 'Memory' }).click();
    const dialog = page.getByRole('dialog', { name: 'Memory' });
    await expect(dialog).toBeVisible();
    await dialog.getByLabel('Agent').selectOption(AGENT);

    // Encontra o card e clica em Edit
    const card = dialog.locator(`[data-fact-key="${key}"]`);
    await expect(card).toBeVisible();
    await card.getByLabel('Edit fact').click();

    // Edita e salva
    const editValue = card.getByLabel('Edit value');
    await expect(editValue).toBeVisible();
    await editValue.fill('edited value');
    await card.getByLabel('Edit tags').fill('edited');
    await card.getByRole('button', { name: 'Save' }).click();

    // Espera voltar pra view mode com conteudo novo
    await expect(card).toContainText('edited value');
    await expect(card).toContainText('edited');
    await expect(card).not.toContainText('original value');

    await deleteFact(request, AGENT, key);
  });

  test('edit cancel descarta mudancas', async ({ page, request }) => {
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

    // Volta pra view mode, valor original preservado
    await expect(card).toContainText('keep me');
    await expect(card).not.toContainText('should not persist');

    await deleteFact(request, AGENT, key);
  });

  test('delete fact via UI remove da lista', async ({ page, request }) => {
    const key = `e2e-del-${Date.now()}`;
    await seedFact(request, AGENT, key, 'to be deleted', []);

    await page.goto('/');
    await page.getByRole('button', { name: 'Memory' }).click();
    const dialog = page.getByRole('dialog', { name: 'Memory' });
    await dialog.getByLabel('Agent').selectOption(AGENT);

    const card = dialog.locator(`[data-fact-key="${key}"]`);
    await expect(card).toBeVisible();

    // confirm() → aceita via dialog handler
    page.once('dialog', (d) => d.accept());
    await card.getByLabel('Delete fact').click();

    await expect(card).toHaveCount(0);
  });

  test('filtro por tag reduz a lista', async ({ page, request }) => {
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

    // Ambos visiveis antes do filtro
    await expect(dialog.locator(`[data-fact-key="${keyA}"]`)).toBeVisible();
    await expect(dialog.locator(`[data-fact-key="${keyB}"]`)).toBeVisible();

    // Aplica filtro de tag unica
    await dialog.getByLabel('Filter by tag').fill(uniqTag);
    // debounce 300ms
    await expect(dialog.locator(`[data-fact-key="${keyA}"]`)).toBeVisible({ timeout: 2000 });
    await expect(dialog.locator(`[data-fact-key="${keyB}"]`)).toHaveCount(0);

    await deleteFact(request, AGENT, keyA);
    await deleteFact(request, AGENT, keyB);
  });

  test('search full-text encontra por conteudo do value', async ({ page, request }) => {
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

  test('edit em key inexistente retorna 404 via API', async ({ request }) => {
    const r = await request.put(`/api/memory/${AGENT}/ghost-key-never-existed`, {
      data: { value: 'x' }
    });
    expect(r.status()).toBe(404);
  });
});
