import { expect, test } from '@playwright/test';
import { deleteAgentPolicyByName } from './helpers/api';

// Settings overlay (renamed from CompanyOverlay; Company+SystemPrompts merged
// into a single Settings overlay reachable via the drawer "More" → "Settings").
test.describe('SettingsOverlay', () => {
  test('exposes Sections + Preview + Agents tabs; matrix lists agents', async ({ page, request }) => {
    await page.goto('/');
    await page.getByRole('button', { name: 'More', exact: true }).click();
    const drawer = page.getByRole('dialog', { name: 'Menu' });
    await drawer.getByRole('button', { name: 'Settings' }).click();

    const dialog = page.getByRole('dialog', { name: 'Settings' });
    await expect(dialog).toBeVisible();

    // Sections tab (default) — section list visible (e.g. global toggles).
    await expect(dialog.getByRole('button', { name: /Sections/ })).toBeVisible();

    // Switch to Preview.
    await dialog.getByRole('button', { name: /Preview/ }).click();
    await expect(dialog.getByText(/no agent|globals only|chars/i)).toBeVisible();

    // Switch to Agents — matrix headers.
    await dialog.getByRole('button', { name: /Agents/ }).click();
    await expect(dialog.getByRole('columnheader', { name: 'any' })).toBeVisible();
    await expect(dialog.getByRole('columnheader', { name: 'inbox', exact: true })).toBeVisible();
    await expect(dialog.getByRole('columnheader', { name: 'executor', exact: true })).toBeVisible();

    // Defensive cleanup.
    await deleteAgentPolicyByName(request, 'inbox').catch(() => undefined);
    await deleteAgentPolicyByName(request, 'executor').catch(() => undefined);
  });
});
