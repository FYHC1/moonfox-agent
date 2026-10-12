import { defineConfig } from '@playwright/test';
import { tmpdir } from 'node:os';
import path from 'node:path';

const baseURL = process.env.MOONFOX_UI_BASE_URL ?? 'http://127.0.0.1:5173';
const automaticFixture = !process.env.MOONFOX_TEST_BACKEND && !process.env.MOONFOX_UI_BASE_URL;
if (automaticFixture) {
  process.env.MOONFOX_TEST_BACKEND = 'http://127.0.0.1:8777';
  process.env.MOONFOX_TEST_TOKEN = 'ui-fixture-token';
  process.env.MOONFOX_UI_FIXTURE = '1';
}
export default defineConfig({
  testDir: './browser',
  fullyParallel: false,
  workers: 1,
  timeout: 35000,
  expect: { timeout: 8000 },
  reporter: 'list',
  outputDir: path.join(process.env.PI_SCRATCH_DIR ?? tmpdir(), 'moonfox-playwright-results'),
  use: {
    baseURL,
    channel: process.env.MOONFOX_BROWSER_CHANNEL ?? (process.platform === 'win32' ? 'msedge' : 'chromium'),
    viewport: { width: 1440, height: 920 },
    locale: 'zh-CN',
    colorScheme: 'dark',
    // Never capture traces/video containing a real token typed during authentication.
    trace: 'off',
    video: 'off',
    screenshot: 'only-on-failure',
  },
  webServer: [
    ...(!process.env.MOONFOX_UI_BASE_URL ? [{
      command: 'node node_modules/vite/bin/vite.js --host 127.0.0.1',
      url: baseURL,
      reuseExistingServer: !process.env.CI,
    }] : []),
    ...(automaticFixture ? [{
      command: 'node browser/fixture-server.mjs',
      url: 'http://127.0.0.1:8777/test/health',
      reuseExistingServer: false,
    }] : []),
  ],
});
