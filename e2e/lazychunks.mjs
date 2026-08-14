/**
 * The code-split is real: verified against the BUILT output, not the dev server.
 *
 * Chunk boundaries are a property of the build, so no unit test can observe them and the
 * dev server does not reproduce them. The two things worth protecting:
 *
 *  - the seven non-default tabs must NOT be in the first paint (that is the whole point);
 *  - Recharts MUST be, because three components on the default Performance tab use it —
 *    a future "optimisation" that lazies it would only move the wait, and this fails if
 *    someone tries.
 *
 * Needs: `npm run build && npx vite preview --port 4173` in frontend/. No backend
 * required — the panels render their error states, which still proves the chunk arrived
 * and the component mounted.
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

const jsRequests = []
page.on('response', r => {
  const u = r.url()
  if (/\/assets\/.*\.js$/.test(u)) jsRequests.push({ file: u.split('/').pop(), status: r.status() })
})

await applyProductionCsp(page)
await page.goto(PREVIEW + '/', { waitUntil: 'networkidle' })
await page.waitForTimeout(2500)

log((await page.getByRole('tab').count()) === TABS.length, 'app renders under the production CSP')

const initial = jsRequests.map(r => r.file)
const TAB_TO_CHUNK = {
  Activity: 'ActivityTab', Allocation: 'AllocationTab', Dividends: 'DividendsTab',
  Fundamentals: 'FundamentalsTab', Watchlist: 'WatchlistTab', Forecast: 'ForecastTab',
  Tax: 'TaxTab', 'Look-through': 'LookThroughTab',
}

const eager = Object.values(TAB_TO_CHUNK).filter(n => initial.some(f => f.startsWith(n + '-')))
log(eager.length === 0, `no deferred tab chunk is in the first paint (eager: ${eager.join(',') || 'none'})`)
log(initial.some(f => f.startsWith('charts-')), 'charts chunk IS eager — the default tab needs it at first paint')
log(initial.some(f => f.startsWith('react-')), 'react is its own chunk, not folded into the app bundle')

for (const [tab, chunk] of Object.entries(TAB_TO_CHUNK)) {
  const before = jsRequests.length
  await page.getByRole('tab', { name: tab, exact: true }).click()
  await page.waitForTimeout(1800)

  const fetched = jsRequests.slice(before).filter(r => r.file.startsWith(chunk + '-'))
  log(fetched.length > 0, `${tab}: chunk requested on click`)
  log(fetched.every(r => r.status === 200), `${tab}: chunk served 200 (${fetched.map(r => r.status).join(',') || 'none'})`)
  log((await page.getByText(new RegExp(`Loading ${tab}`)).count()) === 0, `${tab}: suspense fallback cleared`)
  log((await page.getByText(new RegExp(`${tab} couldn't load`)).count()) === 0, `${tab}: mounted without a chunk error`)
}

log(violations.length === 0, `no CSP violations or page errors (${violations.length}): ${violations.slice(0, 3).join(' | ')}`)

done()
console.log(`\n${jsRequests.length} JS chunks: ${jsRequests.map(r => r.file).join(', ')}`)
await browser.close()
