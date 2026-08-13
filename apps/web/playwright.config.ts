import { defineConfig, devices } from '@playwright/test'

/**
 * Browser E2E for the web workbench.
 *
 * The E2E suite runs against a real, already-running backend: by default the
 * Compose stack served through nginx at http://127.0.0.1:5173 (same origin
 * `/api` proxy, `MODEL_PROVIDER=fake` for deterministic answers). There is no
 * `webServer` block on purpose — CI starts the stack itself and Playwright is
 * pointed at it via `PLAYWRIGHT_BASE_URL`.
 *
 * Privacy: screenshots/video are disabled; only on-failure traces are kept and
 * every prompt/assertion uses synthetic content (fake-provider strings).
 */
export default defineConfig({
  testDir: './e2e',
  outputDir: './test-results',
  fullyParallel: false,
  workers: 1,
  timeout: 60_000,
  expect: { timeout: 20_000 },
  retries: process.env.CI ? 1 : 0,
  reporter: [
    ['list'],
    ['html', { outputFolder: 'playwright-report', open: 'never' }],
  ],
  use: {
    baseURL: process.env.PLAYWRIGHT_BASE_URL ?? 'http://127.0.0.1:5173',
    trace: 'retain-on-failure',
    screenshot: 'off',
    video: 'off',
    actionTimeout: 15_000,
  },
  projects: [
    {
      name: 'desktop-chromium',
      use: { ...devices['Desktop Chrome'] },
      testIgnore: /mobile\.spec\.ts/,
    },
    {
      name: 'mobile-chromium',
      use: {
        ...devices['Pixel 7'],
        viewport: { width: 390, height: 844 },
      },
      testMatch: /mobile\.spec\.ts/,
    },
  ],
})
