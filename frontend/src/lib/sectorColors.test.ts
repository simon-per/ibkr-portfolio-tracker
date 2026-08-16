import { describe, it, expect } from 'vitest'
import {
  CANONICAL_SECTORS,
  HUED_SECTORS,
  OTHER_SECTORS_LABEL,
  UNKNOWN_FILL,
  allocationSectorPaint,
  neutralPaint,
  sectorGroup,
  sectorPaint,
} from './sectorColors'

describe('sectorGroup', () => {
  it('gives each hued sector its own group', () => {
    for (const sector of HUED_SECTORS) {
      expect(sectorGroup(sector)).toBe(sector)
    }
  })

  it('folds everything else, including Unknown, into one group', () => {
    for (const sector of ['Healthcare', 'Utilities', 'Energy', 'Unknown', '', null, undefined]) {
      expect(sectorGroup(sector)).toBe(OTHER_SECTORS_LABEL)
    }
  })
})

describe('sectorPaint', () => {
  it('gives every group a distinct fill', () => {
    // The bug this pins was found by sampling a screenshot, not by a test: `sectorPaint` looked
    // only at the sector record, so `unattributed` and `other_companies` both fell through to
    // the `Other sectors` grey. Three meanings, one colour, three identical legend swatches —
    // and nothing failed, because falling back is correct for a genuinely unknown group.
    const keys = [...HUED_SECTORS, OTHER_SECTORS_LABEL, 'other_companies', 'unattributed']
    const fills = keys.map((k) => sectorPaint(k).fill)
    expect(new Set(fills).size).toBe(keys.length)
  })

  it('pairs every fill with its own label ink', () => {
    // Light `--viz-sector-2` measures 2.17:1 against white, so a single hardcoded ink makes one
    // group's labels unreadable in exactly one theme — which nobody testing the other will see.
    for (const key of [...HUED_SECTORS, OTHER_SECTORS_LABEL, 'other_companies', 'unattributed']) {
      const paint = sectorPaint(key)
      expect(paint.ink).toBeTruthy()
      expect(paint.ink).not.toBe(paint.fill)
    }
  })

  it('falls back rather than returning undefined for a group it has never seen', () => {
    expect(sectorPaint('Some Future Sector').fill).toBeTruthy()
  })
})

describe('allocationSectorPaint', () => {
  it('gives the four hued sectors the shared slots, so a sector is one colour across both tabs', () => {
    HUED_SECTORS.forEach((sector, i) => {
      expect(allocationSectorPaint(sector).fill).toBe(`var(--viz-sector-${i + 1})`)
    })
  })

  it('gives every canonical sector a distinct colour', () => {
    const fills = CANONICAL_SECTORS.map((s) => allocationSectorPaint(s).fill)
    expect(new Set(fills).size).toBe(CANONICAL_SECTORS.length)
  })

  it('colours Unknown by identity, never by row position', () => {
    // It used to take `FALLBACK_COLORS[index % 15]`, so it changed whenever the chart reordered.
    expect(allocationSectorPaint('Unknown').fill).toBe(UNKNOWN_FILL)
    expect(neutralPaint('Unknown').fill).toBe(UNKNOWN_FILL)
  })

  it('is stable for a name no palette covers', () => {
    const first = allocationSectorPaint('Diversified Industrials').fill
    expect(allocationSectorPaint('Diversified Industrials').fill).toBe(first)
    expect(first).toBeTruthy()
  })

  it('does not depend on the order it is asked', () => {
    const forward = CANONICAL_SECTORS.map((s) => allocationSectorPaint(s).fill)
    const backward = [...CANONICAL_SECTORS].reverse().map((s) => allocationSectorPaint(s).fill)
    expect(backward).toEqual([...forward].reverse())
  })
})
