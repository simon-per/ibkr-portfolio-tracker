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
  'FTSE', 'IMI', 'LVMH', 'HDFC', 'ICICI', 'SPI', 'SMI', 'DAX', 'CAC', 'STOXX', 'ACWI',
  'USD', 'EUR', 'CHF', 'GBP', 'JPY', 'ASX', 'TSX', 'ING', 'BNP', 'BMW', 'SAS', 'BHP', 'RELX',
])

/** IBKR's 30-character truncations and abbreviations, spelled out. */
const EXPAND: Record<string, string> = {
  TOT: 'Total', STK: 'Stock', INTL: 'International', MANUFAC: 'Manufacturing',
  GRO: 'Group', GRP: 'Group', HOLDI: 'Holding', HLDGS: 'Holdings', TRST: 'Trust',
  NASD: 'Nasdaq', ISHRS: 'iShares', TECH: 'Tech', SEMICONDUCTO: 'Semiconductor',
  VUI: 'Vuitton',
}

/** Brands whose capitals are inside the word, which no casing rule can recover. */
const CASED: Record<string, string> = {
  ISHARES: 'iShares', JPMORGAN: 'JPMorgan', ABBVIE: 'AbbVie', UNITEDHEALTH: 'UnitedHealth',
  PAYPAL: 'PayPal', BLACKROCK: 'BlackRock', "MCDONALD'S": "McDonald's", EBAY: 'eBay',
  FEDEX: 'FedEx', EXXONMOBIL: 'ExxonMobil', ASTRAZENECA: 'AstraZeneca', BIONTECH: 'BioNTech',
  MERCADOLIBRE: 'MercadoLibre', DOORDASH: 'DoorDash', MONGODB: 'MongoDB', PEPSICO: 'PepsiCo',
  HUBSPOT: 'HubSpot', SERVICENOW: 'ServiceNow', CROWDSTRIKE: 'CrowdStrike', NETAPP: 'NetApp',
  TOTALENERGIES: 'TotalEnergies', ARCELORMITTAL: 'ArcelorMittal', SOFTBANK: 'SoftBank',
  STMICROELECTRONICS: 'STMicroelectronics', MEDIATEK: 'MediaTek', VANECK: 'VanEck',
  WISDOMTREE: 'WisdomTree', GLAXOSMITHKLINE: 'GlaxoSmithKline', DUPONT: 'DuPont',
  SOFI: 'SoFi', IONQ: 'IonQ', GODADDY: 'GoDaddy', LINKEDIN: 'LinkedIn', YOUTUBE: 'YouTube',
}

/** Joining words, lower-case anywhere but first: "Bank of America", "Moët et Chandon". */
const SMALL_WORDS = new Set(['OF', 'AND', 'THE', 'FOR', 'DE', 'DU', 'DES', 'LA', 'LE', 'ET', 'UND', 'VAN', 'DER', 'DI', 'DEL'])

/** Legal-form words dropped from the end of a name, repeatedly ("CO LTD", "INC."). */
const LEGAL_SUFFIX =
  /[\s,]+(INC|INCORPORATED|CORP|CORPORATION|CO|COMPANY|LTD|LIMITED|PLC|N\.?V|S\.?A|AG|SE|LLC|L\.?P)\.?$/i

/** What a stripped "& Co" / "and Company" leaves behind: "JPMORGAN CHASE &" would read as a typo. */
const DANGLING_JOIN = /[\s,]+(&|AND)$/i

function titleWord(word: string, first: boolean): string {
  const upper = word.toUpperCase()
  if (KEEP_UPPER.has(upper)) return upper
  if (EXPAND[upper]) return EXPAND[upper]
  if (CASED[upper]) return CASED[upper]
  if (!first && SMALL_WORDS.has(upper)) return word.toLowerCase()
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

  // Registered/bearer-share tails on European listings ("NESTLE SA-REG", "ROCHE HOLDING
  // AG-GENUSSCHEIN").
  name = name.replace(/-(REG|REGISTERED|BR|BEARER|GENUSSCHEIN|NAMEN)$/i, '')

  // The share class first, with IBKR's hyphen ("ALPHABET INC-CL C") or without it, as the
  // fund issuers' holdings files write it ("ALPHABET INC CLASS C") — so the legal-form and
  // domicile rules below see the name's real end. Set aside to survive the legal-suffix
  // strip ("ALPHABET INC Class A").
  name = name.replace(/(\s*-\s*|\s+)(CL|CLASS)\s+([A-Z])(\s+(SHARES|SHS))?$/i, ' Class $3')
  const classTail = name.match(/ Class [A-Z]$/)?.[0] ?? ''
  if (classTail) name = name.slice(0, -classTail.length)

  // The Nordic legal form, whose slash the domicile rule below would otherwise read as one
  // ("NOVO NORDISK A/S-B" became "Novo Nordisk A").
  name = name.replace(/\s+A\/S(\s*-\s*[A-Z])?$/i, '')

  // A domicile after a slash ("LTD/CAYMAN ISL-A") and IBKR's other listing tails.
  name = name.replace(/\/[A-Z .]+(-[A-Z])?$/, '')
  name = name.replace(/\s+-\s+([A-Z])$/, '')
  name = name.replace(/\s+(REG SHS|NV-NY REG SHS|SPON ADR|SPONS ADR|-SPON ADR|ADR)$/i, '')
  name = name.replace(/\s+NV-NY$/i, '')

  for (let i = 0; i < 3 && LEGAL_SUFFIX.test(name); i++) {
    name = name.replace(LEGAL_SUFFIX, '').replace(DANGLING_JOIN, '')
  }
  name = name.replace(/[\s,]+$/, '')

  if (/[a-z]/.test(name)) return name + classTail
  return name.split(/\s+/).map((word, i) => titleWord(word, i === 0)).join(' ') + classTail
}
