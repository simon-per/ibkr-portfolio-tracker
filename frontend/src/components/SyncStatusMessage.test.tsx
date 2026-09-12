// @vitest-environment jsdom
import { afterEach, describe, it, expect, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

import { SyncStatusMessage } from './SyncStatusMessage'

/**
 * The three outcomes of pressing Sync, and specifically that the middle one exists.
 *
 * IBKR issues about one Flex statement per US-Eastern day, so pressing Sync after the
 * day's run is a no-op — not a success and not a failure. Before the guard, the only way
 * to learn that was to ask IBKR and be refused, which rendered as a red "Sync failed"
 * *and* spent `Code=1025` lockout budget to do it.
 */

const SKIPPED = {
  status: 'skipped' as const,
  reason: 'already_generated_today',
  message: "IBKR has already generated today's statement.",
  last_success_at: '2026-08-08T04:00:00+00:00',
  next_attempt_after: '2026-08-09T04:00:00+00:00',
}

const SYNCED = {
  status: 'success' as const,
  message: 'Successfully synced data from IBKR',
  securities_synced: 43,
  taxlots_synced: 983,
}

afterEach(cleanup)

describe('SyncStatusMessage', () => {
  it('shows nothing before a sync has been attempted', () => {
    const { container } = render(
      <SyncStatusMessage data={undefined} error={null} isPending={false} onForce={() => {}} />
    )
    expect(container.innerHTML).toBe('')
  })

  it('reports counts on a real sync', () => {
    render(<SyncStatusMessage data={SYNCED} error={null} isPending={false} onForce={() => {}} />)
    expect(screen.getByText(/Securities: 43/)).toBeTruthy()
    expect(screen.getByText(/Tax Lots: 983/)).toBeTruthy()
  })

  it('never renders an undefined count on a skip', () => {
    // The specific regression this component was extracted to make testable: a skipped
    // response carries no counts, and the old unconditional success branch would have
    // printed "Securities: undefined" under a green tick.
    const { container } = render(
      <SyncStatusMessage data={SKIPPED} error={null} isPending={false} onForce={() => {}} />
    )
    expect(container.textContent).not.toContain('undefined')
    expect(container.textContent).not.toContain('Securities:')
  })

  it('presents a skip as up to date, not as a failure', () => {
    render(<SyncStatusMessage data={SKIPPED} error={null} isPending={false} onForce={() => {}} />)
    expect(screen.getByText(/Already up to date/)).toBeTruthy()
    expect(screen.queryByText(/Sync failed/)).toBeNull()
    // A skip is a no-op, so claiming success would be its own small lie.
    expect(screen.queryByText(/Sync successful/)).toBeNull()
  })

  it('says when the next statement can be fetched, since that is the actionable part', () => {
    render(<SyncStatusMessage data={SKIPPED} error={null} isPending={false} onForce={() => {}} />)
    expect(screen.getByText(/Next statement available/)).toBeTruthy()
  })

  it('offers a force override and says what it is for', async () => {
    const onForce = vi.fn()
    render(<SyncStatusMessage data={SKIPPED} error={null} isPending={false} onForce={onForce} />)

    await userEvent.click(screen.getByRole('button', { name: /Sync anyway/ }))

    expect(onForce).toHaveBeenCalledOnce()
    // Captioned rather than bare: a forced attempt that fails is a failed statement
    // generation, which is exactly what the Code=1025 lockout counts.
    expect(screen.getByText(/editing the Flex Query in the IBKR portal/)).toBeTruthy()
  })

  it('renders the statement times in the en-US short form, whatever the host locale', () => {
    // `toLocaleString(undefined, …)` followed the viewer's runtime, so the same skip read
    // "09.08.26, 06:00" on a German machine. `lib/utils.ts` pins every other formatter to
    // en-US; the times here go through `formatShortDateTime` now.
    render(<SyncStatusMessage data={SKIPPED} error={null} isPending={false} onForce={() => {}} />)
    const line = screen.getByText(/Next statement available/).textContent ?? ''
    expect(line).toMatch(/available \d{1,2}\/\d{1,2}\/\d{2}, \d{1,2}:\d{2}\s(AM|PM)/)
    expect(line).toMatch(/last synced \d{1,2}\/\d{1,2}\/\d{2}, \d{1,2}:\d{2}\s(AM|PM)/)
  })

  it('does not offer a force override while a sync is already running', () => {
    render(<SyncStatusMessage data={SKIPPED} error={null} isPending onForce={() => {}} />)
    expect(screen.getByRole('button', { name: /Sync anyway/ }).hasAttribute('disabled')).toBe(true)
  })

  it('still reports a real failure', () => {
    render(
      <SyncStatusMessage
        data={undefined}
        error={{ message: 'Code=1015: invalid token' }}
        isPending={false}
        onForce={() => {}}
      />
    )
    expect(screen.getByText(/Sync failed: Code=1015: invalid token/)).toBeTruthy()
  })

  it('shows the error rather than stale data when a forced retry fails', () => {
    // react-query keeps the previous `data` alongside a new error, so the error branch
    // has to win — otherwise a failed "Sync anyway" would still read "Already up to date".
    render(
      <SyncStatusMessage
        data={SKIPPED}
        error={{ message: 'Code=1025: too many failed attempts' }}
        isPending={false}
        onForce={() => {}}
      />
    )
    expect(screen.getByText(/Sync failed/)).toBeTruthy()
    expect(screen.queryByText(/Already up to date/)).toBeNull()
  })
})
