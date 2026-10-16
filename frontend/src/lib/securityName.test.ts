import { describe, expect, it } from 'vitest'
import { displayName } from './securityName'

describe('displayName', () => {
  it.each([
    ['META PLATFORMS INC-CLASS A', 'Meta Platforms Class A'],
    ['ALPHABET INC-CL C', 'Alphabet Class C'],
    ['MASTERCARD INC - A', 'Mastercard'],
    ['NU HOLDINGS LTD/CAYMAN ISL-A', 'Nu Holdings'],
    ['TAIWAN SEMICONDUCTOR MANUFAC', 'Taiwan Semiconductor Manufacturing'],
    ['SAMSUNG ELECTRONICS CO LTD', 'Samsung Electronics'],
    ['AMAZON.COM INC', 'Amazon.com'],
    ['VANGUARD TOT WORLD STK ETF', 'Vanguard Total World Stock ETF'],
    ['ASML HOLDING NV-NY REG SHS', 'ASML Holding'],
    ['NVIDIA CORP', 'NVIDIA'],
    ['S&P GLOBAL INC', 'S&P Global'],
    ["MOODY'S CORP", "Moody's"],
    ['INFINEON TECHNOLOGIES AG', 'Infineon Technologies'],
    ['ADVANCED MICRO DEVICES, INC.', 'Advanced Micro Devices'],
    ['MICROSOFT CORPORATION', 'Microsoft'],
    ['X ARTIFICIAL INTEL BIG DA 1C', 'X Artificial Intel Big Da 1C'],
  ])('%s → %s', (raw, expected) => {
    expect(displayName(raw)).toBe(expected)
  })

  it('keeps a name someone already cased, dropping only its legal form', () => {
    expect(displayName('ASML Holding N.V.')).toBe('ASML Holding')
    expect(displayName('Swisscanto (CH) Index Equity Fund World ex CH')).toBe(
      'Swisscanto (CH) Index Equity Fund World ex CH',
    )
  })

  it('returns an empty string for a missing name', () => {
    expect(displayName(null)).toBe('')
    expect(displayName('')).toBe('')
  })
})
