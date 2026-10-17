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
 * from live data at a real viewport, and the plot is what a user reads.
 *
 * **It measures what is drawn, never what the API serves** (2026-10-17). It used to take
 * the minimum of the API's all-time cost, value and value − cost, but the chart plots the
 * default *range*, and its profit line is rebased to 0 on the range's first point
 * (`rangeProfit.ts`) — so the API's minimum was a number the chart never draws, and the
 * check failed against a correct chart. It also compared against the lowest tick *label*,
 * while `axisFloor` can end the plot a little below its lowest label (the soft −5k band,
 * unlabelled when it is narrower than a tick step). Both now come from the SVG: the plotted
 * lines' extent from their geometry, the axis bottom from recharts' plot-area clip rect,
 * converted to money through the tick labels' own positions.
 *
 * Deliberately tolerant about a genuine loss: the floor is soft, so a real value below
 * -5k must still be shown. This asserts the axis matches the drawn data, not a fixed number.
 *
 *   BASE=https://portfolio.srv1211053.hstgr.cloud node axis.mjs
 *
 * Preconditions: a backend serving real data (a portfolio with no negative series is the
 * normal case and is what makes the zero-floor assertion meaningful; see README).
 */
import { chromium } from 'playwright'
import { BASE, PHONE, reporter } from './lib.mjs'

const r = reporter()

/** `PortfolioValueChart`'s `NEGATIVE_AXIS_FLOOR`: how far below zero the axis may go
 *  without a real value down there. */
const NEGATIVE_AXIS_FLOOR = -5000

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

/** The stroked lines: an `<Area>`'s outline and every `<Line>`. Not `.recharts-area-area`,
 *  the fill, which runs down to the baseline whatever the data. */
const CURVES = '.recharts-line-curve, .recharts-area-curve'

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

/**
 * The value chart's geometry, in SVG pixels: each Y tick's label and position, the
 * plot area's top and bottom, and the highest and lowest point any line reaches. Scoped
 * to the first chart that draws an area — the value chart's headline series — so a
 * second chart further down the tab cannot lend it ticks.
 */
function readGeometry({ ticksSel, curvesSel }) {
  const svg = [...document.querySelectorAll('svg.recharts-surface')]
    .find(s => s.querySelector('.recharts-area-curve'))
  if (!svg) return null
  const clip = svg.querySelector('defs clipPath rect')
  const ticks = [...svg.querySelectorAll(ticksSel)]
    .map(n => ({ text: n.textContent || '', y: Number(n.getAttribute('y')) }))
  const boxes = [...svg.querySelectorAll(curvesSel)].map(c => c.getBBox())
  return {
    ticks,
    curves: boxes.length,
    plotTop: clip ? Number(clip.getAttribute('y')) : null,
    plotBottom: clip ? Number(clip.getAttribute('y')) + Number(clip.getAttribute('height')) : null,
    drawnTop: boxes.length ? Math.min(...boxes.map(b => b.y)) : null,
    drawnBottom: boxes.length ? Math.max(...boxes.map(b => b.y + b.height)) : null,
  }
}

async function axisAt(browser, viewport, label) {
  const context = await browser.newContext(viewport ? { viewport } : undefined)
  const page = await context.newPage()
  try {
    await page.goto(BASE, { waitUntil: 'domcontentloaded' })
    // Wait for a tick *value*, not just the axis group: the `<g class="recharts-yAxis">`
    // is in the DOM before its ticks are laid out, so waiting on it and sleeping raced
    // and reported zero ticks on a chart that had rendered perfectly well.
    await page.waitForSelector('.recharts-area-curve', { timeout: 45_000 })
    await page.waitForFunction(
      sel => document.querySelectorAll(sel).length > 2,
      TICK_LABELS,
      { timeout: 20_000 },
    )

    // The lines animate in from the baseline, so a single read can catch one mid-flight
    // and report a minimum the data never had. Read until two reads agree.
    const args = { ticksSel: TICK_LABELS, curvesSel: CURVES }
    let geo = await page.evaluate(readGeometry, args)
    for (let i = 0; i < 20; i++) {
      await page.waitForTimeout(250)
      const next = await page.evaluate(readGeometry, args)
      const settled = JSON.stringify(next) === JSON.stringify(geo)
      geo = next
      if (settled) break
    }

    const labels = (geo?.ticks ?? []).map(t => t.text)
    const ticks = (geo?.ticks ?? [])
      .map(t => ({ value: parseTick(t.text), y: t.y }))
      .filter(t => t.value !== null && Number.isFinite(t.y))
      .sort((a, b) => a.value - b.value)

    r.log(ticks.length >= 3, `${label}: axis rendered (${ticks.length} ticks: ${labels.join(', ')})`)
    r.log((geo?.curves ?? 0) > 0, `${label}: at least one series drawn (${geo?.curves ?? 0} curves)`)
    r.log(geo?.plotBottom != null, `${label}: plot area found`)
    if (ticks.length < 2 || geo?.plotBottom == null || geo.drawnBottom == null) return

    // Pixels to money through the labels' own positions: the lowest and highest tick.
    const lo = ticks[0]
    const hi = ticks[ticks.length - 1]
    const perPx = (hi.value - lo.value) / (lo.y - hi.y)
    const valueAt = y => lo.value + (lo.y - y) * perPx
    // A path's bounding box ignores its stroke and recharts rounds its offsets, so allow
    // a pixel and a half either way before calling anything clipped or reserved.
    const slackPx = 1.5
    const slack = slackPx * perPx
    const step = ticks[1].value - ticks[0].value

    const drawnMin = valueAt(geo.drawnBottom)
    const axisMin = valueAt(geo.plotBottom)
    const fmt = v => Math.round(v).toLocaleString('en-US')
    console.log(`${label}: drawn minimum ${fmt(drawnMin)}, axis bottom ${fmt(axisMin)}, ` +
      `lowest tick ${fmt(lo.value)}, tick step ${fmt(step)}`)

    r.log(geo.drawnBottom <= geo.plotBottom + slackPx,
      `${label}: nothing drawn below the plot (drawn minimum ${fmt(drawnMin)}, axis bottom ${fmt(axisMin)})`)
    r.log(geo.drawnTop >= geo.plotTop - slackPx,
      `${label}: nothing drawn above the plot`)

    if (drawnMin >= -slack) {
      // The case that was broken: nothing negative drawn, so nothing below zero is justified.
      r.log(axisMin >= -slack,
        `${label}: no axis below zero when nothing drawn is negative (axis bottom ${fmt(axisMin)})`)
      r.log(ticks.some(t => t.value === 0), `${label}: zero is on the axis (lowest tick ${fmt(lo.value)})`)
    } else {
      // A real loss must be visible, and the empty band under it still bounded: the axis
      // stops at the soft floor, or at the drawn minimum when that is lower — never a whole
      // negative tick step below what is drawn.
      r.log(axisMin >= Math.min(drawnMin, NEGATIVE_AXIS_FLOOR) - slack,
        `${label}: negative band bounded (axis bottom ${fmt(axisMin)}, drawn minimum ${fmt(drawnMin)})`)
      r.log(axisMin > drawnMin - step,
        `${label}: less than one tick step of empty axis under the drawn minimum`)
      r.log(ticks.some(t => t.value === 0), `${label}: zero is on the axis (lowest tick ${fmt(lo.value)})`)
    }
  } finally {
    await context.close()
  }
}

const browser = await chromium.launch()
try {
  for (const [viewport, label] of [[null, 'desktop'], [PHONE, 'phone  ']]) {
    await axisAt(browser, viewport, label)
  }
} finally {
  await browser.close()
}

r.done()
