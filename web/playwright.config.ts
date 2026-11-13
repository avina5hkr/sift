import { defineConfig, devices } from '@playwright/test'

// No shared web server: each test starts its own Sift (own data folder, backup folder and port)
// through the `sift` fixture in e2e/fixtures.ts, so no test depends on another.
export default defineConfig({
  testDir: './e2e',
  reporter: 'list',
  timeout: 60_000,
  projects: [{ name: 'webkit', use: { ...devices['Desktop Safari'] } }],
})
