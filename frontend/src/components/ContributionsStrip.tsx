import { PiggyBank } from 'lucide-react'
import { useCurrencySymbol, useFormatCurrency } from '@/lib/CurrencyContext'
import { DeltaChip } from './DeltaChip'
import type { ContributionsResponse, ContributionWindow } from '@/lib/api'

interface ContributionsStripProps {
  data: ContributionsResponse | undefined
  isLoading?: boolean
  isError?: boolean
}

const WINDOW_LABELS: Record<ContributionWindow['label'], string> = {
  all: 'All time',
  '12m': '12M',
  '6m': '6M',
  '3m': '3M',
}

/** Whole units: cents on a monthly average are noise, and the tooltip keeps the exact figure. */
function round(value: number): string {
  return value.toLocaleString('en-US', { maximumFractionDigits: 0 })
}

/**
 * Money in per month, over all time / 12M / 6M / 3M, each trailing window shown as
 * a delta against the all-time average.
 *
 * It is labelled **"Avg Monthly in"** rather than "Avg Monthly" because this app once had
 * two figures called an average per month over two windows on one tab — this one, and
 * `MonthlyDeploymentCard`'s `12M avg: …/mo` of capital *deployed*. Neither was wrong and
 * the only thing distinguishing them lived in a `title` attribute. Both surfaces publish
 * money in now, so the word is no longer load-bearing against that specific collision; it
 * stays because it is what the number is, and because `deployed_eur` has not gone away.
 *
 * Rendered inline inside the Portfolio Value Over Time card header, beside the period
 * metrics — two lines, no card of its own.
 *
 * The headline is spliced at the deposit-coverage boundary: lot cost basis for the
 * pre-IBKR years (which the broker transfer preserved with original dates and cost
 * basis), real deposits from there on. Deposits take over as soon as they exist
 * because lot cost basis cannot survive a rotation — selling one ETF to buy another
 * counts the same money twice.
 *
 * **Deployed rode along as a muted `/suffix` until 2026-09-07** and is now in the `title`
 * only. The argument for surfacing it was that the gap is capital churn rather than
 * saving, so it should be readable without hovering — but that reasoning assumed the
 * reader wanted to know about churn, and the account owner reads this strip for one thing:
 * how much is going in per month. A second figure behind a slash, which a rotation can
 * quadruple, made the one they wanted harder to read.
 */
