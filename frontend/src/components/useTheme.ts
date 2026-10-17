import { createContext, useContext } from 'react'

/**
 * The theme context and its hook. `ThemeProvider.tsx` provides it; they live apart because
 * a module exporting a component and anything else defeats React Fast Refresh.
 */
export type Theme = 'dark' | 'light' | 'system'

export type ThemeProviderState = {
  theme: Theme
  setTheme: (theme: Theme) => void
}

const initialState: ThemeProviderState = {
  theme: 'system',
  setTheme: () => null,
}

export const ThemeProviderContext = createContext<ThemeProviderState>(initialState)

export const useTheme = () => {
  const context = useContext(ThemeProviderContext)

  if (context === undefined)
    throw new Error('useTheme must be used within a ThemeProvider')

  return context
}
