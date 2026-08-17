/**
 * The Activity ledger, against REAL data.
 *
 * `CLAUDE.md` says to audit the transfer rows before trusting any money-added figure: an
 * incoming transfer booked as a deposit shows a portfolio-sized fake contribution, and
 * that audit used to be `manage_cash_flows list` over ssh. These assert the UI now
 * answers it — and that the badge is present, not merely the row.
 *
 * Needs: frontend dev server on BASE, backend pointed at a PRODUCTION SNAPSHOT.
 * The checked-in `portfolio.db` predates trades, cash flows and the IBKR dividend era,
 * so it exercises none of these shapes and every assertion below would fail on it.
 * Delete the snapshot afterwards — it is real account data.
 */
import { BASE, openPage, reporter } from './lib.mjs'

const { log, done } = reporter()
const { browser, page, errors } = await openPage()

await page.goto(BASE, { waitUntil: 'networkidle' })
await page.waitForTimeout(2000)
await page.getByRole('tab', { name: 'Activity', exact: true }).click()
await page.waitForTimeout(4000)

const panel = await page.getByRole('tabpanel').innerText()

// The trade-shaped assertions read the UNFILTERED panel, so they must come first.
// The three defects the prod-data pass caught, each pinned so they cannot come back.
log(!/\b0\.00\b.*realized/i.test(panel), 'no BUY row asserts a 0.00 realized result')
const region = page.getByRole('region', { name: /Activity table/ })
log((await region.count()) === 1, 'the wide table is a named, keyboard-reachable scroll region')

// Fractional share counts must survive: this account trades 0.5 SOXQ, 0.3 MU, 0.1 CSU.
// A quantity column of bare "0" or "-0" is the rounding bug.
const zeroQty = (panel.match(/(^|\s)-?0(\s|$)/gm) || []).length
log(zeroQty === 0, `no quantity rounds to 0 or -0 (found ${zeroQty})`)

// Narrow to cash before asserting anything about transfers.
//
// These two used to read the default panel and went red as the account aged rather than
// because anything broke: the only transfer is the in-kind arrival of 2026-01-21, the
// default `1Y` window still reaches it, but the window now holds ~175 rows and the
// transfers are the oldest of them — so they sit past the first page of 100 and simply
// are not in the panel text. Hoping they land on page one is not an assertion.
//
// The kind filter is what makes it stable as rows accumulate: the account has 47 cash
// rows against a PAGE_SIZE of 100, and trades are what grow.
await page.getByRole('button', { name: 'Cash', exact: true }).click()
await page.waitForTimeout(3000)
const cash = await page.getByRole('tabpanel').innerText()

// Assert the filter actually took, or the three checks below could pass on an unfiltered
// panel and the fix above would be undone silently by a renamed button.
log(!/Dividend/.test(cash), 'the Cash filter narrowed the ledger to cash rows')

log(/Transfer/.test(cash), 'a Transfer row is visible in the ledger')
log(/not money in/.test(cash), 'transfers are badged "not money in"')
log(/Deposit/.test(cash), 'a Deposit row is visible')

log(errors.length === 0, `no console errors (${errors.length}): ${errors.slice(0, 3).join(' | ')}`)

done()
await browser.close()