export function ContributionsStrip({ data, isLoading, isError }: ContributionsStripProps) {
  const curSym = useCurrencySymbol()
  const formatCurrency = useFormatCurrency()

  if (isLoading) {
    // `w-80` was 320px against ~308px of card interior at 390px, so the skeleton
    // itself pushed the page sideways before any data arrived.
    return <div className="ml-0 h-8 w-full max-w-[20rem] animate-pulse rounded bg-muted sm:ml-auto" />
  }

  // Rendering nothing on a failed fetch made the whole strip silently vanish, which is
  // indistinguishable from "this account has no contribution history" — the class of
  // silent failure the rest of the app now refuses.
  if (isError) {
    return (
      <div className="ml-0 flex items-center gap-1.5 pb-0.5 text-xs text-muted-foreground sm:ml-auto">
        <PiggyBank className="h-3.5 w-3.5" />
        <span>Avg Monthly unavailable — the backend didn't respond</span>
      </div>
    )
  }

  if (!data || data.windows.length === 0) return null

  const baseline = data.windows.find(w => w.label === 'all')?.avg_money_in_per_month_eur ?? 0

  const labelTitle = data.coverage_from
    ? `Real deposits from ${data.coverage_from} onward; before that, the cost basis of `
      + `the lots bought each month — which covers the pre-IBKR years because the `
      + `holdings transferred in`
      + (data.transfer_in_date ? ` on ${data.transfer_in_date}` : '')
      + ` kept their original purchase dates. Deposits take over where they exist `
      + `because selling one holding to buy another would otherwise count the same `
      + `money twice. Broker transfers are never counted as money in.`
    : `Cost basis of the lots bought in each period, at the exchange rate on each `
      + `purchase date. Enable 'Deposits & Withdrawals' on the Flex Query to measure `
      + `real deposits instead, which a position switch cannot inflate.`

  return (
    // `ml-auto` only from `sm`: below it the strip is the sole item on its own wrapped
    // row, so pushing it right just left a ragged gutter under the period metrics.
    <div className="ml-0 flex flex-wrap items-end gap-x-5 gap-y-2 sm:ml-auto">
      <div
        className="flex items-center gap-1.5 pb-0.5 text-xs text-muted-foreground"
        title={labelTitle}
      >
        <PiggyBank className="h-3.5 w-3.5" />
        {/* "in", because the figure beside it is money *in* and the card a few hundred
            pixels below reads "12M avg: …/mo" of capital *deployed*. Both were right and
            both were called an average per month, so the only thing separating them was a
            tooltip. */}
        <span className="font-medium">Avg Monthly in</span>
        {/* No `· in / deployed` legend any more, because there is no longer a suffix for
            it to explain. It existed for a real reason while there was one — `CHF2026/2023`
            reads as a single broken number until you know it is two, and a caveat living
            only in `title` does not exist on a phone. Both went together on 2026-09-07. */}
      </div>

      {data.windows.map(w => {
        const delta = w.label !== 'all' && baseline !== 0
          ? ((w.avg_money_in_per_month_eur - baseline) / Math.abs(baseline)) * 100
          : null

        const source = {
          deposits: 'real deposits',
          spliced: `purchases up to ${data.coverage_from}, then real deposits`,
          deployed: 'cost basis of purchases (no deposit ledger yet)',
        }[w.money_in_method]

        const title = [
          `${WINDOW_LABELS[w.label]}: ${formatCurrency(w.money_in_eur)} in over ${w.months.toFixed(1)} months, from ${source}`,
          `${formatCurrency(w.avg_money_in_per_month_eur)} per month`,
          w.money_in_method !== 'deployed'
            ? `${formatCurrency(w.deposits_eur)} of that is deposits`
            : null,
          `${formatCurrency(w.deployed_eur)} deployed into positions over the same period`
            + (w.deployed_eur > w.money_in_eur
              ? ' — the excess is capital rotated between holdings, not new money'
              : ''),
          w.partial ? `* only ${w.months.toFixed(1)} months of history available` : null,
        ].filter(Boolean).join(' · ')

        return (
          <div key={w.label} title={title}>
            {/* The delta sits beside the label rather than on its own line: that is what
                keeps this to two lines inside an already-busy card header. */}
            <div className="flex items-center gap-1 text-[11px] leading-tight text-muted-foreground">
              <span>{WINDOW_LABELS[w.label]}</span>
              {w.money_in_method === 'spliced' && (
                <span className="opacity-60" title="part purchases, part deposits">~</span>
              )}
              {/* DeltaChip rather than a second hand-rolled percentage: this file
                  owned the original colour rule that lib/delta.ts was extracted
                  from, and then kept a competing renderer beside it — `+7%` here
                  against `▲ +7.4%` on the Dividends tab, and no flat band, so a
                  1% move showed a confident green. */}
              {delta !== null && (
                <DeltaChip
                  pct={delta}
                  className="text-[11px]"
                  title={`vs the all-time average of ${formatCurrency(baseline)}/month`}
                />
              )}
            </div>
            <div className="text-sm font-semibold leading-tight tabular-nums">
              {curSym}{round(w.avg_money_in_per_month_eur)}
              {w.partial && <span className="text-muted-foreground">*</span>}
              {/* The `/deployed` suffix that used to sit here came out on 2026-09-07: this
                  strip is read as a contribution rate, and a second figure sharing the
                  slash with it — one that a rotation can quadruple — is not one. It is
                  still in the `title` above, which is where the cross-check belongs. */}
            </div>
          </div>
        )
      })}
    </div>
  )
}
