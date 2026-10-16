import { cn } from '@/lib/utils'

/**
 * A round monogram standing in for a company logo: the ticker's first letters on a tint
 * picked from the ticker itself, so a holding keeps its colour on every screen.
 *
 * Not real logos: the CSP allows images from our own origin only, and a third-party logo
 * service would also learn every holding in the book.
 */
const HUES = [217, 262, 330, 12, 32, 152, 188, 240, 290, 350, 200, 170]

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

export function TickerAvatar({ symbol, className }: { symbol: string; className?: string }) {
  const hue = hueFor(symbol)
  return (
    <span
      aria-hidden="true"
      className={cn(
        'inline-flex h-9 w-9 shrink-0 items-center justify-center rounded-full text-[11px] font-semibold tracking-tight',
        className,
      )}
      style={{
        backgroundColor: `hsl(${hue} 85% 55% / 0.14)`,
        color: `hsl(${hue} 70% 42%)`,
      }}
    >
      {monogram(symbol)}
    </span>
  )
}
