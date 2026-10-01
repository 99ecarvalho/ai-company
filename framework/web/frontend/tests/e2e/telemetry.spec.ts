import { expect, test } from '@playwright/test';
import { closeConversation, seedRun } from './helpers/api';

test.describe('TelemetryOverlay', () => {
  test('opens overlay, shows KPIs + tables; window selector works', async ({ page, request }) => {
    // Seed a run so there is something in telemetry
    const topic = `e2e-tele-${Date.now()}`;
    const { convId } = await seedRun(request, 'inbox', topic, 'telemetry seed');

    await page.goto('/');
    await page.getByRole('button', { name: 'Telemetry' }).click();

    const dialog = page.getByRole('dialog', { name: 'Telemetry' });
    await expect(dialog).toBeVisible();

    // KPI labels (they're uppercase-tracking <div>s — check the unique ones)
    await expect(dialog.getByText('total cost')).toBeVisible();
    await expect(dialog.getByText('total time')).toBeVisible();
    await expect(dialog.getByText('output tokens')).toBeVisible();

    // By agent table
    await expect(dialog.getByText('By agent')).toBeVisible();

    // window selector switches to 7d without error
    await dialog.locator('select').selectOption('7d');

    await closeConversation(request, convId);
  });
});
