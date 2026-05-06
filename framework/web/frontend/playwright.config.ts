import { defineConfig, devices } from '@playwright/test';

const baseURL = process.env.E2E_BASE_URL || 'http://localhost:9090';

export default defineConfig({
  testDir: './tests/e2e',
  globalSetup: './tests/e2e/global-setup.ts',
  fullyParallel: false, // testes compartilham DB; serial evita races
  retries: process.env.CI ? 1 : 0,
  workers: 1,
  reporter: [['list'], ['html', { open: 'never' }]],
  timeout: 30_000,
  expect: { timeout: 10_000 },
  use: {
    baseURL,
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    video: 'retain-on-failure',
    headless: true
  },
  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] }
    }
  ]
});
