import { describe, expect, it } from 'vitest'
import { ForbiddenError, UnauthorizedError } from './api'
import { cryptoAccess, isAccessRefusal, retryUnlessRefused } from './cryptoAccess'

describe('cryptoAccess', () => {
  it('tells "add your key" from "the server has none"', () => {
    expect(cryptoAccess(new UnauthorizedError('x'), true)).toBe('locked')
    expect(cryptoAccess(new ForbiddenError('x'), true)).toBe('server-has-no-key')
    // /health already says so: no request needed to know the lock cannot open.
    expect(cryptoAccess(null, false)).toBe('server-has-no-key')
  })

  it('is open for anything else, including an ordinary failure', () => {
    expect(cryptoAccess(null, true)).toBe('open')
    expect(cryptoAccess(new Error('502'), undefined)).toBe('open')
  })
})

describe('retryUnlessRefused', () => {
  it('retries a transient failure once and a refusal never', () => {
    expect(retryUnlessRefused(0, new Error('network'))).toBe(true)
    expect(retryUnlessRefused(1, new Error('network'))).toBe(false)
    expect(retryUnlessRefused(0, new UnauthorizedError('x'))).toBe(false)
    expect(retryUnlessRefused(0, new ForbiddenError('x'))).toBe(false)
    expect(isAccessRefusal(new Error('x'))).toBe(false)
  })
})
