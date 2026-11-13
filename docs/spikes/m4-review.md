# M4: Review screen with 20,000 items

Playwright (WebKit, headless) test `web/e2e/review-benchmark.spec.ts`. The API is mocked with
`page.route`: 10,000 groups, each a keeper and one copy (20,000 items, every 20th a video), served
200 groups a page; thumbnails are a 1-pixel image. The test prints a line starting
`M4 REVIEW BENCHMARK`; two runs on the development Mac, the second inside `make e2e`.

| Measure | Result | Target |
|---|---|---|
| First paint of the grid, page opened cold (from navigation start, app code included) | 166 to 315 ms (two runs) | under 1 s |
| First paint of the grid, Review opened from Cleanup home (click to first tile painted) | 25 to 41 ms | under 1 s |
| Scroll through all 20,000 items (873 frames at 4,000 px a frame, 50 pages loaded on the way) | 14.6 s | smooth |
| Frame time while scrolling, median / 95th percentile / worst | 17 / 17 to 18 / 28 to 32 ms | no frame over 50 ms |
| Frames over 50 ms | 0 | 0 |
| Most group rows in the page at once | 7 | windowed |

Only the rows in view (plus 600 px each side) are drawn; the scroll bar covers all groups, with
the ones not loaded yet counted at a standard row height. Further pages load when the loaded
end comes within 1,500 px of the view.
