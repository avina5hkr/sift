import type { Page, Route } from '@playwright/test'
import { expect, test } from './fixtures'

// The Review screen with 20,000 items (10,000 groups of a keeper and one copy), served by a mocked
// API (page.route), so this measures the screen alone. Numbers are printed; see
// docs/spikes/m4-review.md for the last recorded run.

const GROUPS = 10_000
const PAGE = 200
const PIXEL = Buffer.from('R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7', 'base64')

type Item = Record<string, unknown>
const item = (id: number, role: 'keeper' | 'copy'): Item => ({
  id,
  path: `/sdcard/DCIM/Camera/IMG_${id}.${id % 20 === 0 ? 'mp4' : 'jpg'}`,
  size: 1_000_000 + (id % 977) * 1000,
  mtime: 1_700_000_000 + id,
  origin: 'camera',
  kind: id % 20 === 0 ? 'video' : 'image',
  width: 4000,
  height: 3000,
  checked: role === 'copy',
  role,
})
const groups = Array.from({ length: GROUPS }, (_, g) => {
  const keeper = item(2 * g + 1, 'keeper')
  const copy = item(2 * g + 2, 'copy')
  return { group_id: 2 * g + 1, content_hash: 'ab'.repeat(32), keeper, copies: [copy], reclaimable: copy.size }
})

async function mockApi(page: Page) {
  await page.route('**/api/cleanup/summary', (route: Route) =>
    route.fulfill({
      json: {
        has_backup: true,
        analyzed_at: Math.floor(Date.now() / 1000) - 60,
        new_since: 0,
        storage: { Camera: 4e10, 'WhatsApp images': 1e9, 'WhatsApp video': 2e9, Sent: 1e8, Other: 1e9 },
        cards: [{ tab: 'exact', groups: GROUPS, files: GROUPS, bytes: GROUPS * 1_400_000 }],
      },
    }),
  )
  await page.route('**/api/cleanup/candidates*', (route: Route) => {
    const cursor = Number(new URL(route.request().url()).searchParams.get('cursor') ?? 0)
    const next = cursor + PAGE < GROUPS ? cursor + PAGE : null
    return route.fulfill({ json: { tab: 'exact', groups: groups.slice(cursor, cursor + PAGE), total_groups: GROUPS, next_cursor: next } })
  })
  await page.route('**/api/selection', (route: Route) => route.fulfill({ json: { changed: 1 } }))
  await page.route('**/api/media/**', (route: Route) => route.fulfill({ body: PIXEL, contentType: 'image/gif' }))
}

test.setTimeout(240_000)

// Timing under a busy machine (other e2e workers) can miss the target once; the target itself
// stays strict, so a real slowdown still fails every attempt.
test.describe.configure({ retries: 2 })

test('20,000 items: first paint of the grid and a scroll through all of them', async ({ page, sift }) => {
  await sift.complete()
  await mockApi(page)

  // First paint from a cold start of the page (time since navigation began, app code included).
  await page.addInitScript(() => {
    const w = window as unknown as { __grid?: number }
    const seen = new MutationObserver(() => {
      if (document.querySelector('[data-testid=copy]')) {
        seen.disconnect()
        requestAnimationFrame(() => requestAnimationFrame(() => (w.__grid = performance.now())))
      }
    })
    document.addEventListener('DOMContentLoaded', () => seen.observe(document.body, { childList: true, subtree: true }))
  })
  await page.goto('/cleanup/review?t=e2e')
  await page.waitForFunction(() => (window as unknown as { __grid?: number }).__grid !== undefined)
  const cold = await page.evaluate(() => (window as unknown as { __grid: number }).__grid)

  // First paint when opening Review from the Cleanup home (the app already loaded).
  await page.goto('/cleanup?t=e2e')
  await expect(page.getByRole('button', { name: 'Review' })).toBeVisible()
  const open = await page.evaluate(
    () =>
      new Promise<number>((resolve) => {
        const t0 = performance.now()
        const seen = new MutationObserver(() => {
          if (document.querySelector('[data-testid=copy]')) {
            seen.disconnect()
            requestAnimationFrame(() => requestAnimationFrame(() => resolve(performance.now() - t0)))
          }
        })
        seen.observe(document.body, { childList: true, subtree: true })
        ;(Array.from(document.querySelectorAll('button')).find((b) => b.textContent === 'Review') as HTMLElement).click()
      }),
  )

  // Scroll through everything: 4000 px a frame, until the last group is in view and every page is in.
  const scroll = await page.evaluate(
    () =>
      new Promise<{ frames: number; seconds: number; p50: number; p95: number; max: number; slow: number; maxRows: number }>((resolve) => {
        const el = document.querySelector('[data-testid=review-scroller]') as HTMLElement
        const deltas: number[] = []
        let last = 0
        let maxRows = 0
        const start = performance.now()
        const tick = (now: number) => {
          if (last) deltas.push(now - last)
          last = now
          maxRows = Math.max(maxRows, document.querySelectorAll('[data-testid=group]').length)
          const done = /groups$/.test(document.querySelector('footer p')?.textContent ?? '') && el.scrollTop + el.clientHeight >= el.scrollHeight - 5
          if (done) {
            const sorted = [...deltas].sort((a, b) => a - b)
            const at = (q: number) => sorted[Math.min(sorted.length - 1, Math.floor(q * sorted.length))] as number
            resolve({
              frames: deltas.length,
              seconds: (performance.now() - start) / 1000,
              p50: at(0.5),
              p95: at(0.95),
              max: sorted[sorted.length - 1] as number,
              slow: deltas.filter((d) => d > 50).length,
              maxRows,
            })
            return
          }
          el.scrollTop += 4000
          requestAnimationFrame(tick)
        }
        requestAnimationFrame(tick)
      }),
  )

  const result = { items: GROUPS * 2, groups: GROUPS, firstPaintColdMs: Math.round(cold), firstPaintOpenMs: Math.round(open), scroll }
  console.log(`M4 REVIEW BENCHMARK ${JSON.stringify(result)}`)
  expect(open).toBeLessThan(1000)
  expect(cold).toBeLessThan(2000)
  expect(scroll.maxRows).toBeLessThan(30)
})
