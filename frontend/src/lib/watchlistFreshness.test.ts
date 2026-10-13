import { describe, expect, it } from 'vitest'
import { WATCHLIST_STALE_AFTER_DAYS, watchlistFreshness } from './watchlistFreshness'

const now = new Date('2026-10-13T10:00:00+00:00')
const item = (last_synced: string | null) => ({ last_synced })

describe('watchlistFreshness', () => {
  it('has nothing to say about an empty watchlist', () => {
    expect(watchlistFreshness([], now)).toBeNull()
  })

  it('reports one time when every row came from the same run', () => {
    const f = watchlistFreshness(
      [item('2026-10-13T02:00:05+00:00'), item('2026-10-13T02:01:40+00:00')],
      now,
    )!
    expect(f.oldest?.toISOString()).toBe('2026-10-13T02:00:05.000Z')
    expect(f.newest?.toISOString()).toBe('2026-10-13T02:01:40.000Z')
    expect(f).toMatchObject({ neverSynced: 0, mixed: false, stale: false })
  })

  it('headlines the oldest row and flags the spread when rows are days apart', () => {
    const f = watchlistFreshness(
      [item('2026-10-13T02:00:00+00:00'), item('2026-08-25T09:00:00+00:00')],
      now,
    )!
    expect(f.oldest?.toISOString()).toBe('2026-08-25T09:00:00.000Z')
    expect(f).toMatchObject({ mixed: true, stale: true })
  })

  it('stays quiet across an ordinary weekend and turns stale past the threshold', () => {
    // Saturday's run read on Tuesday just before the next one: three days, the
    // schedule's own longest gap.
    const weekend = watchlistFreshness([item('2026-10-10T02:00:00+00:00')], new Date('2026-10-13T01:59:00+00:00'))!
    expect(weekend.stale).toBe(false)

    const late = new Date(Date.parse('2026-10-10T02:00:00+00:00') + (WATCHLIST_STALE_AFTER_DAYS * 24 + 1) * 3600_000)
    expect(watchlistFreshness([item('2026-10-10T02:00:00+00:00')], late)!.stale).toBe(true)
  })

  it('counts a row that was never synced, and that alone is stale', () => {
    const f = watchlistFreshness([item('2026-10-13T02:00:00+00:00'), item(null)], now)!
    expect(f).toMatchObject({ neverSynced: 1, stale: true })
    expect(f.oldest?.toISOString()).toBe('2026-10-13T02:00:00.000Z')
  })

  it('has no time at all when nothing was ever synced', () => {
    expect(watchlistFreshness([item(null)], now)).toEqual({
      oldest: null, newest: null, neverSynced: 1, mixed: false, stale: true,
    })
  })
})
