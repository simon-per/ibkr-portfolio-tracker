import type { ReactNode } from 'react'
import { CircleAlert, CircleCheck, CirclePause, TriangleAlert, type LucideIcon } from 'lucide-react'
import { TONE_MARK } from '@/lib/tone'
import { cn } from '@/lib/utils'

export type BannerTone = 'success' | 'error' | 'idle'

const LOOK: Record<BannerTone, { icon: LucideIcon; box: string; mark: string }> = {
  success: { icon: CircleCheck, box: 'border-gain/30 bg-gain-soft', mark: TONE_MARK.positive },
  error: { icon: CircleAlert, box: 'border-loss/30 bg-loss-soft', mark: TONE_MARK.negative },
  // A skipped sync is neither: neutral, never green or red (see SyncStatusMessage).
  idle: { icon: CirclePause, box: 'border-border bg-muted', mark: TONE_MARK.muted },
}

/**
 * The outcome of a sync press, under the header: one rounded box, its state's glyph and
 * wash, the message in plain ink. The stock sync and the crypto sync both answer here, so
 * a failure looks the same whichever book you are in.
 */
export function StatusBanner({
  tone, children, className,
}: { tone: BannerTone; children: ReactNode; className?: string }) {
  const { icon: Icon, box, mark } = LOOK[tone]
  return (
    <div className={cn('mt-4 flex gap-3 rounded-xl border p-3 sm:p-4', box, className)}>
      <Icon aria-hidden="true" className={cn('mt-0.5 h-4 w-4 shrink-0', mark)} />
      <div className="min-w-0 flex-1 text-sm">{children}</div>
    </div>
  )
}

/** A warning line inside a banner: a successful run that still has something to say. */
export function BannerWarning({ children }: { children: ReactNode }) {
  return (
    <p className="mt-1 flex gap-1.5 text-sm text-yellow-800 dark:text-yellow-200">
      <TriangleAlert aria-hidden="true" className={cn('mt-0.5 h-3.5 w-3.5 shrink-0', TONE_MARK.warning)} />
      <span className="min-w-0">{children}</span>
    </p>
  )
}
