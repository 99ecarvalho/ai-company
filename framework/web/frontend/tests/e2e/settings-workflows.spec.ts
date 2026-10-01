import { expect, test } from '@playwright/test';

// Unique name per run to avoid colliding with the instance's existing workflows
// and with runs executing in parallel.
const TEST_WF = `e2e-test-${Date.now()}-${Math.floor(Math.random() * 10000)}`;

test.describe('Settings → Workflows', () => {
  test('CRUD: create, edit, validate, delete', async ({ page, request }) => {
    // Defensive cleanup in case a previous run left garbage behind.
    await request.delete(`/api/workflows/${encodeURIComponent(TEST_WF)}`).catch(() => undefined);

    await page.goto('/settings?tab=workflows');
    await expect(page.getByRole('button', { name: /Workflows/ })).toBeVisible();

    // Sanity: there is already at least one workflow (the ones the instance uses today).
    const picker = page.getByLabel('Workflow:');
    await expect(picker).toBeVisible();

    // ---------- Create ----------
    page.once('dialog', (d) => d.accept(TEST_WF));
    await page.getByRole('button', { name: /New workflow/ }).click();

    // Wait for the selection to switch to the new workflow.
    await expect(picker).toHaveValue(TEST_WF);
    // Template default: 1 step "start" -> done.
    await expect(page.getByRole('combobox', { name: 'Initial step' })).toHaveValue('start');

    // ---------- Edit: add step + change initial_step ----------
    await page.getByRole('button', { name: /Add step/ }).click();
    // Rename step 2 from "step-2" to "end" to make the assertion easier.
    const step2Name = page.locator('input').filter({ hasText: '' }).nth(0); // fallback
    // Find it by its exact initial value:
    const step2 = page.locator('input[value="step-2"]');
    await expect(step2).toBeVisible();
    await step2.fill('end');

    // start.next: remove "done", add "end"
    // The "done" chip has a "Remove done" button via aria-label.
    await page
      .getByRole('button', { name: 'Remove done' })
      .first()
      .click();
    // Add "end" to the start step's next via the "+ add…" select
    const firstAdd = page.locator('select').filter({ hasText: '+ add…' }).first();
    await firstAdd.selectOption({ label: 'end' });

    // Save
    await page.getByRole('button', { name: /^Save$/ }).click();
    await expect(page.getByText('Up to date.')).toBeVisible();

    // Verify persistence via API.
    const r1 = await request.get(`/api/workflows/${encodeURIComponent(TEST_WF)}`);
    expect(r1.ok()).toBeTruthy();
    const wf = await r1.json();
    expect(wf.initial_step).toBe('start');
    expect(Object.keys(wf.steps).sort()).toEqual(['end', 'start']);
    expect(wf.steps.start.next).toContain('end');
    expect(wf.steps.end.next).toContain('done');

    // ---------- Server-side validation: next pointing to a nonexistent step ----------
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

    // Gone from the dropdown.
    await expect(picker).not.toHaveValue(TEST_WF);
    const r3 = await request.get(`/api/workflows/${encodeURIComponent(TEST_WF)}`);
    expect(r3.status()).toBe(404);
  });

  test('deep-link via ?tab=workflows shows the panel', async ({ page }) => {
    await page.goto('/settings?tab=workflows');
    await expect(page.getByText(/Workflow taxonomy for this instance/)).toBeVisible();
  });
});
