import { Button } from '@/components/ui/button'
import type { SyncResponse } from '@/lib/api'
import { formatShortDateTime } from '@/lib/utils'

/**
 * What the header says after you press Sync.
 *
 * Extracted from `Dashboard` when the skip state was added, because there are now three
 * outcomes rather than two and the middle one is the easy thing to get wrong: a skipped
 * sync carries no `securities_synced`, so the old unconditional
 * `Securities: {data.securities_synced}` would have rendered "Securities: undefined"
 * under a green tick. Inside Dashboard that branch could only be reached by mounting a
 * dozen queries; here it is a props table.
 *
 * **The skip is neutral — not green, not red — and that is the whole point of it.**
 * IBKR issues about one Flex statement per US-Eastern day, so pressing Sync again after
 * the day's run is neither a success nor a failure but a no-op. It used to render as a
 * red "✗ Sync failed", because the only way to find out was to ask IBKR and be refused,
 * and each refusal spent `Code=1025` lockout budget to produce that banner.
 */
export interface SyncStatusMessageProps {
  data?: SyncResponse
  /** The mutation's error, if it failed. */
  error?: { message: string } | null
  isPending: boolean
  /** Re-run with `force`, re-asking IBKR for a statement it says it already made. */
  onForce: () => void
}

export function SyncStatusMessage({ data, error, isPending, onForce }: SyncStatusMessageProps) {
  if (error) {
    return (
      <div className="mt-4 p-4 bg-red-50 dark:bg-red-950 border border-red-200 dark:border-red-800 rounded-lg">
        <p className="text-sm text-red-800 dark:text-red-200">✗ Sync failed: {error.message}</p>
      </div>
    )
  }

  if (!data) return null

  if (data.status === 'skipped') {
    return (
      <div className="mt-4 p-4 bg-muted border rounded-lg">
        <p className="text-sm text-muted-foreground">⏸ Already up to date. {data.message}</p>
        {data.next_attempt_after && (
          <p className="mt-1 text-sm text-muted-foreground">
            Next statement available {formatShortDateTime(data.next_attempt_after)}
            {data.last_success_at && <> · last synced {formatShortDateTime(data.last_success_at)}</>}.
          </p>
        )}
        {/* The only path to a second generation in one day: the IBKR portal resets it
            when the query definition is edited. Deliberately understated and captioned
            with its cost, so it does not become the reflex next click — a forced attempt
            that fails is a failed generation, which is what Code=1025 counts. */}
        <div className="mt-2 flex flex-wrap items-center gap-x-2">
          <Button onClick={onForce} disabled={isPending} variant="ghost" size="sm" className="-ml-2">
            Sync anyway
          </Button>
          <span className="text-xs text-muted-foreground">
            only useful after editing the Flex Query in the IBKR portal
          </span>
        </div>
      </div>
    )
  }

  return (
    <div className="mt-4 p-4 bg-green-50 dark:bg-green-950 border border-green-200 dark:border-green-800 rounded-lg">
      <p className="text-sm text-green-800 dark:text-green-200">
        ✓ Sync successful! Securities: {data.securities_synced}, Tax Lots: {data.taxlots_synced}
      </p>
      {data.warnings && data.warnings.length > 0 && (
        <div className="mt-2">
          {data.warnings.map((warning, i) => (
            <p key={i} className="text-sm text-yellow-800 dark:text-yellow-200">
              ⚠ {warning}
            </p>
          ))}
        </div>
      )}
    </div>
  )
}
