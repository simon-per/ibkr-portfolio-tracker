// @vitest-environment jsdom
import { describe, it, expect, afterEach, beforeEach, vi } from 'vitest'
import {
  API_KEY_STORAGE_KEY,
  UnauthorizedError,
  api,
  describeErrorBody,
  getApiKey,
  setApiKey,
} from './api'

/**
 * `lib/api.ts` is mostly thin `fetch` wrappers, but the shared `request()` decides
 * what every failure in the app looks like and whether the admin key travels.
 *
 * The bug these were written for: FastAPI answers a validation failure with
 * `detail` as an **array of objects**, and the old code passed it straight to
 * `new Error()`, so a 422 reached the user as the string "[object Object]".
 * Confirmed against the running backend — `/api/tax/report?year=notayear` really
 * does return `{"detail":[{"loc":["query","year"],"msg":"..."}]}`.
 */

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
  localStorage.clear()
})

beforeEach(() => localStorage.clear())

function mockFetch(body: unknown, init: { ok?: boolean; status?: number; statusText?: string } = {}) {
  const spy = vi.fn().mockResolvedValue({
    ok: init.ok ?? true,
    status: init.status ?? 200,
    statusText: init.statusText ?? 'OK',
    json: async () => body,
  })
  vi.stubGlobal('fetch', spy)
  return spy
}

describe('describeErrorBody', () => {
  it('passes a plain string detail through', () => {
    expect(describeErrorBody({ detail: 'start_date must be before end_date' }, 400, 'Bad Request'))
      .toBe('start_date must be before end_date')
  })

  it('renders a FastAPI validation array as readable text', () => {
    const body = {
      detail: [
        {
          type: 'int_parsing',
          loc: ['query', 'year'],
          msg: 'Input should be a valid integer, unable to parse string as an integer',
          input: 'notayear',
        },
      ],
    }
    expect(describeErrorBody(body, 422, 'Unprocessable Entity')).toBe(
      'year: Input should be a valid integer, unable to parse string as an integer',
    )
  })

  it('never stringifies an object into the message', () => {
    // The regression: `new Error([{...}])` produced exactly this.
    const body = { detail: [{ loc: ['query', 'year'], msg: 'bad' }] }
    expect(describeErrorBody(body, 422, '')).not.toContain('[object Object]')
  })

  it('drops the container prefix and keeps the field', () => {
    const body = { detail: [{ loc: ['body', 'base_currency'], msg: 'unsupported' }] }
    expect(describeErrorBody(body, 422, '')).toBe('base_currency: unsupported')
  })

  it('joins several validation problems', () => {
    const body = {
      detail: [
        { loc: ['query', 'year'], msg: 'not an integer' },
        { loc: ['query', 'limit'], msg: 'too large' },
      ],
    }
    expect(describeErrorBody(body, 422, '')).toBe('year: not an integer; limit: too large')
  })

  it('falls back to the status when the body has no usable detail', () => {
    expect(describeErrorBody(null, 500, 'Internal Server Error'))
      .toBe('HTTP 500: Internal Server Error')
    expect(describeErrorBody({}, 502, 'Bad Gateway')).toBe('HTTP 502: Bad Gateway')
    expect(describeErrorBody({ detail: '   ' }, 503, 'Unavailable')).toBe('HTTP 503: Unavailable')
  })

  it('survives a detail shape nobody anticipated', () => {
    // Never throw while building an error message — that would replace the real
    // failure with a second one.
    expect(() => describeErrorBody({ detail: 42 }, 400, 'Bad Request')).not.toThrow()
    expect(describeErrorBody({ detail: [{}] }, 422, 'Unprocessable')).toBe('HTTP 422: Unprocessable')
  })
})

