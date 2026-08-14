/**
 * Keyboard and ARIA sweep: the tab strip, the five collapsible cards, sortable headers
 * and the rebalance panel's inputs.
 *
 * Four cards put `onClick` on a `<CardHeader>` div and two tables put it on a `<th>`, so
 * those surfaces were mouse-only; `ui/tabs.tsx` had no ARIA at all. Unit tests pin the
 * shared components in isolation — this pins that the real page wires them up.
 *
 * Needs: frontend dev server on BASE **and a backend with data**. This said "backend
 * optional (nothing here reads data)" and it was never true: `aria-sort` headers only
 * exist once Fundamentals and Watchlist have rows, the footer assertion reads `/health`,
 * and a refused connection fills the console so the zero-errors check fails too. Without
 * one it reports 11/14 and all three failures are phantoms. The checked-in `portfolio.db`
 * is enough — this needs positions, not realistic ones.
 */
import { BASE, TABS, openPage, reporter } from './lib.mjs'

const { log, done } = reporter()
const { browser, page, errors } = await openPage({ width: 1440, height: 950 })

await page.goto(BASE, { waitUntil: 'networkidle' })
await page.waitForTimeout(2500)

log((await page.title()) === 'Portfolio Analyzer', `title is "${await page.title()}"`)

const tablist = page.getByRole('tablist')
log((await tablist.count()) === 1, 'exactly one tablist')

const tabs = page.getByRole('tab')
const tabCount = await tabs.count()
log(tabCount === TABS.length, `${TABS.length} tabs present (got ${tabCount})`)

// Roving tabIndex: the strip is one tab stop, arrows move within it.
await tabs.first().focus()
await page.keyboard.press('ArrowRight')
await page.waitForTimeout(600)
const afterRight = await page.getByRole('tab', { selected: true }).innerText()
log(afterRight === 'Activity', `ArrowRight selects Activity (got ${afterRight})`)

await page.waitForTimeout(2500)
log((await page.getByRole('tabpanel').count()) === 1, 'exactly one tabpanel is exposed')

await page.getByRole('tab', { selected: true }).focus()
await page.keyboard.press('End')
await page.waitForTimeout(600)
const afterEnd = await page.getByRole('tab', { selected: true }).innerText()
log(afterEnd === 'Tax', `End jumps to the last tab (got ${afterEnd})`)

await page.keyboard.press('Home')
await page.waitForTimeout(2500)
const afterHome = await page.getByRole('tab', { selected: true }).innerText()
log(afterHome === 'Performance', `Home returns to the first tab (got ${afterHome})`)

// The four collapsibles on the Performance tab must be real buttons that Enter
// toggles. The fifth, on Allocation, is checked at the end — see below.
for (const name of ['Monthly Returns', 'Monthly Deployment', 'Dividend Income', 'Performance Attribution']) {
  const btn = page.getByRole('button', { name: new RegExp(name) }).first()
  if ((await btn.count()) === 0) { log(false, `${name}: header is not a button`); continue }
  const before = await btn.getAttribute('aria-expanded')
  await btn.focus()
  await page.keyboard.press('Enter')
  await page.waitForTimeout(500)
  const after = await btn.getAttribute('aria-expanded')
  log(before === 'false' && after === 'true', `${name}: Enter toggles aria-expanded (${before} -> ${after})`)
}

const sorted = await page.locator('th[aria-sort]').count()
log(sorted > 0, `${sorted} headers carry aria-sort`)

const footer = await page.locator('footer').innerText().catch(() => '')
log(/Portfolio Analyzer v/.test(footer), 'footer reports the running build')

// The rebalance panel lives on Allocation rather than Performance, so it is
// checked last: switching tabs would otherwise change what the aria-sort count
// above is measuring. Its controls are the only editable inputs in the app, so
// an accessible name on each is the whole of its keyboard story.
await page.getByRole('tab', { name: 'Allocation' }).click()
await page.waitForTimeout(1200)

const rebalance = page.getByRole('button', { name: /Target Allocation/ }).first()
if ((await rebalance.count()) === 0) {
  log(false, 'Target Allocation: header is not a button')
} else {
  const before = await rebalance.getAttribute('aria-expanded')
  await rebalance.focus()
  await page.keyboard.press('Enter')
  await page.waitForTimeout(500)
  const after = await rebalance.getAttribute('aria-expanded')
  // It opens pre-expanded once targets exist in this browser, so accept either
  // direction rather than assuming a fresh profile.
  log(before !== after, `Target Allocation: Enter toggles aria-expanded (${before} -> ${after})`)

  if (after === 'true') {
    const band = page.getByLabel('Tolerance band in percentage points')
    log(await band.count() === 1, 'the tolerance band input has an accessible name')

    const targets = page.getByLabel(/^Target weight for /)
    const count = await targets.count()
    // No positions in the local DB means no target rows to label — say so
    // rather than passing on an empty set.
    log(count > 0, `${count} target inputs carry an accessible name`)
  }
}

log(errors.length === 0, `no console errors (${errors.length}): ${errors.slice(0, 3).join(' | ')}`)

done()
await browser.close()
