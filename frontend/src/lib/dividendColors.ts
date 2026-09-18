import { MAX_SERIES, OTHER } from './dividendChart'

/**
 * Categorical palette from the validated reference set, stepped per theme and
 * checked against this app's actual card surfaces (#ffffff / #020817): adjacent
 * pairs pass CVD dE >= 8.4 and normal-vision dE >= 19.3; all dark steps >= 3:1.
 * The ORDER is the colorblind-safety mechanism — stack order must follow it.
 * Symbols beyond the slots fold into a muted "Other", never a ninth hue.
 *
 * Lives here rather than in the component because the slot COUNT is
 * `MAX_SERIES`, which lives next door: those were two statements of one number in
 * two files, and raising the cap alone would have handed the extra symbols
 * `palette[8] ?? OTHER_COLOR` — grey, indistinguishable from the Other bucket,
 * with nothing failing. `dividendColors.test.ts` pins them together.
 */
export const SERIES_LIGHT = ['#2a78d6', '#eb6834', '#1baf7a', '#eda100', '#e87ba4', '#008300', '#4a3aa7', '#e34948']
export const SERIES_DARK = ['#3987e5', '#d95926', '#199e70', '#c98500', '#d55181', '#008300', '#9085e9', '#e66767']
export const OTHER_COLOR = '#898781'

/**
 * A symbol's colour, by its position in the stack order.
 *
 * `stackSymbols` is ranked once for the whole response and shared by the monthly
 * and rolling charts, so a symbol keeps its colour when the view is switched.
 * Anything unrecognised — including a projected payment for a symbol outside the
 * top slots — falls to the Other grey rather than throwing.
 */
export function dividendColor(
  symbol: string,
  stackSymbols: string[],
  palette: string[],
): string {
  if (symbol === OTHER) return OTHER_COLOR
  return palette[stackSymbols.indexOf(symbol)] ?? OTHER_COLOR
}

/** The palette for a theme. `system` resolves to light, as elsewhere in the app. */
export function dividendPalette(theme: string): string[] {
  return theme === 'dark' ? SERIES_DARK : SERIES_LIGHT
}

/** Re-exported so a caller needs one import to size a legend. */
export { MAX_SERIES }
