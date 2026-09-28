// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { SegmentedControl } from './SegmentedControl'

afterEach(cleanup)

describe('SegmentedControl', () => {
  it('is a named group whose buttons report their pressed state', () => {
    const onChange = vi.fn()
    render(
      <SegmentedControl
        label="Chart view"
        value="monthly"
        onChange={onChange}
        options={[
          { value: 'monthly', label: 'Monthly' },
          { value: 'ttm', label: 'TTM' },
        ]}
      />,
    )
    expect(screen.getByRole('group', { name: 'Chart view' })).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Monthly' }).getAttribute('aria-pressed')).toBe('true')
    expect(screen.getByRole('button', { name: 'TTM' }).getAttribute('aria-pressed')).toBe('false')
    fireEvent.click(screen.getByRole('button', { name: 'TTM' }))
    expect(onChange).toHaveBeenCalledWith('ttm')
    // Never tab roles: e2e holds the page to exactly one tablist.
    expect(screen.queryAllByRole('tab')).toHaveLength(0)
  })

  it('works as a set of independent toggles', () => {
    const onToggle = vi.fn()
    render(
      <SegmentedControl
        label="Event types"
        variant="buttons"
        isSelected={v => v === 'trade'}
        onToggle={onToggle}
        options={[
          { value: 'trade', label: 'Trades' },
          { value: 'dividend', label: 'Dividends' },
        ]}
      />,
    )
    expect(screen.getByRole('button', { name: 'Trades' }).getAttribute('aria-pressed')).toBe('true')
    fireEvent.click(screen.getByRole('button', { name: 'Dividends' }))
    expect(onToggle).toHaveBeenCalledWith('dividend')
  })

  it('takes numbers and an accessible name for an icon-only label', () => {
    const onChange = vi.fn()
    render(
      <SegmentedControl<number>
        label="How many"
        variant="buttons"
        value={50}
        onChange={onChange}
        options={[
          { value: 25, label: '25' },
          { value: 50, label: <span aria-hidden="true">★</span>, ariaLabel: 'Fifty' },
        ]}
      />,
    )
    expect(screen.getByRole('button', { name: 'Fifty' }).getAttribute('aria-pressed')).toBe('true')
    fireEvent.click(screen.getByRole('button', { name: '25' }))
    expect(onChange).toHaveBeenCalledWith(25)
  })

  it('never submits a form it sits in', () => {
    const onSubmit = vi.fn(e => e.preventDefault())
    render(
      <form onSubmit={onSubmit}>
        <SegmentedControl label="x" value="a" onChange={() => {}} options={[{ value: 'a', label: 'A' }]} />
        <SegmentedControl label="y" variant="buttons" value="b" onChange={() => {}} options={[{ value: 'b', label: 'B' }]} />
      </form>,
    )
    fireEvent.click(screen.getByRole('button', { name: 'A' }))
    fireEvent.click(screen.getByRole('button', { name: 'B' }))
    expect(onSubmit).not.toHaveBeenCalled()
  })
})

/**
 * The backstop, in the shape of `storage.test.ts`'s: a seventh hand-rolled group. Six had
 * accumulated, one without its `aria-pressed`, before this component existed.
 */
describe('no hand-rolled toggle groups', () => {
  it('leaves role="group" only in ui/SegmentedControl.tsx', () => {
    const sources = import.meta.glob(['../*.tsx', './*.tsx'], {
      query: '?raw', import: 'default', eager: true,
    }) as Record<string, string>
    const offenders = Object.entries(sources)
      .filter(([path]) => !path.endsWith('.test.tsx') && !path.endsWith('/SegmentedControl.tsx'))
      .filter(([, text]) => /role=["']group["']/.test(text))
      .map(([path]) => path)
    expect(
      offenders,
      `${offenders.join(', ')} hand-rolls a button group. Use <SegmentedControl> so the ` +
      `pressed state, the group name and the two looks stay in one place.`,
    ).toEqual([])
  })
})
