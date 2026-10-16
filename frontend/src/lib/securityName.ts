/**
 * A security's name as a person would write it.
 *
 * IBKR's descriptions are upper-case, truncated to ~30 characters and carry listing noise
 * ("META PLATFORMS INC-CLASS A", "NU HOLDINGS LTD/CAYMAN ISL-A", "TAIWAN SEMICONDUCTOR
 * MANUFAC"); GLEIF's legal names are often upper-case too ("ADVANCED MICRO DEVICES, INC.").
 * Display only — every lookup, sort key and export keeps the raw string.
 *
 * A name that already has lower-case letters was written by someone who meant its casing
 * ("Swisscanto (CH) Index Equity Fund", "ASML Holding N.V."), so only its legal suffix goes.
 */

/** Words that stay upper-case: acronyms and brands written that way. */
const KEEP_UPPER = new Set([
  'ETF', 'ASML', 'AMD', 'S&P', 'MSCI', 'USA', 'US', 'UK', 'EU', 'AI', 'II', 'III', 'IV',
  'NXP', 'SK', 'TSMC', 'IBKR', 'HP', 'NVIDIA', 'ESG', 'REIT', 'SPDR', 'CH', 'UCITS', 'ADR',
  'IPF', 'EM', 'PHLX', 'NYSE', 'ABB', 'UBS', 'SAP', 'BYD', 'CATL', 'IBM', 'AT&T', 'ARM',
])

/** IBKR's 30-character truncations and abbreviations, spelled out. */
const EXPAND: Record<string, string> = {
  TOT: 'Total', STK: 'Stock', INTL: 'International', MANUFAC: 'Manufacturing',
  GRO: 'Group', GRP: 'Group', HOLDI: 'Holding', HLDGS: 'Holdings', TRST: 'Trust',
  NASD: 'Nasdaq', ISHRS: 'iShares', TECH: 'Tech', SEMICONDUCTO: 'Semiconductor',
}

/** Legal-form words dropped from the end of a name, repeatedly ("CO LTD", "INC."). */
const LEGAL_SUFFIX =
  /[\s,]+(INC|INCORPORATED|CORP|CORPORATION|CO|COMPANY|LTD|LIMITED|PLC|N\.?V|S\.?A|AG|SE|LLC|L\.?P)\.?$/i

function titleWord(word: string): string {
  const upper = word.toUpperCase()
  if (KEEP_UPPER.has(upper)) return upper
  if (EXPAND[upper]) return EXPAND[upper]
  // Share-class codes and tickers-with-digits ("1C", "3X") read as codes, not words.
  if (/\d/.test(word)) return upper
  // Hyphenated and dotted parts each get a capital: "Amazon.com", "Coca-Cola".
  return word
    .toLowerCase()
    .replace(/(^|[-/(])([a-z])/g, (_, sep: string, ch: string) => sep + ch.toUpperCase())
}

export function displayName(raw: string | null | undefined): string {
  if (!raw) return ''
  let name = raw.trim()

  // A domicile after a slash ("LTD/CAYMAN ISL-A") and IBKR's share-class tails.
  name = name.replace(/\/[A-Z .]+(-[A-Z])?$/, '')
  name = name.replace(/\s*-\s*(CL|CLASS)\s+([A-Z])$/i, ' Class $2')
  name = name.replace(/\s+-\s+([A-Z])$/, '')
  name = name.replace(/\s+(REG SHS|NV-NY REG SHS|SPON ADR|SPONS ADR|-SPON ADR|ADR)$/i, '')
  name = name.replace(/\s+NV-NY$/i, '')

  // The class tail must survive the legal-suffix strip ("ALPHABET INC Class A").
  const classTail = name.match(/ Class [A-Z]$/)?.[0] ?? ''
  if (classTail) name = name.slice(0, -classTail.length)
  for (let i = 0; i < 3 && LEGAL_SUFFIX.test(name); i++) name = name.replace(LEGAL_SUFFIX, '')
  name = name.replace(/[\s,]+$/, '')

  if (/[a-z]/.test(name)) return name + classTail
  return name.split(/\s+/).map(titleWord).join(' ') + classTail
}
