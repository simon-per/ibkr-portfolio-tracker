import { createContext, useContext } from 'react'
import { readStored } from './storage'

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

export interface ModeContextValue {
  mode: PortfolioMode
  setMode: (mode: PortfolioMode) => void
}

/** Read by `useMode`; provided by `ModeProvider.tsx` (a component module of its own, for
 *  React Fast Refresh). */
export const ModeContext = createContext<ModeContextValue>({
  mode: 'stocks',
  setMode: () => {},
})

export function useMode(): ModeContextValue {
  return useContext(ModeContext)
}
