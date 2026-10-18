import { describe, expect, it } from 'vitest'
import { readFileSync, readdirSync, statSync } from 'node:fs'
import { join, relative, sep } from 'node:path'
import { formatCurrency, formatMoneyWhole } from './utils'

/**
 * Display conventions that were each copied by hand somewhere: money strings, the dark
 * theme's native controls, and the absent glyph. Source scans in the style of
 * `nullsLast.test.ts` (issue #49).
 */
const SRC = join(__dirname, '..')
const rel = (p: string) => relative(SRC, p).split(sep).join('/')

function sources(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name)
    if (statSync(path).isDirectory()) return sources(path)
    return /\.(ts|tsx)$/.test(name) && !/\.test\.(ts|tsx)$/.test(name) ? [path] : []
  })
}

/** Source with comments blanked, line breaks kept so offsets still map to lines. */
function code(path: string): string {
  return readFileSync(path, 'utf8')
    .replace(/\/\*[\s\S]*?\*\//g, (m) => m.replace(/[^\n]/g, ' '))
    .replace(/\/\/.*$/gm, '')
}

/** `path:line` for every match of a global pattern, across lines. */
function hits(re: RegExp, skip: (relPath: string) => boolean = () => false): string[] {
  return sources(SRC)
    .filter((p) => !skip(rel(p)))
    .flatMap((p) => {
      const src = code(p)
      return [...src.matchAll(re)].map((m) => `${rel(p)}:${src.slice(0, m.index).split('\n').length}`)
    })
}

/**
 * A currency symbol glued to a figure: `${curSym}${n}`, `{curSym}{n}` (on one line or
 * two), `${curSym}0`, or a literal `€`/`$`/`CHF ` before an interpolation.
 */
const HAND_BUILT_MONEY =
  /\$\{\s*(?:curSym|currencySymbol)\s*\}\s*(?:\$\{|\d)|\{\s*(?:curSym|currencySymbol)\s*\}\s*\{|(?:€|\$|CHF[ \u00a0]|CHF\\u00a0)\$\{/g

/** The shared formatters are where a symbol may meet a number. */
const FORMATTERS = new Set(['lib/utils.ts', 'lib/dividendFormat.ts'])

describe('money strings', () => {
  it('put the sign before the symbol, in whole units', () => {
    expect(formatMoneyWhole(-1234, 'CHF')).toBe('-CHF\u00a01,234')
    expect(formatMoneyWhole(-1234, 'CHF')).toBe(formatCurrency(-1234, 'CHF').replace('.00', ''))
    expect(formatMoneyWhole(1234.5, 'EUR')).toBe('€1,235')
    expect(formatMoneyWhole(-56, 'USD')).toBe('-$56')
  })

  it('print an amount that rounds to zero unsigned', () => {
    expect(formatMoneyWhole(-0.3, 'CHF')).toBe('CHF\u00a00')
    expect(formatMoneyWhole(0, 'EUR')).toBe('€0')
  })

  it('the scan recognises every hand-built shape', () => {
    const samples = [
      '`${curSym}${formatCount(v)}`',
      '<b>{curSym}{formatCount(v)}</b>',
      '<b>{curSym}\n  {formatCount(v)}</b>',
      '`${curSym}0`',
      '`€${v}`',
      '`CHF ${v}`',
    ]
    for (const s of samples) expect(s.match(HAND_BUILT_MONEY), s).not.toBeNull()
  })

  it('are never built by hand from a symbol and a number', () => {
    // "CHF -1,234" where formatCurrency prints "-CHF 1,234.00". Use formatMoneyWhole,
    // formatCurrency, or formatAxisMoney on an axis.
    expect(hits(HAND_BUILT_MONEY, (p) => FORMATTERS.has(p))).toEqual([])
  })
})

describe('the dark theme', () => {
  it('declares color-scheme so native scrollbars and <select> popups follow it', () => {
    const css = readFileSync(join(SRC, 'index.css'), 'utf8')
    expect(css).toMatch(/:root\s*\{[^}]*color-scheme:\s*light/)
    // `ThemeProvider` toggles `.dark` on <html>.
    expect(css).toMatch(/\.dark\s*\{[^}]*color-scheme:\s*dark/)
  })
})

/**
 * A plus written by hand before a figure: `n >= 0 ? '+' : ''`, `` `+${…}` ``, `>+{…}`. Ten
 * sites did, with two zero rules between them; `formatSigned` is the one rule.
 */
const HAND_SIGNED = /\?\s*'\+'\s*:|`\+\$\{|>\+\{/g

describe('signed figures', () => {
  it('the scan recognises every hand-signed shape', () => {
    const samples = [
      "`${n >= 0 ? '+' : ''}${money(n)}`",
      "{v > 0 ? '+' : ''}{v.toFixed(1)}%",
      '`+${money(n)} invested`',
      '<span>+{money(n)}</span>',
    ]
    for (const s of samples) expect(s.match(HAND_SIGNED), s).not.toBeNull()
  })

  it('are signed through formatSigned, never by hand', () => {
    expect(hits(HAND_SIGNED, (p) => p === 'lib/utils.ts')).toEqual([])
  })
})

describe('the absent glyph', () => {
  it('is ABSENT (em dash) everywhere, never an en dash placeholder', () => {
    expect(hits(/\{'\\u2013'\}|'\\u2013'|'–'|"–"|>\s*–\s*</g)).toEqual([])
  })
})
