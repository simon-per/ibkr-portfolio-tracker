import { useEffect, useMemo, useState } from 'react'
import { Crosshair } from 'lucide-react'
import { Card, CardContent } from '@/components/ui/card'
import { CollapsibleCardHeader } from '@/components/ui/CollapsibleCardHeader'
import { DataTable, type Column } from '@/components/ui/DataTable'
import { TickerAvatar } from '@/components/ui/TickerAvatar'
import { useBaseCurrency } from '@/lib/CurrencyContext'
import { readStored, writeStored, useRememberedOpen } from '@/lib/storage'
import { TARGETS_KEY, readTargets } from '@/lib/rebalanceTargets'
import { formatMoneyWhole } from '@/lib/utils'
import {
  DEFAULT_BAND_PP,
  computeRebalancePlan,
  targetShortfallPp,
  type DriftInput,
  type DriftRow,
  type TargetMap,
} from '@/lib/rebalance'
import { displayName } from '@/lib/securityName'

const BAND_KEY = 'allocationBandPp'

function pct(value: number | null, digits = 1): string {
  return value === null ? '—' : `${value.toFixed(digits)}%`
}

function signedPp(value: number | null): string {
  if (value === null) return '—'
  return `${value > 0 ? '+' : ''}${value.toFixed(1)} pp`
}

function driftClass(row: DriftRow): string {
  if (row.withinBand === null) return 'text-muted-foreground'
  if (row.withinBand) return 'text-muted-foreground'
  return row.driftPp! > 0
    ? 'text-amber-700 dark:text-amber-400'
    : 'text-sky-700 dark:text-sky-400'
}

interface RebalanceCardProps {
  positions: DriftInput[] | undefined
  isLoading?: boolean
  isError?: boolean
}

