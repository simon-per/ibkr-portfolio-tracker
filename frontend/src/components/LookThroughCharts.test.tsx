// @vitest-environment jsdom
import { afterEach, describe, expect, it } from 'vitest'
import { cleanup, render } from '@testing-library/react'
import { TileContent } from './LookThroughCharts'

afterEach(cleanup)

function tile(width: number, height: number, listings: string[] = [], name = 'NVIDIA CORP') {
  return render(
    <svg>
      <TileContent x={0} y={0} width={width} height={height} depth={2} name={name} pct={1.23}
        listings={listings} root={{ paintKey: 'Technology' }} />
    </svg>,
  ).container
}

describe('a treemap tile names its company (review item VI8)', () => {
  it('shows the name on a tile wide enough for it', () => {
    const c = tile(120, 60)
    expect(c.textContent).toContain('NVIDIA')
    expect(c.querySelector('[data-testid="tile-short-label"]')).toBeNull()
  })

  it('shows the ticker on a narrow but tall tile, rather than a bare percentage', () => {
    const c = tile(56, 60, ['NVDA@NASDAQ'])
    expect(c.querySelector('[data-testid="tile-short-label"]')?.textContent).toBe('NVDA')
    expect(c.textContent).toContain('1.2%')
  })

  it('cuts the name to fit when the company has no listing of its own', () => {
    const label = tile(56, 60).querySelector('[data-testid="tile-short-label"]')?.textContent
    expect(label?.length).toBeGreaterThan(1)
    expect(label?.endsWith('…')).toBe(true)
  })

  it('stays a bare percentage where two lines do not fit', () => {
    expect(tile(56, 30, ['NVDA@NASDAQ']).querySelector('[data-testid="tile-short-label"]')).toBeNull()
  })
})
