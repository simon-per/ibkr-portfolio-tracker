// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, renderHook } from '@testing-library/react'
import { clampHeight, storedHeight, useResizableHeight } from './useResizableHeight'

const OPTIONS = { storageKey: 'testHeight', initial: 260, min: 120, max: 560 }

beforeEach(() => localStorage.clear())
afterEach(() => vi.restoreAllMocks())

/** Just enough of a pointer event for the handle: a position and a capture target. */
function pointer(clientY: number) {
  const target = {
    setPointerCapture: vi.fn(),
    releasePointerCapture: vi.fn(),
    hasPointerCapture: vi.fn(() => true),
  }
  return { clientY, pointerId: 1, currentTarget: target, preventDefault: vi.fn() } as never
}

function key(name: string) {
  return { key: name, preventDefault: vi.fn() } as never
}

describe('clampHeight / storedHeight', () => {
  it('keeps a height inside its range, in whole pixels', () => {
    expect(clampHeight(50, 120, 560)).toBe(120)
    expect(clampHeight(900, 120, 560)).toBe(560)
    expect(clampHeight(300.4, 120, 560)).toBe(300)
  })

  it('falls back when nothing usable is stored', () => {
    expect(storedHeight('testHeight', 260, 120, 560)).toBe(260)
    localStorage.setItem('testHeight', 'tall')
    expect(storedHeight('testHeight', 260, 120, 560)).toBe(260)
    localStorage.setItem('testHeight', '9999')
    expect(storedHeight('testHeight', 260, 120, 560)).toBe(560)
  })
})

describe('useResizableHeight', () => {
  it('opens at the default, or at what this viewer left it at', () => {
    expect(renderHook(() => useResizableHeight(OPTIONS)).result.current.height).toBe(260)
    localStorage.setItem('testHeight', '400')
    expect(renderHook(() => useResizableHeight(OPTIONS)).result.current.height).toBe(400)
  })

  it('follows a drag, and stores the height once when it ends', () => {
    const { result } = renderHook(() => useResizableHeight(OPTIONS))
    const setItem = vi.spyOn(Storage.prototype, 'setItem')

    act(() => result.current.handleProps.onPointerDown(pointer(100)))
    act(() => result.current.handleProps.onPointerMove(pointer(180)))
    expect(result.current.height).toBe(340)
    expect(setItem).not.toHaveBeenCalled()

    act(() => result.current.handleProps.onPointerMove(pointer(2000)))
    expect(result.current.height).toBe(560)              // clamped while dragging too
    act(() => result.current.handleProps.onPointerUp(pointer(140)))
    expect(result.current.height).toBe(300)
    expect(localStorage.getItem('testHeight')).toBe('300')
  })

  it('ignores a move with no drag in progress', () => {
    const { result } = renderHook(() => useResizableHeight(OPTIONS))
    act(() => result.current.handleProps.onPointerMove(pointer(500)))
    expect(result.current.height).toBe(260)
  })

  it('resizes from the keyboard: down grows, up shrinks, Home and End go to the ends', () => {
    const { result } = renderHook(() => useResizableHeight(OPTIONS))
    act(() => result.current.handleProps.onKeyDown(key('ArrowDown')))
    expect(result.current.height).toBe(280)
    act(() => result.current.handleProps.onKeyDown(key('ArrowUp')))
    act(() => result.current.handleProps.onKeyDown(key('ArrowUp')))
    expect(result.current.height).toBe(240)
    act(() => result.current.handleProps.onKeyDown(key('End')))
    expect(result.current.height).toBe(560)
    act(() => result.current.handleProps.onKeyDown(key('Home')))
    expect(result.current.height).toBe(120)
    expect(result.current.handleProps['aria-valuenow']).toBe(120)
    act(() => result.current.handleProps.onKeyDown(key('Enter')))
    expect(result.current.height).toBe(120)
  })

  it('still resizes when the browser refuses storage', () => {
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('SecurityError')
    })
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('QuotaExceededError')
    })
    const { result } = renderHook(() => useResizableHeight(OPTIONS))
    expect(result.current.height).toBe(260)
    act(() => result.current.handleProps.onKeyDown(key('ArrowDown')))
    expect(result.current.height).toBe(280)
  })
})
