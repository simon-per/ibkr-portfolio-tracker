import { Component, type ReactNode } from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Dashboard } from './components/Dashboard'
import { CryptoDashboard } from './components/CryptoDashboard'
import { ThemeProvider } from './components/ThemeProvider'
import { CurrencyProvider } from './lib/CurrencyContext'
import { ModeProvider, useMode } from './lib/portfolioMode'

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      refetchOnWindowFocus: false,
      retry: 1,
      staleTime: 5 * 60 * 1000, // 5 minutes
    },
  },
})

interface ErrorBoundaryState {
  hasError: boolean
  error: Error | null
}

class ErrorBoundary extends Component<{ children: ReactNode }, ErrorBoundaryState> {
  constructor(props: { children: ReactNode }) {
    super(props)
    this.state = { hasError: false, error: null }
  }

  static getDerivedStateFromError(error: Error): ErrorBoundaryState {
    return { hasError: true, error }
  }

  render() {
    if (this.state.hasError) {
      return (
        <div className="min-h-screen flex items-center justify-center bg-background p-8">
          <div className="max-w-md text-center space-y-4">
            <h1 className="text-2xl font-bold text-foreground">Something went wrong</h1>
            <p className="text-muted-foreground">
              {this.state.error?.message || 'An unexpected error occurred.'}
            </p>
            <button
              onClick={() => window.location.reload()}
              className="inline-flex items-center justify-center rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90"
            >
              Reload
            </button>
          </div>
        </div>
      )
    }

    return this.props.children
  }
}

/**
 * The crypto view's own boundary, one level outside it.
 *
 * The mode is persisted, so a crypto view that throws while rendering would otherwise
 * reach the app-wide boundary above, whose Reload lands straight back in crypto mode and
 * throws again — the stock book unreachable with no way out but clearing site data. This
 * fallback offers the way back. The overview inside has a second, narrower boundary
 * (`LazyTabPanel`) that keeps the header and the mode toggle usable for anything that
 * fails below them.
 */
class CryptoBoundary extends Component<
  { children: ReactNode; onLeave: () => void },
  ErrorBoundaryState
> {
  constructor(props: { children: ReactNode; onLeave: () => void }) {
    super(props)
    this.state = { hasError: false, error: null }
  }

  static getDerivedStateFromError(error: Error): ErrorBoundaryState {
    return { hasError: true, error }
  }

  render() {
    if (!this.state.hasError) return this.props.children
    return (
      <div className="min-h-screen flex items-center justify-center bg-background p-8">
        <div className="max-w-md text-center space-y-4" role="alert">
          <h1 className="text-2xl font-bold text-foreground">The crypto view couldn&apos;t load</h1>
          <p className="text-muted-foreground">
            {this.state.error?.message || 'An unexpected error occurred.'}
          </p>
          <div className="flex justify-center gap-2">
            <button
              type="button"
              onClick={this.props.onLeave}
              className="inline-flex items-center justify-center rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90"
            >
              Back to stocks
            </button>
            <button
              type="button"
              onClick={() => window.location.reload()}
              className="inline-flex items-center justify-center rounded-md border border-input px-4 py-2 text-sm font-medium hover:bg-accent"
            >
              Reload
            </button>
          </div>
        </div>
      </div>
    )
  }
}

/** Stocks or crypto — the whole page under the header is one or the other. */
function ModeView() {
  const { mode, setMode } = useMode()
  if (mode === 'crypto') {
    return (
      <CryptoBoundary onLeave={() => setMode('stocks')}>
        <CryptoDashboard />
      </CryptoBoundary>
    )
  }
  return <Dashboard />
}

function App() {
  return (
    <ThemeProvider defaultTheme="light" storageKey="ibkr-theme">
      <QueryClientProvider client={queryClient}>
        <CurrencyProvider>
          <ModeProvider>
            <ErrorBoundary>
              <div className="min-h-screen bg-background">
                <ModeView />
              </div>
            </ErrorBoundary>
          </ModeProvider>
        </CurrencyProvider>
      </QueryClientProvider>
    </ThemeProvider>
  )
}

export default App
