import type { AnalystRatingCounts } from '@/lib/api'
import { RATING_SEGMENTS, ratingBadgeClass, ratingCountsText, type RatingCounts } from '@/lib/analystRating'
import { cn } from '@/lib/utils'

/**
 * The five recommendation counts as one thin bar, Strong Buy on the left: each segment's
 * width is its share of the analysts, a hairline of surface between segments. Nothing for
 * a bar with no counts. The counts themselves are its accessible name.
 */
export function AnalystRatingBar({ counts, className }: { counts: RatingCounts; className?: string }) {
  const segments = RATING_SEGMENTS.filter((s) => counts[s.key] > 0)
  if (segments.length === 0) return null
  return (
    <span
      role="img"
      aria-label={ratingCountsText(counts)}
      className={cn('flex h-1 gap-px overflow-hidden rounded-full', className)}
    >
      {segments.map((s) => (
        <span key={s.key} className={s.className} style={{ flex: `${counts[s.key]} 1 0%` }} />
      ))}
    </span>
  )
}

/**
 * A table's analyst cell: the consensus chip with the bar of its counts beneath, the
 * counts on hover. The positions table and the watchlist draw the same one.
 */
export function AnalystRatingBadge({ rating }: { rating: AnalystRatingCounts }) {
  return (
    <span className="inline-flex flex-col items-stretch gap-1" title={ratingCountsText(rating)}>
      <span
        className={cn(
          'inline-block whitespace-nowrap rounded-full px-2 py-0.5 text-center text-xs font-medium',
          ratingBadgeClass(rating.consensus),
        )}
      >
        {rating.consensus}
      </span>
      <AnalystRatingBar counts={rating} />
    </span>
  )
}
