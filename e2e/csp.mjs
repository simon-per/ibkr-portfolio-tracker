/**
 * The app renders under the production Content-Security-Policy.
 *
 * The CSP lives in nginx, so nothing in the dev server or the test suite exercises it —
 * it is only ever true or false in a browser. SVG charts, inline styles and the
 * data-URI favicon are the usual casualties.
 *
 * **Runs against the BUILT output, never the dev server.** Vite's dev server injects an
 * inline `<script type="module">` for react-refresh, which `script-src 'self'` blocks by
 * design — so the app never boots and the check reports a violation that cannot exist in
 * production, where that script is not emitted. Pointing this at :5173 tests Vite's HMR
 * transport, not our policy.
 *
 * Needs: `npm run build && npx vite preview --port 4173` in frontend/. No backend
 * required — panels render error states, which still exercises the CSP.
 */
import { PREVIEW, TABS, applyProductionCsp, openPage, reporter } from './lib.mjs'

const { log, done } = reporter()
const { browser, page } = await openPage()

const violations = []
page.on('console', m => {
  const t = m.text()
  if (/Content Security Policy|Refused to/i.test(t)) violations.push(t)
})
page.on('pageerror', e => violations.push('pageerror: ' + e.message))

await applyProductionCsp(page)

await page.goto(PREVIEW + '/', { waitUntil: 'networkidle' })
await page.waitForTimeout(4000)

const tabs = await page.getByRole('tab').count()
log(tabs === TABS.length, `app renders under the production CSP (${tabs} tabs)`)
if (tabs !== TABS.length) {
  // Almost always the wrong target rather than a real policy failure.
  log(false, 'is PREVIEW pointing at `vite preview` (built output) rather than the dev server?')
}

// The recharts-heavy tabs: SVG plus inline styles.
for (const t of ['Performance', 'Allocation', 'Dividends', 'Forecast', 'Activity']) {
  await page.getByRole('tab', { name: t, exact: true }).click()
  await page.waitForTimeout(2500)
}
const svgs = await page.locator('svg').count()
log(svgs > 0, `charts and icons render (${svgs} svg elements)`)

const favicon = await page.locator('link[rel="icon"]').getAttribute('href')
log((favicon || '').startsWith('data:image/svg+xml'), 'the inline data-URI favicon is allowed by img-src')

log(violations.length === 0, `no CSP violations (${violations.length}): ${violations.slice(0, 3).join(' | ')}`)

done()
await browser.close()
