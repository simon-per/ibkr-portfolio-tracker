import { ForbiddenError, UnauthorizedError } from './api'

/**
 * Whether the crypto book can be read, from what `/api/crypto/*` answered.
 *
 * The crypto routes are private and fail closed (docs/crypto.md), so there are two
 * refusals and they need different words: **locked** — the server has a key and this
 * browser has not presented it, fixed by the lock button — and **no key on the server**,
 * which no browser can fix and the lock button is not even shown for. One helper so the
 * header's status line and the overview cannot disagree about which one it is.
 */
export type CryptoAccess = 'open' | 'locked' | 'server-has-no-key'

export function cryptoAccess(error: unknown, writeAuthEnabled: boolean | undefined): CryptoAccess {
  if (writeAuthEnabled === false || error instanceof ForbiddenError) return 'server-has-no-key'
  if (error instanceof UnauthorizedError) return 'locked'
  return 'open'
}

export function isAccessRefusal(error: unknown): boolean {
  return error instanceof UnauthorizedError || error instanceof ForbiddenError
}

/**
 * TanStack Query's `retry` for the crypto reads: once for a transient failure, never for a
 * refusal — asking again with the same key cannot change the answer, and a retry would only
 * delay the locked state by a round trip.
 */
export function retryUnlessRefused(failureCount: number, error: unknown): boolean {
  return !isAccessRefusal(error) && failureCount < 1
}
