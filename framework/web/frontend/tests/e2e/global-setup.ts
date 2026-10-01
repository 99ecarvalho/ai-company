/**
 * Global setup for the E2E suite:
 *  1. close-all (removes previous convs from the sidebar).
 *  2. ping /health to make sure the stack is up.
 *
 * We do NOT restart agents here — assumes `make test-e2e` already recreated
 * them with suitable POOL_SIZE/IDLE_TIMEOUT_SEC.
 */
import { request } from '@playwright/test';

export default async () => {
  const baseURL = process.env.E2E_BASE_URL || 'http://localhost:9090';
  const ctx = await request.newContext({ baseURL });
  try {
    const r = await ctx.get('/health');
    if (!r.ok()) {
      throw new Error(`stack /health = ${r.status()} (expected 200)`);
    }
    await ctx.post('/api/conversations/close-all');
  } finally {
    await ctx.dispose();
  }
};
