/**
 * Colour measurement for the palette tests: OKLab distance, Machado-2009 colour-vision
 * simulation, OKLCH lightness/chroma and WCAG contrast — the model the dataviz skill's
 * validator uses, so a figure quoted in `index.css` can be re-run here and agree.
 *
 * Test support, not app code: nothing in the bundle imports it. It lives in `lib/` rather
 * than inline in a test because two suites measure with it (`chartPalette.test.ts`,
 * `benchmarkColors.test.ts`), and two inline copies of a colour model are the drift this
 * repo keeps writing down. `dividendColors.test.ts` measures with a different model
 * (CIEDE2000 over Viénot-1999) on purpose — its bars were calibrated to that one.
 */

export type Rgb = [number, number, number]
export type Vision = 'normal' | 'protan' | 'deutan'

const toLinear = (c: number) => (c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4)

/** `#rrggbb` → linear-light sRGB, each channel 0..1. */
export function hexToLinear(hex: string): Rgb {
  return [1, 3, 5].map((i) => toLinear(parseInt(hex.slice(i, i + 2), 16) / 255)) as Rgb
}

/** `"220 14% 96%"` (the Tailwind token form) → `#rrggbb`. */
export function hslTripleToHex(triple: string): string {
  const [h, s, l] = triple.trim().split(/\s+/).map((part) => Number(part.replace('%', '')))
  const sat = s / 100
  const light = l / 100
  const k = (n: number) => (n + h / 30) % 12
  const a = sat * Math.min(light, 1 - light)
  const f = (n: number) => light - a * Math.max(-1, Math.min(k(n) - 3, Math.min(9 - k(n), 1)))
  return '#' + [f(0), f(8), f(4)]
    .map((c) => Math.round(c * 255).toString(16).padStart(2, '0')).join('')
}

// Machado, Oliveira & Fernandes (2009), severity 1.0, applied in linear RGB.
const MACHADO: Record<Exclude<Vision, 'normal'>, number[][]> = {
  protan: [[0.152286, 1.052583, -0.204868], [0.114503, 0.786281, 0.099216], [-0.003882, -0.048116, 1.051998]],
  deutan: [[0.367322, 0.860646, -0.227968], [0.280085, 0.672501, 0.047413], [-0.01182, 0.04294, 0.968881]],
}

function simulate(rgb: Rgb, vision: Vision): Rgb {
  if (vision === 'normal') return rgb
  const m = MACHADO[vision]
  return m.map((row) => Math.max(0, Math.min(1, row[0] * rgb[0] + row[1] * rgb[1] + row[2] * rgb[2]))) as Rgb
}

function oklab([r, g, b]: Rgb): Rgb {
  const l = Math.cbrt(0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b)
  const m = Math.cbrt(0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b)
  const s = Math.cbrt(0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b)
  return [
    0.2104542553 * l + 0.793617785 * m - 0.0040720468 * s,
    1.9779984951 * l - 2.428592205 * m + 0.4505937099 * s,
    0.0259040371 * l + 0.7827717662 * m - 0.808675766 * s,
  ]
}

/** OKLab Euclidean distance ×100, as seen with `vision`. */
export function deltaE(a: string, b: string, vision: Vision = 'normal'): number {
  const x = oklab(simulate(hexToLinear(a), vision))
  const y = oklab(simulate(hexToLinear(b), vision))
  return 100 * Math.hypot(x[0] - y[0], x[1] - y[1], x[2] - y[2])
}

/** The worst a pair looks to a protanope or a deuteranope. */
export function cvdDeltaE(a: string, b: string): number {
  return Math.min(deltaE(a, b, 'protan'), deltaE(a, b, 'deutan'))
}

/** OKLCH lightness (0..1) and chroma. */
export function oklch(hex: string): { l: number; c: number } {
  const [l, a, b] = oklab(hexToLinear(hex))
  return { l, c: Math.hypot(a, b) }
}

/** WCAG 2 contrast ratio. */
export function contrast(a: string, b: string): number {
  const lum = (hex: string) => {
    const [r, g, bl] = hexToLinear(hex)
    return 0.2126 * r + 0.7152 * g + 0.0722 * bl
  }
  const [hi, lo] = [lum(a), lum(b)].sort((x, y) => y - x)
  return (hi + 0.05) / (lo + 0.05)
}

/**
 * Every value `css` declares for custom property `--name`, in source order. `index.css`
 * declares each themed token once under `:root` and once under `.dark`, so a well-formed
 * token yields `[light, dark]`.
 */
export function cssDeclarations(css: string, name: string): string[] {
  // Token names are `[a-z0-9-]`; anything else would be a caller's typo, not a pattern.
  if (!/^[a-z0-9-]+$/.test(name)) throw new Error(`not a token name: ${name}`)
  return [...css.matchAll(new RegExp(`--${name}:\\s*([^;]+);`, 'g'))].map((m) => m[1].trim())
}
