/**
 * Global setup pra suite E2E:
 *  1. close-all (remove convs anteriores da sidebar).
 *  2. ping /health pra garantir stack up.
 *
 * NAO restartamos agentes aqui — assume que `make test-e2e` ja recreou
 * com POOL_SIZE/IDLE_TIMEOUT_SEC adequados.
 */
import { request } from '@playwright/test';

export default async () => {
  const baseURL = process.env.E2E_BASE_URL || 'http://localhost:9090';
  const ctx = await request.newContext({ baseURL });
  try {
    const r = await ctx.get('/health');
    if (!r.ok()) {
      throw new Error(`stack /health = ${r.status()} (esperado 200)`);
    }
    await ctx.post('/api/conversations/close-all');
  } finally {
    await ctx.dispose();
  }
};
