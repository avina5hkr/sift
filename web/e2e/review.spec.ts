import { expect, test } from './fixtures'

const CAM = '/sdcard/DCIM/Camera/IMG_E2E.jpg'
const SENT = '/sdcard/Android/media/com.whatsapp/WhatsApp/Media/WhatsApp Images/Sent/IMG-E2E.jpg'

test('exact duplicates: back up, analyse, review with previews and a persisted tick', async ({ page, sift }) => {
  await sift.prepare()
  await sift.post('/api/test/phone/images', { paths: [CAM, SENT] })
  await sift.post('/api/setup/sets', { enabled: ['Camera', 'WhatsApp media'] })
  const { job_id } = await sift.post<{ job_id: number }>('/api/backup/start', {
    sets: ['Camera', 'WhatsApp media'],
    dry_run: false,
  })
  await expect
    .poll(async () => (await sift.get<{ job_id: number; outcome: string } | null>('/api/backup/summary'))?.outcome, {
      timeout: 60_000,
    })
    .toBe('done')
  expect(job_id).toBeGreaterThan(0)

  await page.goto('/?t=e2e')
  await page.getByRole('link', { name: 'Cleanup' }).click()
  await page.getByRole('button', { name: 'Analyze', exact: true }).click()
  await expect(page.getByText('Exact duplicates').first()).toBeVisible({ timeout: 30_000 })
  await page.getByRole('link', { name: /Review/ }).or(page.getByRole('button', { name: /^Review/ })).first().click()

  await expect(page.getByText('KEEP').first()).toBeVisible({ timeout: 15_000 })
  const tick = page.getByRole('checkbox', { name: /IMG-E2E\.jpg/ })
  await expect(tick).toHaveAttribute('aria-checked', 'true')
  const thumbs = page.locator('img[src*="/api/media/"]')
  await expect(thumbs.first()).toBeVisible()
  await expect
    .poll(async () => thumbs.first().evaluate((img: HTMLImageElement) => img.complete && img.naturalWidth > 0))
    .toBe(true)

  await tick.click()
  await expect(tick).toHaveAttribute('aria-checked', 'false')
  await page.reload()
  await expect(page.getByRole('checkbox', { name: /IMG-E2E\.jpg/ })).toHaveAttribute('aria-checked', 'false')
})
