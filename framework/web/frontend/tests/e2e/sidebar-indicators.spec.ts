import { expect, test } from '@playwright/test';
import { execSync } from 'node:child_process';
import { closeConversation, seedRun } from './helpers/api';

/** Seeds a pending_ask directly in the DB (there's no API to create one manually —
 * the table is populated by the broker when an agent calls ask_human). Ugly but
 * enough to exercise the visual badge without a real ask_human. */
function seedPendingAsk(convDbId: number): void {
  const sql =
    `INSERT INTO messaging.pending_asks ` +
    `  (conversation_id, asker_id, question, blocking) ` +
    `VALUES (` +
    `  ${convDbId}, ` +
    `  (SELECT id FROM messaging.users WHERE kind='bot' LIMIT 1), ` +
    `  'e2e test question', true` +
    `) ON CONFLICT (conversation_id) DO UPDATE SET ` +
    `  resolved_at=NULL, question=EXCLUDED.question`;
  execSync(
    `docker compose exec -T postgres psql -U ${process.env.POSTGRES_USER ?? 'ai_company'} -d ${process.env.POSTGRES_DB ?? 'ai_company'} -c "${sql.replace(/"/g, '\\"')}"`,
    { stdio: 'pipe' }
  );
}

function clearPendingAsk(convDbId: number): void {
  execSync(
    `docker compose exec -T postgres psql -U ${process.env.POSTGRES_USER ?? 'ai_company'} -d ${process.env.POSTGRES_DB ?? 'ai_company'} -c "DELETE FROM messaging.pending_asks WHERE conversation_id=${convDbId}"`,
    { stdio: 'pipe' }
  );
}

test.describe('Sidebar indicators', () => {
  test('unread dot appears on a non-active conv with a bot reply', async ({ page, request }) => {
    // Seed a conv with a bot reply (mock replies via CLAUDE_MOCK).
    const topic = `e2e-unread-${Date.now()}`;
    const { convId } = await seedRun(request, 'inbox', topic, 'unread seed');

    await page.goto('/');
    const card = page.locator(`[data-id="${convId}"]`);
    await expect(card).toBeVisible({ timeout: 10_000 });

    // If the auto-switch (store) dropped the user into the conv, the conv becomes "active"
    // and the markSeen effect clears the unread. We force going back to capture.
    await page.getByRole('button', { name: 'New capture' }).click();

    // Now the conv isn't active -> unread must appear.
    await expect(card).toHaveAttribute('data-unread', 'true', { timeout: 10_000 });
    await expect(card.locator('[aria-label="Unread"]')).toBeVisible();

    // Click the conv -> mark as read -> unread disappears.
    await card.click();
    await expect(card).toHaveAttribute('data-unread', 'false', { timeout: 5_000 });

    await closeConversation(request, convId);
  });

  test('"needs you" badge highlights conv with pending_ask', async ({ page, request }) => {
    const topic = `e2e-needs-${Date.now()}`;
    const { convId } = await seedRun(request, 'inbox', topic, 'needs-you seed');

    // Extract conv_db_id via the /api/conversations API (the list includes db_id).
    const r = await request.get('/api/conversations');
    const data = await r.json();
    const conv = data.items.find((c: { id: string }) => c.id === convId);
    expect(conv, 'conv appears in the listing').toBeTruthy();

    seedPendingAsk(conv.db_id);

    try {
      await page.goto('/');
      const card = page.locator(`[data-id="${convId}"]`);
      await expect(card).toBeVisible({ timeout: 10_000 });

      // Wait for polling (5s) to bring has_pending_ask=true.
      await expect(card).toHaveAttribute('data-pending', 'true', { timeout: 10_000 });

      // Badge visible with text "needs you".
      await expect(card.getByText(/needs you/i)).toBeVisible();
    } finally {
      clearPendingAsk(conv.db_id);
      await closeConversation(request, convId);
    }
  });
});
