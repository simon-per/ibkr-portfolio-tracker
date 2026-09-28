import { useQuery } from '@tanstack/react-query'
import { api } from '@/lib/api'

/**
 * Build identity. /health used to return only {"status":"healthy"}, so confirming a
 * deploy had landed — or that the scheduler was armed at all — meant ssh'ing to the box.
 *
 * Extracted from `Dashboard` with the header (see `AppHeader`) so the crypto view carries
 * the same footer. Refetched on mount only — it changes on deploy.
 */
export function AppFooter() {
  const { data: health } = useQuery({
    queryKey: ['health'],
    queryFn: () => api.healthCheck(),
    staleTime: Infinity,
  })

  if (!health) return null

  return (
    <footer className="border-t">
      {/* A flex row rather than inline spans with `ml-*`: those margins survive a
          wrap and indent the start of the next line. */}
      <div className="flex w-full flex-wrap items-center gap-x-2 gap-y-1 px-4 py-3 text-xs text-muted-foreground">
        <span>Portfolio Analyzer v{health.version}</span>
        {health.commit && health.commit !== 'unknown' && (
          <span className="font-mono">{health.commit.slice(0, 7)}</span>
        )}
        {/* Both call out a *disabled* safeguard, never a working one: a scheduler
            that has quietly stopped looks exactly like a healthy site. */}
        {!health.scheduler_enabled && (
          <span className="text-amber-700 dark:text-amber-400">
            · scheduler disabled — no automatic syncs
          </span>
        )}
        {!health.write_auth_enabled && (
          <span className="text-amber-700 dark:text-amber-400">
            · write API unauthenticated
          </span>
        )}
      </div>
    </footer>
  )
}
