import { createContext, useCallback, useContext, useMemo, useState, type ReactNode } from 'react'
import { readStored, writeStored } from './storage'

/**
 * Which book the app is showing: the stock portfolio (IBKR + pillar 3a) or the crypto one
 * (CoinStats — docs/crypto.md).
 *
 * **Two books, not two tabs.** The crypto view shares no figure with the stock view: no
 * total, no chart, no allocation. So it is a mode that swaps the whole page under the
 * header rather than one more tab beside Performance, where it would sit in the same strip
 * as the stock sections and invite reading one number against the other.
 *
 * Persisted per browser, and **validated on read**: a stored value that is neither mode
 * (an old build's key, a hand edit) falls back to stocks rather than rendering nothing.
 */
export type PortfolioMode = 'stocks' | 'crypto'

export const MODE_STORAGE_KEY = 'ibkr-portfolio-mode'

export function readStoredMode(): PortfolioMode {
  return readStored(MODE_STORAGE_KEY) === 'crypto' ? 'crypto' : 'stocks'
}

interface ModeContextValue {
  mode: PortfolioMode
  setMode: (mode: PortfolioMode) => void
}

const ModeContext = createContext<ModeContextValue>({
  mode: 'stocks',
  setMode: () => {},
})

export function ModeProvider({ children }: { children: ReactNode }) {
  const [mode, setModeState] = useState<PortfolioMode>(readStoredMode)

  const setMode = useCallback((next: PortfolioMode) => {
    setModeState(next)
    writeStored(MODE_STORAGE_KEY, next)
  }, [])

  const value = useMemo(() => ({ mode, setMode }), [mode, setMode])
  return <ModeContext.Provider value={value}>{children}</ModeContext.Provider>
}

export function useMode(): ModeContextValue {
  return useContext(ModeContext)
}
