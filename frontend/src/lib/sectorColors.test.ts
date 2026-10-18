import { describe, it, expect } from 'vitest'
import { CHART_OTHER } from './chartPalette'
import { NEUTRAL_PAINT } from './chartPaint'
import {
  ASSET_TYPE_ORDER,
  CANONICAL_SECTORS,
  CASH_BUCKET,
  OTHER_SECTORS_LABEL,
  REGION_ORDER,
  allocationSectorPaint,
  assetTypePaint,
  neutralPaint,
  regionPaint,
  sectorGroup,
  sectorPaint,
} from './sectorColors'

const HUED = CANONICAL_SECTORS.filter((s) => allocationSectorPaint(s).fill !== CHART_OTHER)

describe('the assignment is by identity', () => {
  it('gives each canonical sector its own palette slot, in canonical order, until the slots run out', () => {
    CANONICAL_SECTORS.forEach((sector, i) => {
      expect(allocationSectorPaint(sector).fill).toBe(i < 10 ? `var(--chart-${i + 1})` : CHART_OTHER)
    })
    expect(HUED).toHaveLength(10)
  })

  it('gives regions and asset types their slots by their own fixed order', () => {
    REGION_ORDER.forEach((region, i) => expect(regionPaint(region).fill).toBe(`var(--chart-${i + 1})`))
    ASSET_TYPE_ORDER.forEach((type, i) => expect(assetTypePaint(type).fill).toBe(`var(--chart-${i + 1})`))
  })

  it('does not depend on the order it is asked, or on what else is on screen', () => {
    const forward = CANONICAL_SECTORS.map((s) => allocationSectorPaint(s).fill)
    const backward = [...CANONICAL_SECTORS].reverse().map((s) => allocationSectorPaint(s).fill)
    expect(backward).toEqual([...forward].reverse())
    expect(allocationSectorPaint(' Technology ').fill).toBe(allocationSectorPaint('Technology').fill)
  })

  it('keeps Cash, the unclassified buckets and the tail three different neutrals', () => {
    // Cash in every breakdown, Unknown on the sector and region charts, Other on regions.
    for (const paint of [allocationSectorPaint, regionPaint, assetTypePaint, neutralPaint]) {
      expect(paint(CASH_BUCKET)).toBe(NEUTRAL_PAINT.strong)
      expect(paint('Unknown')).toBe(NEUTRAL_PAINT.faint)
      expect(paint('Other')).toBe(NEUTRAL_PAINT.faint)
    }
    expect(regionPaint('Antarctica')).toBe(NEUTRAL_PAINT.tail)
    const fills = Object.values(NEUTRAL_PAINT).map((p) => p.fill)
    expect(new Set(fills).size).toBe(3)
  })
})

describe('sectorGroup', () => {
  it('gives each hued sector its own group', () => {
    for (const sector of HUED) expect(sectorGroup(sector)).toBe(sector)
  })

  it('folds a sector with no hue, Unknown and anything unrecognised into one group', () => {
    for (const sector of ['Utilities', 'Unknown', 'Crypto', '', null, undefined]) {
      expect(sectorGroup(sector)).toBe(OTHER_SECTORS_LABEL)
    }
  })

  it('groups a sector exactly when the Allocation tab colours it, in the same colour', () => {
    for (const sector of CANONICAL_SECTORS) {
      const group = sectorGroup(sector)
      if (group === sector) expect(sectorPaint(group).fill).toBe(allocationSectorPaint(sector).fill)
      else expect(allocationSectorPaint(sector).fill).toBe(CHART_OTHER)
    }
  })
})

describe('sectorPaint', () => {
  it('gives every group a distinct fill', () => {
    // Found by sampling a screenshot, not by a test: `sectorPaint` once looked only at the
    // sectors, so `unattributed` and `other_companies` fell through to the fold's grey.
    // A hatch counts as part of the fill: it is what keeps Not attributed apart from Cash.
    const keys = [...HUED, OTHER_SECTORS_LABEL, 'other_companies', 'unattributed', 'cash']
    const fills = keys.map((k) => `${sectorPaint(k).fill}|${sectorPaint(k).hatch ?? ''}`)
    expect(new Set(fills).size).toBe(keys.length)
  })

  it("paints the treemap's Cash exactly as the Allocation tab paints its Cash bucket", () => {
    // Colour follows the entity: it is the same money on both tabs.
    expect(sectorPaint('cash')).toEqual(assetTypePaint(CASH_BUCKET))
    expect(sectorPaint('cash')).toEqual(allocationSectorPaint(CASH_BUCKET))
  })

  it('hatches Not attributed with two measured neutrals, so its ink reads on either stripe', () => {
    const paint = sectorPaint('unattributed')
    expect([paint.fill, paint.hatch]).toEqual([NEUTRAL_PAINT.strong.fill, NEUTRAL_PAINT.faint.fill])
    expect(paint.ink).toBe(NEUTRAL_PAINT.strong.ink)
    expect(paint.ink).toBe(NEUTRAL_PAINT.faint.ink)
  })

  it('pairs every fill with a label ink', () => {
    for (const key of [...HUED, OTHER_SECTORS_LABEL, 'other_companies', 'unattributed', 'cash']) {
      const paint = sectorPaint(key)
      expect(paint.ink).toBeTruthy()
      expect(paint.ink).not.toBe(paint.fill)
    }
  })

  it('falls back to the fold rather than a prototype key or undefined', () => {
    expect(sectorPaint('Some Future Sector')).toBe(NEUTRAL_PAINT.tail)
    expect(sectorPaint('toString')).toBe(NEUTRAL_PAINT.tail)
  })
})
