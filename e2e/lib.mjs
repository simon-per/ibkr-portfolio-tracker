/** Shared plumbing for the browser checks. Kept tiny on purpose. */
import { chromium } from 'playwright'

export const BASE = process.env.BASE || 'http://localhost:5173'
export const PREVIEW = process.env.PREVIEW || 'http://localhost:4173'

/** The production CSP, verbatim from `backend/frontend-nginx.conf`. */
export const CSP =
  "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; " +
  "img-src 'self' data:; font-src 'self' data:; connect-src 'self'; " +
  "frame-ancestors 'self'; base-uri 'self'; form-action 'self'; object-src 'none'"

export const TABS = [
  'Performance', 'Activity', 'Allocation', 'Look-through', 'Dividends',
  'Fundamentals', 'Watchlist', 'Forecast', 'Tax',
]

export function reporter() {
  const out = []
  return {
    log(ok, msg) {
      out.push(`${ok ? 'PASS' : 'FAIL'}  ${msg}`)
      if (!ok) process.exitCode = 1
    },
    done() {
      console.log(out.join('\n'))
      const failed = out.filter(l => l.startsWith('FAIL')).length
      console.log(`\n${out.length - failed}/${out.length} passed`)
    },
  }
}

/** An iPhone-class viewport — the size the mobile layout is designed against. */
export const PHONE = {
  width: 390,
  height: 844,
  isMobile: true,
  hasTouch: true,
  deviceScaleFactor: 3,
}

/** A page that records console errors and uncaught exceptions for later assertion. */
export async function openPage({
  width = 1500,
  height = 1000,
  isMobile,
  hasTouch,
  deviceScaleFactor,
} = {}) {
  const browser = await chromium.launch()
  const page = await browser.newPage({
    viewport: { width, height },
    // Spread only when asked for, so every existing caller gets byte-identical context
    // options. `isMobile` switches Chromium to the mobile viewport model (visual
    // viewport plus meta-viewport handling), which changes how documentElement
    // .scrollWidth is reported — the exact number mobile.mjs asserts on.
    ...(isMobile !== undefined && { isMobile }),
    ...(hasTouch !== undefined && { hasTouch }),
    ...(deviceScaleFactor !== undefined && { deviceScaleFactor }),
  })
  const errors = []
  page.on('console', m => { if (m.type() === 'error') errors.push(m.text()) })
  page.on('pageerror', e => errors.push('pageerror: ' + e.message))
  return { browser, page, errors }
}

/** Serve the document with the production CSP applied, as nginx does. */
export async function applyProductionCsp(page) {
  await page.route('**/*', async route => {
    const r = await route.fetch()
    const headers = { ...r.headers() }
    if ((headers['content-type'] || '').includes('text/html')) {
      headers['content-security-policy'] = CSP
    }
    await route.fulfill({ response: r, headers })
  })
}
