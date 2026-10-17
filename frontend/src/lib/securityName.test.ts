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
    // A stripped "& Co" / "and Company" takes its joining word with it.
    ['JPMORGAN CHASE & CO', 'JPMorgan Chase'],
    ['ELI LILLY AND CO', 'Eli Lilly'],
    ['MERCK & CO INC', 'Merck'],
    ['PROCTER & GAMBLE CO', 'Procter & Gamble'],
    ['JOHNSON & JOHNSON', 'Johnson & Johnson'],
    // Registered-share and bearer tails, and the Nordic A/S.
    ['NESTLE SA-REG', 'Nestle'],
    ['ALLIANZ SE-REG', 'Allianz'],
    ['UBS GROUP AG-REG', 'UBS Group'],
    ['ROCHE HOLDING AG-GENUSSCHEIN', 'Roche Holding'],
    ['NOVO NORDISK A/S-B', 'Novo Nordisk'],
    ['VISA INC-CLASS A SHARES', 'Visa Class A'],
    // Issuer holdings files write the class without IBKR's hyphen.
    ['ALPHABET INC CLASS C', 'Alphabet Class C'],
    ['BERKSHIRE HATHAWAY INC CLASS B', 'Berkshire Hathaway Class B'],
    ['NOVO NORDISK A/S CLASS B', 'Novo Nordisk Class B'],
    ['META PLATFORMS INC CLASS A', 'Meta Platforms Class A'],
    // Index and brand names keep their casing.
    ['VANGUARD FTSE ALL-WORLD UCITS ETF USD ACC', 'Vanguard FTSE All-World UCITS ETF USD Acc'],
    ['ISHARES CORE MSCI EM IMI UCITS ETF USD ACC', 'iShares Core MSCI EM IMI UCITS ETF USD Acc'],
    ['LVMH MOET HENNESSY LOUIS VUI', 'LVMH Moet Hennessy Louis Vuitton'],
    ['ABBVIE INC', 'AbbVie'],
    ['UNITEDHEALTH GROUP INC', 'UnitedHealth Group'],
    ['PAYPAL HOLDINGS INC', 'PayPal Holdings'],
    ['HDFC BANK LTD', 'HDFC Bank'],
    // Joining words stay small after the first word.
    ['BANK OF AMERICA CORP', 'Bank of America'],
    ['THE TRADE DESK INC', 'The Trade Desk'],
  ])('%s → %s', (raw, expected) => {
    expect(displayName(raw)).toBe(expected)
  })

  it('keeps a name someone already cased, dropping only its legal form', () => {
    expect(displayName('ASML Holding N.V.')).toBe('ASML Holding')
    expect(displayName('Swisscanto (CH) Index Equity Fund World ex CH')).toBe(
      'Swisscanto (CH) Index Equity Fund World ex CH',
    )
  })

  it('drops a dangling "and" left by a cased legal form too', () => {
    expect(displayName('Eli Lilly and Company')).toBe('Eli Lilly')
    expect(displayName('Merck & Co., Inc.')).toBe('Merck')
  })

  it('returns an empty string for a missing name', () => {
    expect(displayName(null)).toBe('')
    expect(displayName('')).toBe('')
  })
})
