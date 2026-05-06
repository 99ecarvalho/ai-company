import { expect, test } from '@playwright/test';
import { closeConversation, seedRun } from './helpers/api';

test.describe('TelemetryOverlay', () => {
  test('abre overlay, mostra KPIs + tabelas; window selector funciona', async ({ page, request }) => {
    // Seed um run pra ter algo na telemetria
    const topic = `e2e-tele-${Date.now()}`;
    const { convId } = await seedRun(request, 'inbox', topic, 'telemetry seed');

    await page.goto('/');
    await page.getByRole('button', { name: 'Telemetry' }).click();

    const dialog = page.getByRole('dialog', { name: 'Telemetry' });
    await expect(dialog).toBeVisible();

    // KPI labels (sao <div> em uppercase tracking — checa pelos unicos)
    await expect(dialog.getByText('total cost')).toBeVisible();
    await expect(dialog.getByText('total time')).toBeVisible();
    await expect(dialog.getByText('output tokens')).toBeVisible();

    // tabela By agent
    await expect(dialog.getByText('By agent')).toBeVisible();

    // window selector troca pra 7d sem erro
    await dialog.locator('select').selectOption('7d');

    await closeConversation(request, convId);
  });
});
