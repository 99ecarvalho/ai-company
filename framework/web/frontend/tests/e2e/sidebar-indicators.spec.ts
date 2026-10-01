import { expect, test } from '@playwright/test';
import { execSync } from 'node:child_process';
import { closeConversation, seedRun } from './helpers/api';

/** Seed de pending_ask direto no DB (nao ha API pra criar manualmente —
 * tabela e populada pelo broker quando agente chama ask_human). Feio mas
 * suficiente pra exercitar o badge visual sem precisar de ask_human real. */
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
  test('bolinha de unread aparece em conv com bot reply nao ativa', async ({ page, request }) => {
    // Seed conv com bot reply (mock responde via CLAUDE_MOCK).
    const topic = `e2e-unread-${Date.now()}`;
    const { convId } = await seedRun(request, 'inbox', topic, 'unread seed');

    await page.goto('/');
    const card = page.locator(`[data-id="${convId}"]`);
    await expect(card).toBeVisible({ timeout: 10_000 });

    // Se o auto-switch (store) jogou o usuario na conv, a conv fica "active"
    // e o effect de markSeen apaga o unread. Forçamos voltar pro capture.
    await page.getByRole('button', { name: 'New capture' }).click();

    // Agora conv nao e ativa -> unread deve aparecer.
    await expect(card).toHaveAttribute('data-unread', 'true', { timeout: 10_000 });
    await expect(card.locator('[aria-label="Unread"]')).toBeVisible();

    // Clica na conv -> marcar como lida -> unread some.
    await card.click();
    await expect(card).toHaveAttribute('data-unread', 'false', { timeout: 5_000 });

    await closeConversation(request, convId);
  });

  test('badge "needs you" destaca conv com pending_ask', async ({ page, request }) => {
    const topic = `e2e-needs-${Date.now()}`;
    const { convId } = await seedRun(request, 'inbox', topic, 'needs-you seed');

    // Extrai conv_db_id via API /api/conversations (lista tras db_id).
    const r = await request.get('/api/conversations');
    const data = await r.json();
    const conv = data.items.find((c: { id: string }) => c.id === convId);
    expect(conv, 'conv aparece no listing').toBeTruthy();

    seedPendingAsk(conv.db_id);

    try {
      await page.goto('/');
      const card = page.locator(`[data-id="${convId}"]`);
      await expect(card).toBeVisible({ timeout: 10_000 });

      // Espera polling (5s) trazer has_pending_ask=true.
      await expect(card).toHaveAttribute('data-pending', 'true', { timeout: 10_000 });

      // Badge visivel com texto "needs you".
      await expect(card.getByText(/needs you/i)).toBeVisible();
    } finally {
      clearPendingAsk(conv.db_id);
      await closeConversation(request, convId);
    }
  });
});
