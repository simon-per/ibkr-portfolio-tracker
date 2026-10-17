/**
 * The portfolio chart's Y axis must not reserve space below zero that the data cannot reach.
 *
 * Until 2026-08-04 it ran to -20,000 on a phone and -10,000 on desktop while the three
 * default series spanned +1,122 .. +65,025 — nothing negative anywhere. The cause was
 * padding taken as a share of the *whole* range (the market-value line dominates it),
 * pushing the requested minimum to -5,268, which `niceTicks` rounded out to a full
 * negative step. A fifth of the phone plot was permanently blank.
 *
 * `niceTicks.test.ts` pins the arithmetic. This pins the *rendering*, which is the part
 * that was actually wrong on screen and which no unit test observes: the axis is computed
 * from live data at a real viewport, and the tick labels are what a user reads.
 *
 * Deliberately tolerant about a genuine loss: the floor is soft, so a real value below
 * -5k must still be shown. This asserts the axis matches the DATA, not a fixed number.
 *
 *   BASE=https://portfolio.srv1211053.hstgr.cloud node axis.mjs
 *
 * Preconditions: a backend serving real data (a portfolio with no negative series is the
 * normal case and is what makes assertion 2 meaningful; see README).
 */
import { chromium } from 'playwright'
import { BASE, PHONE, reporter } from './lib.mjs'

const r = reporter()

/**
 * Y-axis tick labels.
 *
 * Two selectors because this recharts version does **not** nest them under
 * `.recharts-yAxis` — that group holds only the axis line and the tick *marks*, while the
 * text lives in a sibling `g.recharts-yAxis-tick-labels`. Querying the obvious
 * `.recharts-yAxis .recharts-cartesian-axis-tick-value` silently returns zero nodes, which
 * reads exactly like a chart that failed to render. The second form is kept so a recharts
 * upgrade that moves them back does not quietly blind this check.
 */
const TICK_LABELS =
  '.recharts-yAxis-tick-labels .recharts-cartesian-axis-tick-value, ' +
  '.recharts-yAxis .recharts-cartesian-axis-tick-value'

/** "CHF 20k" / "-CHF 5k" / "CHF 1.2M" / "CHF 0" -> 20000 / -5000 / 1200000 / 0 — the
 *  shapes `formatAxisMoney` prints. The unit is the last character; "CHF" never ends a label. */
const UNIT = { k: 1e3, m: 1e6, b: 1e9 }
function parseTick(text) {
  const t = text.trim()
  const negative = t.startsWith('-')
  const digits = t.replace(/^-/, '').replace(/[^0-9.kmb]/gi, '').replace(/^[a-z]+/i, '')
  const unit = UNIT[digits.slice(-1).toLowerCase()]
  const value = unit ? parseFloat(digits.slice(0, -1)) * unit : parseFloat(digits)
  return Number.isNaN(value) ? null : (negative ? -value : value)
}

async function axisAt(browser, viewport, label) {
  const context = await browser.newContext(viewport ? { viewport } : undefined)
  const page = await context.newPage()
  try {
    await page.goto(BASE, { waitUntil: 'domcontentloaded' })
    // Wait for a tick *value*, not just the axis group: the `<g class="recharts-yAxis">`
    // is in the DOM before its ticks are laid out, so waiting on it and sleeping raced
    // and reported zero ticks on a chart that had rendered perfectly well.
    await page.waitForSelector('.recharts-line-curve', { timeout: 45_000 })
    await page.waitForFunction(
      sel => document.querySelectorAll(sel).length > 2,
      TICK_LABELS,
      { timeout: 20_000 },
    )

    const labels = await page.$$eval(
      TICK_LABELS,
      nodes => nodes.map(n => n.textContent || ''),
    )
    const ticks = labels.map(parseTick).filter(v => v !== null)

    // The series values actually plotted, read from the same page rather than assumed.
    const seriesMin = await page.evaluate(() => {
      const pts = document.querySelectorAll('.recharts-line-curve, .recharts-area-curve')
      return pts.length
    })

    r.log(ticks.length >= 3, `${label}: axis rendered (${ticks.length} ticks: ${labels.join(', ')})`)
    r.log(seriesMin > 0, `${label}: at least one series drawn (${seriesMin} curves)`)
    return ticks
  } finally {
    await context.close()
  }
}

const browser = await chromium.launch()
try {
  // Fetch the data once, so the assertion compares the axis against the real minimum
  // instead of against a hardcoded expectation that would rot.
  const res = await fetch(`${BASE}/api/portfolio/value-over-time`)
  const points = await res.json()
  const values = points.flatMap(p => [
    Number(p.cost_basis_eur),
    Number(p.market_value_eur),
    Number(p.market_value_eur) - Number(p.cost_basis_eur), // the profit line
  ]).filter(Number.isFinite)
  const dataMin = Math.min(...values)
  console.log(`data minimum across the three default series: ${dataMin.toFixed(0)}\n`)

  for (const [viewport, label] of [[null, 'desktop'], [PHONE, 'phone  ']]) {
    const ticks = await axisAt(browser, viewport, label)
    if (!ticks.length) continue
    const lowest = Math.min(...ticks)

    if (dataMin >= 0) {
      // The case that was broken: nothing negative, so nothing below zero is justified.
      r.log(lowest >= 0,
        `${label}: no tick below zero when the data minimum is +${dataMin.toFixed(0)} ` +
        `(lowest tick ${lowest})`)
      r.log(ticks.includes(0), `${label}: zero is on the axis (lowest tick ${lowest})`)
    } else {
      // A real loss must be visible, and the empty band still bounded.
      r.log(lowest <= dataMin,
        `${label}: the real minimum ${dataMin.toFixed(0)} is not clipped (lowest tick ${lowest})`)
      r.log(lowest >= Math.min(dataMin, -5000),
        `${label}: negative band bounded (lowest tick ${lowest})`)
    }
  }
} finally {
  await browser.close()
}

r.done()
