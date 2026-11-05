import { defineConfig, devices } from '@playwright/test'

export default defineConfig({
  testDir: './e2e',
  reporter: 'list',
  use: { baseURL: 'http://127.0.0.1:8766' },
  projects: [{ name: 'webkit', use: { ...devices['Desktop Safari'] } }],
  webServer: {
    command:
      'rm -rf .e2e-home && SIFT_HOME=.e2e-home SIFT_DEV_TOKEN=e2e SIFT_WEB_DIST=dist uv run sift --no-window --port 8766',
    url: 'http://127.0.0.1:8766/',
    reuseExistingServer: false,
  },
})
