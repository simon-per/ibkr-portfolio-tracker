/**
 * The one tooltip style every Recharts chart uses.
 *
 * Recharts' default tooltip is white with black text whatever the theme, which made it
 * unreadable in dark mode and washed out in light. `PortfolioValueChart` carried the fix as an
 * inline `contentStyle`; the Analytics tab's five charts would have been five copies of it.
 * The tokens are the app's own, so the tooltip follows the theme the way a card does.
 */
import type { CSSProperties } from 'react'

export const CHART_TOOLTIP_STYLE: CSSProperties = {
  backgroundColor: 'hsl(var(--card))',
  color: 'hsl(var(--card-foreground))',
  border: '1px solid hsl(var(--border))',
  borderRadius: '8px',
  fontSize: '12px',
  // Recharts clamps a tooltip to the viewBox, so it never causes overflow — but
  // unbounded it fills the whole plot area at 390px.
  maxWidth: '70vw',
}

/** The row labels inside the tooltip, which Recharts otherwise colours per series. */
export const CHART_TOOLTIP_ITEM_STYLE: CSSProperties = {
  color: 'hsl(var(--card-foreground))',
}

/** The tooltip's header (the x value). */
export const CHART_TOOLTIP_LABEL_STYLE: CSSProperties = {
  color: 'hsl(var(--muted-foreground))',
  marginBottom: 4,
}

/**
 * The one grid every chart draws: solid hairlines in the border colour, along the value
 * axis only. Spread it into `<CartesianGrid>`; a horizontal bar chart flips the two flags.
 *
 * Six charts had drifted into three looks — dashed `stroke-muted`, solid border, and the
 * crypto chart's dashed lines in both directions — which read as six different products.
 * `chartAxisFamily.test.ts` refuses a `<CartesianGrid>` that does not spread this.
 */
export const CHART_GRID_PROPS = {
  stroke: 'hsl(var(--border))',
  vertical: false,
} as const
