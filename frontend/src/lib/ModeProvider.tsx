import { useCallback, useMemo, useState, type ReactNode } from 'react'
import { writeStored } from './storage'
import { MODE_STORAGE_KEY, ModeContext, readStoredMode, type PortfolioMode } from './portfolioMode'

export function ModeProvider({ children }: { children: ReactNode }) {
  const [mode, setModeState] = useState<PortfolioMode>(readStoredMode)

  const setMode = useCallback((next: PortfolioMode) => {
    setModeState(next)
    writeStored(MODE_STORAGE_KEY, next)
  }, [])

  const value = useMemo(() => ({ mode, setMode }), [mode, setMode])
  return <ModeContext.Provider value={value}>{children}</ModeContext.Provider>
}
