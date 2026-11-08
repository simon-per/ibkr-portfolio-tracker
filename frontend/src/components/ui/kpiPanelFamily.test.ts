import { describe, expect, it } from 'vitest'

/**
 * A seam-divided `KpiPanel` shows its background through an empty cell — a grey block — and
 * the phone keeps two columns, so a panel with an odd, fixed number of tiles must let the
 * last one span the row (`fillLastRow`). The Tax tab's two three-tile panels did not
 * (review item VI6, 2026-11-08). Panels whose tiles are mapped or conditional cannot be
 * counted from the source and are left to their authors.
 */
const sources = import.meta.glob('../*.tsx', { query: '?raw', import: 'default', eager: true })

function panels(src: string): string[] {
  const out: string[] = []
  const open = /<KpiPanel\b/g
  let match: RegExpExecArray | null
  while ((match = open.exec(src)) !== null) {
    const end = src.indexOf('</KpiPanel>', match.index)
    if (end > 0) out.push(src.slice(match.index, end))
  }
  return out
}

describe('KpiPanel family', () => {
  it('gives every panel with an odd, fixed number of tiles fillLastRow', () => {
    const offenders: string[] = []
    for (const [path, raw] of Object.entries(sources)) {
      if (path.endsWith('.test.tsx')) continue
      for (const block of panels(raw as string)) {
        const opening = block.slice(0, block.indexOf('>') + 1)
        const dynamic = /\.map\(|&&\s*[(<]|\?\s*\(?\s*<KpiCard/.test(block)
        const tiles = (block.match(/<KpiCard\b/g) ?? []).length
        if (!dynamic && tiles % 2 === 1 && !/fillLastRow/.test(opening)) {
          offenders.push(`${path}: ${tiles} tiles`)
        }
      }
    }
    expect(offenders).toEqual([])
  })
})
