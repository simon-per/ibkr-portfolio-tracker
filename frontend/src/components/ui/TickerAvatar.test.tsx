// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { api } from '@/lib/api'
import { TickerAvatar } from './TickerAvatar'

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

function withClient(node: React.ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={client}>{node}</QueryClientProvider>)
}

describe('TickerAvatar', () => {
  it('shows the stored logo, versioned, in place of the monogram', async () => {
    vi.spyOn(api, 'getLogos').mockResolvedValue({ logos: { '7': 'abc123def456' } })
    const { container } = withClient(<TickerAvatar symbol="GOOGL" securityId={7} />)
    const img = await waitFor(() => {
      const found = container.querySelector('img')
      expect(found).not.toBeNull()
      return found!
    })
    expect(img.getAttribute('src')).toMatch(/\/api\/logos\/7\?v=abc123def456$/)
    expect(img.getAttribute('alt')).toBe('')
    expect(screen.queryByText('GO')).toBeNull()
  })

  it('keeps the monogram for a security without a logo', async () => {
    const getLogos = vi.spyOn(api, 'getLogos').mockResolvedValue({ logos: { '8': 'x' } })
    const { container } = withClient(<TickerAvatar symbol="GOOGL" securityId={7} />)
    await waitFor(() => expect(getLogos).toHaveBeenCalled())
    expect(screen.getByText('GO')).toBeTruthy()
    expect(container.querySelector('img')).toBeNull()
  })

  it('falls back to the monogram when the image fails to load', async () => {
    vi.spyOn(api, 'getLogos').mockResolvedValue({ logos: { '7': 'abc' } })
    const { container } = withClient(<TickerAvatar symbol="GOOGL" securityId={7} />)
    const img = await waitFor(() => {
      const found = container.querySelector('img')
      expect(found).not.toBeNull()
      return found!
    })
    fireEvent.error(img)
    expect(await screen.findByText('GO')).toBeTruthy()
    expect(container.querySelector('img')).toBeNull()
  })

  it('never asks for the manifest without a security id', () => {
    const getLogos = vi.spyOn(api, 'getLogos')
    withClient(<TickerAvatar symbol="AMZN" />)
    expect(screen.getByText('AM')).toBeTruthy()
    expect(getLogos).not.toHaveBeenCalled()
  })

  it('renders the monogram outside a QueryClientProvider', () => {
    render(<TickerAvatar symbol="AMZN" securityId={1} />)
    expect(screen.getByText('AM')).toBeTruthy()
  })
})