describe('the admin key', () => {
  it('round-trips through localStorage', () => {
    setApiKey('secret')
    expect(localStorage.getItem(API_KEY_STORAGE_KEY)).toBe('secret')
    expect(getApiKey()).toBe('secret')
  })

  it('is removed rather than stored blank', () => {
    setApiKey('secret')
    setApiKey('')
    expect(localStorage.getItem(API_KEY_STORAGE_KEY)).toBeNull()
    expect(getApiKey()).toBe('')
  })

  it('reads as empty when storage is unavailable', () => {
    // Private-mode Safari and some embedded webviews throw on access.
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('denied')
    })
    expect(getApiKey()).toBe('')
  })

  it('does not throw when storage refuses a write', () => {
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('denied')
    })
    expect(() => setApiKey('secret')).not.toThrow()
  })
})

describe('request', () => {
  it('sends the key when one is stored', async () => {
    setApiKey('secret')
    const spy = mockFetch({ base_currency: 'CHF' })
    await api.getSettings()
    const headers = spy.mock.calls[0][1].headers
    expect(headers['X-API-Key']).toBe('secret')
  })

  it('omits the header entirely when no key is stored', async () => {
    // Not an empty string: an empty header would be a credential the backend has
    // to reject rather than an absent one.
    const spy = mockFetch({ base_currency: 'CHF' })
    await api.getSettings()
    expect('X-API-Key' in spy.mock.calls[0][1].headers).toBe(false)
  })

  it('returns the parsed body on success', async () => {
    mockFetch({
      base_currency: 'CHF',
      supported_currencies: ['EUR'],
      dividend_forecast_withholding_pct: 15,
    })
    await expect(api.getSettings()).resolves.toEqual({
      base_currency: 'CHF',
      supported_currencies: ['EUR'],
      dividend_forecast_withholding_pct: 15,
    })
  })

  it('sends the dividend withholding setting as a percentage', async () => {
    const spy = mockFetch({
      base_currency: 'CHF',
      supported_currencies: ['EUR'],
      dividend_forecast_withholding_pct: 26.375,
    })
    await api.updateDividendWithholding(26.375)

    expect(spy.mock.calls[0][0]).toContain('/api/settings/dividend-withholding')
    expect(spy.mock.calls[0][1]).toMatchObject({
      method: 'PUT',
      body: JSON.stringify({ dividend_forecast_withholding_pct: 26.375 }),
    })
  })

  it('reports a validation failure legibly rather than as an object', async () => {
    mockFetch(
      { detail: [{ loc: ['query', 'year'], msg: 'Input should be a valid integer' }] },
      { ok: false, status: 422, statusText: 'Unprocessable Entity' },
    )
    await expect(api.getSettings()).rejects.toThrow('year: Input should be a valid integer')
  })

  it('passes an ordinary HTTPException detail through', async () => {
    mockFetch({ detail: 'Date range too large' }, { ok: false, status: 400, statusText: 'Bad Request' })
    await expect(api.getSettings()).rejects.toThrow('Date range too large')
  })

  describe('401', () => {
    it('points at the lock button when no key is stored', async () => {
      mockFetch({ detail: 'Not authenticated' }, { ok: false, status: 401, statusText: 'Unauthorized' })
      await expect(api.getSettings()).rejects.toThrow(UnauthorizedError)
      await expect(api.getSettings()).rejects.toThrow(/needs the admin key/)
    })

    it('says the saved key was rejected when one is stored', async () => {
      // Different advice: adding a key you already have would not help.
      setApiKey('wrong')
      mockFetch({ detail: 'Not authenticated' }, { ok: false, status: 401, statusText: 'Unauthorized' })
      await expect(api.getSettings()).rejects.toThrow(/saved admin key was rejected/)
    })
  })

  it('falls back to the status when the error body is not JSON', async () => {
    const spy = vi.fn().mockResolvedValue({
      ok: false,
      status: 502,
      statusText: 'Bad Gateway',
      json: async () => {
        throw new SyntaxError('Unexpected token <')
      },
    })
    vi.stubGlobal('fetch', spy)
    await expect(api.getSettings()).rejects.toThrow('HTTP 502: Bad Gateway')
  })

  it('propagates a network failure as an Error', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('Failed to fetch')))
    await expect(api.getSettings()).rejects.toThrow(/Failed to fetch/)
  })
})
