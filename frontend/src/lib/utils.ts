import { type ClassValue, clsx } from "clsx"
import { twMerge } from "tailwind-merge"

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}

/**
 * The one glyph for an absent figure. `KpiCard` and the tables already drew an em dash;
 * a hyphen in the watchlist and an en dash in the heatmap read as three different
 * states, and a lone `-` beside signed figures looks like a stray minus.
 */
export const ABSENT = '—'

export function formatCurrency(value: number, currency: string = "EUR"): string {
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: currency,
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  }).format(value)
}

/**
 * A per-unit price that keeps its significant digits.
 *
 * `formatCurrency` is fixed at two decimals, which is right for amounts and wrong for a
 * crypto price: a token worth 0.0000123 would print as "$0.00" — a price of zero, which
 * reads as worthless rather than cheap. Below 1 this keeps up to six significant digits
 * instead; at 1 and above it is exactly `formatCurrency`.
 */
export function formatPrice(value: number, currency: string = "EUR"): string {
  const abs = Math.abs(value)
  if (abs === 0 || abs >= 1 || !Number.isFinite(value)) return formatCurrency(value, currency)
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency,
    minimumSignificantDigits: 2,
    maximumSignificantDigits: 6,
  }).format(value)
}

/**
 * A holding's quantity, with no more digits than it needs: "5", "0.01", "1,000,000",
 * "0.00012345". Below 1 it keeps six significant digits (a 0.00000123 BTC balance is not
 * zero); from 1 it keeps up to four decimals, and from 1,000 up to two.
 */
export function formatQuantity(value: number): string {
  const abs = Math.abs(value)
  if (abs > 0 && abs < 1) {
    return new Intl.NumberFormat("en-US", { maximumSignificantDigits: 6 }).format(value)
  }
  return new Intl.NumberFormat("en-US", {
    maximumFractionDigits: abs >= 1000 ? 2 : 4,
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
 * A timestamp as a short local date and time — "Aug 9, 6:00 AM", with the year only when
 * it is not this one ("Dec 30, 2025, 6:00 AM") — pinned to `en-US` like every other
 * formatter here. The month is spelled because "8/9/26" is 9 August to an American and
 * 8 September to most other readers of a product in CHF and EUR.
 *
 * Three sites (the sync header's last/next run, and `SyncStatusMessage`'s skip notice)
 * called `toLocaleString(undefined, …)` inline, which follows the viewer's runtime
 * locale; one formatter so the pin cannot be dropped at one of them. A full timestamp
 * names a real instant, so `new Date(iso)` is the right parse — `parseLocalDate` is for
 * the API's date-only strings.
 */
export function formatShortDateTime(iso: string | Date, now: Date = new Date()): string {
  const d = typeof iso === "string" ? new Date(iso) : iso
  return d.toLocaleString("en-US", {
    month: "short",
    day: "numeric",
    ...(d.getFullYear() !== now.getFullYear() && { year: "numeric" }),
    hour: "numeric",
    minute: "2-digit",
  })
}

/**
 * A Recharts tooltip formatter's answer for one series value.
 *
 * `null` — not `''`, and never a formatted zero — when the point carries no value,
 * because Recharts drops the tooltip row only when the formatter returns `null`. The
 * value chart writes `null` into `chartData` for a benchmark with no bar on that date
 * (and for cash fields an older backend does not send); its formatter guarded
 * `!== undefined`, so `null` reached `formatCurrency`, was coerced to `0`, and a line
 * that was not drawn read "€0.00" in the tooltip.
 */
export function tooltipValue<T>(
  value: T | null | undefined,
  format: (value: T) => string,
): string | null {
  return value == null ? null : format(value)
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
 * A money amount on a chart axis: "CHF 150k", "€1.2M", "-$2.5k", "CHF 0".
 *
 * Every chart wrote its own — seven `(value / 1000).toFixed(…)k` variants, three with the
 * symbol and four without — and each was wrong somewhere: the Forecast axis printed
 * "CHF 1000k" for a million and "CHF 0k" for zero, the value chart rounded a 12,500 tick
 * to "13k", and the dividend chart printed "1.0k". One decimal at most, trailing ".0"
 * dropped, the sign before the symbol as `formatCurrency` puts it. `symbol` is optional
 * for the axes whose card already names the currency.
 */
export function formatAxisMoney(value: number, symbol: string = ""): string {
  const abs = Math.abs(value)
  const sign = value < 0 && abs >= 0.5 ? "-" : ""
  return `${sign}${symbol}${compactBody(abs)}`
}

const AXIS_UNITS: readonly [number, string][] = [[1e9, "B"], [1e6, "M"], [1e3, "k"], [1, ""]]

/** `abs` in the largest unit it reaches *after* rounding, so 999,950 is "1M", not "1000k". */
function compactBody(abs: number): string {
  // Whole units below a thousand; one decimal from there up.
  const round = (i: number) => {
    const size = AXIS_UNITS[i][0]
    return size === 1 ? Math.round(abs) : Math.round((abs / size) * 10) / 10
  }
  let i = AXIS_UNITS.findIndex(([size]) => abs >= size)
  if (i === -1) i = AXIS_UNITS.length - 1
  let rounded = round(i)
  if (rounded >= 1000 && i > 0) rounded = round(--i)
  return `${Number.isInteger(rounded) ? rounded.toFixed(0) : rounded.toFixed(1)}${AXIS_UNITS[i][1]}`
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
  if (value === null) return ABSENT
  if (value >= 1e12) return `${(value / 1e12).toFixed(1)}T`
  if (value >= 1e9) return `${(value / 1e9).toFixed(1)}B`
  if (value >= 1e6) return `${(value / 1e6).toFixed(0)}M`
  return formatCount(value)
}

/**
 * A market cap as a table prints it: in the base currency when the server could convert
 * it (`market_cap_base`, `app/services/market_cap.py`), else the listing's own figure
 * with its currency code beside it — never a native number under the base currency's
 * symbol, and never a bare one (a KRW cap printed bare read like dollars). One rule for
 * the Fundamentals tab and the Watchlist.
 */
export function marketCapParts(
  cap: { market_cap: number | null; market_cap_base: number | null; data_currency: string | null },
  currencySymbol: string,
): { figure: string; code: string | null } {
  if (cap.market_cap_base != null) {
    return { figure: `${currencySymbol}${formatMarketCap(cap.market_cap_base)}`, code: null }
  }
  if (cap.market_cap == null) return { figure: ABSENT, code: null }
  return { figure: formatMarketCap(cap.market_cap), code: cap.data_currency }
}