export function RebalanceCard({ positions, isLoading, isError }: RebalanceCardProps) {
  const { baseCurrency } = useBaseCurrency()
  const [targets, setTargets] = useState<TargetMap>(() => readTargets())
  const [bandPp, setBandPp] = useState(() => {
    const saved = Number(readStored(BAND_KEY))
    return Number.isFinite(saved) && saved > 0 && saved <= 50 ? saved : DEFAULT_BAND_PP
  })
  // Open for someone who has already set targets, collapsed for someone who
  // has not — the panel is secondary to the treemaps above it.
  const [open, setOpen] = useRememberedOpen('target-allocation', () => Object.keys(readTargets()).length > 0)

  useEffect(() => {
    writeStored(TARGETS_KEY, JSON.stringify(targets))
  }, [targets])
  useEffect(() => {
    writeStored(BAND_KEY, String(bandPp))
  }, [bandPp])

  const plan = useMemo(
    () => computeRebalancePlan(positions ?? [], targets, bandPp),
    [positions, targets, bandPp],
  )

  const sorted = useMemo(
    () => [...plan.rows].sort((a, b) => b.marketValue - a.marketValue),
    [plan.rows],
  )

  function setTarget(securityId: number, raw: string) {
    setTargets((prev) => {
      const next = { ...prev }
      // Clearing the field removes the target rather than storing 0 — an empty
      // input means "unmanaged", and 0 means "hold none of this".
      if (raw.trim() === '') {
        delete next[securityId]
        return next
      }
      const value = Number(raw)
      if (!Number.isFinite(value) || value < 0 || value > 100) return prev
      next[securityId] = value
      return next
    })
  }

  function adoptCurrentWeights() {
    const next: TargetMap = {}
    for (const row of plan.rows) {
      if (row.currentPct !== null && !row.unheld) {
        next[row.securityId] = Number(row.currentPct.toFixed(1))
      }
    }
    setTargets(next)
  }

  const columns = useMemo<Column<(typeof sorted)[number]>[]>(
    () => [
      {
        key: 'position',
        header: 'Position',
        shortHeader: 'Position',
        mobile: 'title',
        cell: (row, view) =>
          view === 'table' ? (
            <div className="flex items-center gap-2.5">
              <TickerAvatar symbol={row.symbol} securityId={row.securityId} className="h-7 w-7 text-[10px]" />
              <div className="min-w-0">
                <span className="font-medium">{row.symbol}</span>
                <span className="block text-xs text-muted-foreground">
                  {row.unpriced ? 'No price' : displayName(row.description)}
                </span>
              </div>
            </div>
          ) : (
            row.symbol
          ),
      },
      {
        key: 'description',
        header: 'Description',
        shortHeader: 'Description',
        desktop: 'hide',
        mobile: 'meta',
        cell: (row) => (row.unpriced ? 'No price' : displayName(row.description)),
      },
      {
        key: 'value',
        header: 'Value',
        shortHeader: 'Value',
        align: 'right',
        mobile: 'value',
        cell: (row) =>
          row.unpriced ? '—' : formatMoneyWhole(row.marketValue, baseCurrency),
      },
      {
        key: 'drift',
        header: 'Drift',
        shortHeader: 'Drift',
        align: 'right',
        mobile: 'delta',
        tone: (row) => driftClass(row),
        cell: (row) => signedPp(row.driftPp),
      },
      {
        key: 'current',
        header: 'Current',
        shortHeader: 'Current',
        align: 'right',
        cell: (row) => pct(row.currentPct),
      },
      {
        key: 'target',
        header: 'Target',
        shortHeader: 'Target',
        align: 'right',
        // The editable cell. One definition, so the accessible name and the clearing
        // behaviour cannot differ between the table and the card.
        cell: (row) => (
          <input
            type="number"
            min={0}
            max={100}
            step={0.1}
            value={row.targetPct ?? ''}
            onChange={(e) => setTarget(row.securityId, e.target.value)}
            placeholder="—"
            className="w-20 rounded-md border border-input bg-card px-2 py-1 text-right tabular-nums"
            aria-label={`Target weight for ${row.symbol}`}
          />
        ),
      },
      {
        key: 'trade',
        header: 'Trade to close',
        shortHeader: 'Trade to close',
        align: 'right',
        cell: (row) =>
          row.tradeValue === null
            ? '—'
            : `${row.tradeValue > 0 ? '+' : ''}${formatMoneyWhole(row.tradeValue, baseCurrency)}`,
      },
    ],
    // `setTarget` is deliberately out of the deps: it only ever calls setTargets with a
    // functional update, so a stale closure over it cannot go wrong, and including it
    // would rebuild every column on each keystroke.
    [baseCurrency]
  )

  const shortfall = targetShortfallPp(plan)
  const hasTargets = Object.keys(targets).length > 0
  /**
   * `undefined` is "not loaded", not "none held" — an empty array is the latter.
   * Without this the panel drew a whole plan out of an outage: every saved target
   * became an *unheld* row reading "Not currently held", under a header claiming
   * 0 positions outside the band. Two confident falsehoods about the portfolio,
   * which is the failure `e2e/errors.mjs` exists to catch.
   */
  const unavailable = isError || positions === undefined

  const summary = unavailable
    ? "Couldn't load positions, so drift can't be measured."
    : !hasTargets
      ? 'No targets set — enter a weight against any position to start.'
      : plan.balanced
        ? `Every target is within ${bandPp} pp. Nothing to do.`
        : plan.judgedCount === 0
          // Not "0 outside the band": nothing could be compared at all, which is a
          // different statement and must not read as an all-clear.
          ? 'No target could be compared against a current weight yet.'
          : `${plan.rows.filter((r) => r.withinBand === false).length} position(s) outside the ${bandPp} pp band.`

  return (
    <Card>
      <CollapsibleCardHeader
        open={open}
        onToggle={() => setOpen((v) => !v)}
        title="Target Allocation & Drift"
        icon={Crosshair}
        description={summary}
        contentId="rebalance-panel"
      />
      {open && (
        <CardContent id="rebalance-panel" className="space-y-4">
          {isLoading ? (
            <div className="h-24 bg-muted animate-pulse rounded" />
          ) : unavailable ? (
            // No table at all. Rendering one from an empty position list is what
            // produced the "Not currently held" rows.
            <p className="text-sm text-red-800 dark:text-red-200">
              Couldn't load your positions — the server didn't respond, so current weights
              and drift are unavailable. Your saved targets are untouched. It retries
              automatically.
            </p>
          ) : (
            <>
              <div className="flex flex-wrap items-center gap-4 text-sm">
                <label className="flex items-center gap-2">
                  <span className="text-muted-foreground">Tolerance band</span>
                  <input
                    type="number"
                    min={1}
                    max={50}
                    step={1}
                    value={bandPp}
                    onChange={(e) => {
                      const v = Number(e.target.value)
                      if (Number.isFinite(v) && v > 0 && v <= 50) setBandPp(v)
                    }}
                    className="w-16 rounded-md border border-input bg-card px-2 py-1 text-right"
                    aria-label="Tolerance band in percentage points"
                  />
                  <span className="text-muted-foreground">pp</span>
                </label>
                <button
                  type="button"
                  onClick={adoptCurrentWeights}
                  className="rounded border px-2 py-1 hover:bg-muted"
                >
                  Adopt current weights
                </button>
                <button
                  type="button"
                  onClick={() => setTargets({})}
                  disabled={!hasTargets}
                  className="rounded border px-2 py-1 hover:bg-muted disabled:opacity-50"
                >
                  Clear targets
                </button>
              </div>

              {/*
                Every caveat below rides on a *successful* render, so it is
                invisible unless stated. Targets summing to anything but 100 is
                not corrected — see lib/rebalance.ts.
              */}
              {hasTargets && Math.abs(shortfall) > 0.05 && (
                <p className="text-sm text-amber-700 dark:text-amber-400">
                  Targets sum to {plan.targetSumPct.toFixed(1)}%, not 100%.{' '}
                  {shortfall < 0
                    ? 'The trades below will therefore not net to zero.'
                    : 'More is targeted than exists, so these trades cannot all be made.'}
                </p>
              )}
              {plan.unpricedCount > 0 && (
                <p className="text-sm text-amber-700 dark:text-amber-400">
                  {plan.unpricedCount} position(s) have no cached price, so they carry no weight
                  here and the portfolio total is understated. Drift cannot be judged for them.
                </p>
              )}
              {hasTargets && plan.unmanagedPct > 0.05 && (
                <p className="text-sm text-muted-foreground">
                  {plan.unmanagedPct.toFixed(1)}% of the portfolio's current value has no target
                  and is left alone.
                </p>
              )}

              <DataTable
                rows={sorted}
                columns={columns}
                getRowKey={(row) => row.securityId}
                label="Target allocation and drift by position"
                density="normal"
              />
            </>
          )}
        </CardContent>
      )}
    </Card>
  )
}
