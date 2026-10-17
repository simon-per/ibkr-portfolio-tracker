import { useContext, useState } from 'react'
import { QueryClientContext } from '@tanstack/react-query'
import { api } from '@/lib/api'
import { cn } from '@/lib/utils'
import { useLogos } from '@/lib/useLogos'

/**
 * A holding's round avatar: its logo mark when one is stored, else a monogram — the
 * ticker's first letters on a tint picked from the ticker itself, so a holding keeps its
 * colour on every screen.
 *
 * Real logos since 2026-10-17, at the owner's request (docs/logos.md): fetched server-side
 * from logo.dev, which therefore learns the held ISINs — a trade-off the owner accepted —
 * and served from our own origin, so the CSP stays `img-src 'self'`. The logo and the
 * monogram share one box, so a row never moves when one replaces the other.
 */
const HUES = [217, 262, 330, 12, 32, 152, 188, 240, 290, 350, 200, 170]

const BOX = 'inline-flex h-9 w-9 shrink-0 items-center justify-center rounded-full'

function hueFor(symbol: string): number {
  let h = 0
  for (const ch of symbol) h = (h * 31 + ch.charCodeAt(0)) >>> 0
  return HUES[h % HUES.length]
}

function monogram(symbol: string): string {
  // "CH0117044948" and "000660.KS" have no letters worth showing up front.
  const letters = symbol.replace(/\..*$/, '').replace(/[^A-Za-z]/g, '')
  return (letters || symbol).slice(0, 2).toUpperCase()
}

interface AvatarProps {
  symbol: string
  /** Without it (activity rows carry none) the avatar is always the monogram. */
  securityId?: number
  className?: string
}

function Monogram({ symbol, className }: AvatarProps) {
  const hue = hueFor(symbol)
  return (
    <span
      aria-hidden="true"
      className={cn(BOX, 'text-[11px] font-semibold tracking-tight', className)}
      style={{
        backgroundColor: `hsl(${hue} 85% 55% / 0.14)`,
        color: `hsl(${hue} 70% 42%)`,
      }}
    >
      {monogram(symbol)}
    </span>
  )
}

function LogoAvatar({ symbol, securityId, className }: AvatarProps & { securityId: number }) {
  const version = useLogos()[String(securityId)]
  // The version that failed to load, so a replaced logo gets its own try.
  const [failed, setFailed] = useState<string | null>(null)
  if (!version || failed === version) return <Monogram symbol={symbol} className={className} />
  return (
    <span aria-hidden="true" className={cn(BOX, 'overflow-hidden bg-white ring-1 ring-border', className)}>
      <img
        src={api.logoUrl(securityId, version)}
        alt=""
        loading="lazy"
        decoding="async"
        className="h-full w-full object-contain"
        onError={() => setFailed(version)}
      />
    </span>
  )
}

export function TickerAvatar({ symbol, securityId, className }: AvatarProps) {
  // A table rendered outside a QueryClientProvider (its own unit tests) keeps the
  // monogram: decoration must never be what breaks a table.
  const hasQueryClient = useContext(QueryClientContext) !== undefined
  if (securityId == null || !hasQueryClient) return <Monogram symbol={symbol} className={className} />
  return <LogoAvatar symbol={symbol} securityId={securityId} className={className} />
}
