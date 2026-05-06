import { expect, test } from '@playwright/test';

// Nome unico por run pra evitar colisao com workflows existentes da instancia
// (rapido/completo/analise-avulsa) e entre runs que rodem em paralelo.
const TEST_WF = `e2e-test-${Date.now()}-${Math.floor(Math.random() * 10000)}`;

test.describe('Settings → Workflows', () => {
  test('CRUD: create, edit, validate, delete', async ({ page, request }) => {
    // Defensive cleanup se algum run anterior deixou lixo.
    await request.delete(`/api/workflows/${encodeURIComponent(TEST_WF)}`).catch(() => undefined);

    await page.goto('/settings?tab=workflows');
    await expect(page.getByRole('button', { name: /Workflows/ })).toBeVisible();

    // Sanity: ja tem pelo menos um workflow (os que a instancia usa hoje).
    const picker = page.getByLabel('Workflow:');
    await expect(picker).toBeVisible();

    // ---------- Criar ----------
    page.once('dialog', (d) => d.accept(TEST_WF));
    await page.getByRole('button', { name: /New workflow/ }).click();

    // Espera seleção virar o workflow novo.
    await expect(picker).toHaveValue(TEST_WF);
    // Template default: 1 step "start" -> done.
    await expect(page.getByRole('combobox', { name: 'Initial step' })).toHaveValue('start');

    // ---------- Editar: adicionar step + trocar initial_step ----------
    await page.getByRole('button', { name: /Add step/ }).click();
    // Renomear step 2 de "step-2" pra "end" pra facilitar a asserção.
    const step2Name = page.locator('input').filter({ hasText: '' }).nth(0); // fallback
    // Acha pelo valor inicial exato:
    const step2 = page.locator('input[value="step-2"]');
    await expect(step2).toBeVisible();
    await step2.fill('end');

    // start.next: remove "done", adiciona "end"
    // O chip de "done" tem um botão "Remove done" por aria-label.
    await page
      .getByRole('button', { name: 'Remove done' })
      .first()
      .click();
    // Adicionar "end" no next do step start via select "+ add…"
    const firstAdd = page.locator('select').filter({ hasText: '+ add…' }).first();
    await firstAdd.selectOption({ label: 'end' });

    // Save
    await page.getByRole('button', { name: /^Save$/ }).click();
    await expect(page.getByText('Up to date.')).toBeVisible();

    // Verifica persistencia via API.
    const r1 = await request.get(`/api/workflows/${encodeURIComponent(TEST_WF)}`);
    expect(r1.ok()).toBeTruthy();
    const wf = await r1.json();
    expect(wf.initial_step).toBe('start');
    expect(Object.keys(wf.steps).sort()).toEqual(['end', 'start']);
    expect(wf.steps.start.next).toContain('end');
    expect(wf.steps.end.next).toContain('done');

    // ---------- Validação server-side: next apontando pra step inexistente ----------
    const r2 = await request.put(`/api/workflows/${encodeURIComponent(TEST_WF)}`, {
      data: {
        initial_step: 'start',
        steps: {
          start: { agent: null, artifact: null, next: ['ghost'] }
        }
      }
    });
    expect(r2.status()).toBe(400);
    const err = await r2.json();
    expect(err.detail).toMatch(/ghost/);

    // ---------- Delete ----------
    page.once('dialog', (d) => d.accept());
    await page.getByRole('button', { name: /^Delete$/ }).click();

    // Sumiu do dropdown.
    await expect(picker).not.toHaveValue(TEST_WF);
    const r3 = await request.get(`/api/workflows/${encodeURIComponent(TEST_WF)}`);
    expect(r3.status()).toBe(404);
  });

  test('deep-link via ?tab=workflows shows the panel', async ({ page }) => {
    await page.goto('/settings?tab=workflows');
    await expect(page.getByText(/Workflow taxonomy for this instance/)).toBeVisible();
  });
});
