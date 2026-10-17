/**
 * What a figure's tone looks like, in one place: the value's ink and the tinted icon well.
 *
 * `KpiCard`, `SectionHeader`, `CollapsibleCardHeader` and `IconChip` all read these, so a
 * "negative" figure and a "negative" section badge cannot drift to two reds. The value ink
 * keeps the `green-600 dark:green-400` classes ~70 call sites and several tests use; the
 * wells use the semantic tokens (`bg-gain-soft text-gain`), which index.css pins to the
 * same steps.
 */

export type Tone = 'neutral' | 'positive' | 'negative' | 'warning' | 'muted'

/** Ink for a figure. Neutral is plain foreground: no class at all. */
export const TONE_TEXT: Record<Tone, string> = {
  neutral: '',
  positive: 'text-green-600 dark:text-green-400',
  negative: 'text-red-600 dark:text-red-400',
  warning: 'text-yellow-600 dark:text-yellow-400',
  muted: 'text-muted-foreground',
}

/**
 * The icon well. Neutral is the brand tint rather than grey: an icon on a neutral metric
 * is wayfinding, and a grey well reads as disabled.
 */
export const TONE_CHIP: Record<Tone, string> = {
  neutral: 'bg-info-soft text-info',
  positive: 'bg-gain-soft text-gain',
  negative: 'bg-loss-soft text-loss',
  warning: 'bg-warn-soft text-warn',
  muted: 'bg-muted text-muted-foreground',
}

/**
 * `currentColor` for a drawn mark (a sparkline, a dot): one token class per tone, so an SVG
 * that strokes with `currentColor` follows the theme without a `dark:` pair.
 */
export const TONE_MARK: Record<Tone, string> = {
  neutral: 'text-info',
  positive: 'text-gain',
  negative: 'text-loss',
  warning: 'text-warn',
  muted: 'text-muted-foreground',
}
