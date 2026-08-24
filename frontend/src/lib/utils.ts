import { type ClassValue, clsx } from "clsx"
import { twMerge } from "tailwind-merge"

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}

export function formatCurrency(value: number, currency: string = "EUR"): string {
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: currency,
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  }).format(value)
}

/**
 * A word-length code is separated from its amount by a **non-breaking** space
 * (U+00A0), written as an escape here so it cannot be mistaken for a plain space
 * or silently normalised by an editor. That is what `Intl.NumberFormat` itself
 * emits for CHF, and it stops "CHF" being split from its figure at a line break.
 */
const NBSP = "\u00a0"

const CURRENCY_SYMBOLS: Record<string, string> = {
  EUR: "€",
  USD: "$",
  CHF: `CHF${NBSP}`,
}

// Short symbol/prefix for a currency, for use in inline `${symbol}${value}` labels.
export function currencySymbol(currency: string = "EUR"): string {
  return CURRENCY_SYMBOLS[currency] ?? `${currency}${NBSP}`
}

export function formatPercent(value: number): string {
  return new Intl.NumberFormat("en-US", {
    style: "percent",
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  }).format(value / 100)
}

const ISO_DATE_ONLY = /^(\d{4})-(\d{2})-(\d{2})$/

/**
 * Parse a `YYYY-MM-DD` string as a **calendar date in the local zone**.
 *
 * `new Date("2026-03-14")` is specified to parse a date-only string as UTC
 * midnight. `Intl.DateTimeFormat` then renders that instant in the *local* zone,
 * so anywhere west of Greenwich it prints 13 March — the whole API speaks
 * date-only strings, so that is every trade, dividend and chart point shifted a
 * day for a viewer in New York.
 *
 * This is the mirror of the bug `dateRanges.ts` was written to fix: that one
 * serialised a local date *to* UTC and started YTD a day early in positive
 * offsets, this one parses *from* UTC and labels a day early in negative ones.
 * Same feature, opposite direction.
 *
 * Anything that is not date-only keeps stdlib parsing: a full timestamp names a
 * real instant, where converting to local time is correct rather than a bug.
 */
export function parseLocalDate(value: string): Date {
  const m = ISO_DATE_ONLY.exec(value)
  if (!m) return new Date(value)
  return new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]))
}

export function formatDate(date: string | Date): string {
  const d = typeof date === "string" ? parseLocalDate(date) : date
  return new Intl.DateTimeFormat("en-US", {
    year: "numeric",
    month: "short",
    day: "numeric",
  }).format(d)
}

/**
 * A plain integer with thousands separators, pinned to `en-US`.
 *
 * The pin is the whole point, and `LookThroughTab` writes down why: a bare
 * `toLocaleString()` renders 8,007 as **"8.007"** under a German runtime, which does not
 * look malformed — it looks like eight point oh oh seven. Every other formatter in this
 * file is already pinned; the call sites that were not are the ones that grew their own
 * formatting inline.
 */
export function formatCount(value: number): string {
  return new Intl.NumberFormat("en-US").format(value)
}

/**
 * A market capitalisation as T / B / M, falling back to a separated integer.
 *
 * Extracted because it existed **byte-identically** in `FundamentalsTab.tsx` and
 * `watchlistColumns.tsx` — one defect that had to be fixed twice, which is the shape
 * CLAUDE.md opens with. Its sub-million branch was the worst instance of the locale bug
 * in the app: a market cap of 850,000 rendered as "850.000" under a German runtime,
 * reading as eight hundred fifty *thousandths* of a currency unit.
 */
export function formatMarketCap(value: number | null): string {
  if (value === null) return '-'
  if (value >= 1e12) return `${(value / 1e12).toFixed(1)}T`
  if (value >= 1e9) return `${(value / 1e9).toFixed(1)}B`
  if (value >= 1e6) return `${(value / 1e6).toFixed(0)}M`
  return formatCount(value)
}
