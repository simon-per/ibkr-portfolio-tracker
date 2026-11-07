// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { CompanyLink } from './CompanyLink'
import { CompanyLinksContext, companyLinks } from '@/lib/companyLinks'
import { FIXTURE_COMPANIES, NVDA_CIK } from '@/lib/companyFinancials.fixture'

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

function withLinks(node: React.ReactNode) {
  return (
    <CompanyLinksContext.Provider value={companyLinks(FIXTURE_COMPANIES)}>{node}</CompanyLinksContext.Provider>
  )
}

describe('CompanyLink', () => {
  it('links a holding the Company tab has to its company', () => {
    render(withLinks(<CompanyLink securityId={11}>NVIDIA</CompanyLink>))
    const link = screen.getByRole('link', { name: 'NVIDIA' })
    expect(link.getAttribute('href')).toBe(`#company/${NVDA_CIK}`)
    expect(link.getAttribute('title')).toBe('Open company')
  })

  it('draws a holding the tab does not have exactly as before: no link, no wrapper', () => {
    const { container } = render(withLinks(<CompanyLink securityId={999}><span>Fund</span></CompanyLink>))
    expect(screen.queryByRole('link')).toBeNull()
    expect(container.innerHTML).toBe('<span>Fund</span>')
  })

  it('makes a logo beside a linked name a mouse-only twin, so a row is one link to a keyboard', () => {
    render(withLinks(
      <>
        <CompanyLink securityId={11} decorative><span>logo</span></CompanyLink>
        <CompanyLink securityId={11}>NVDA</CompanyLink>
      </>,
    ))
    expect(screen.getAllByRole('link')).toHaveLength(1)
    const logo = screen.getByText('logo').closest('a')!
    expect(logo.getAttribute('href')).toBe(`#company/${NVDA_CIK}`)
    expect(logo.getAttribute('tabindex')).toBe('-1')
    expect(logo.getAttribute('aria-hidden')).toBe('true')
  })

  it('draws no link outside the Dashboard, where there is no company list', () => {
    render(<CompanyLink securityId={11}>NVIDIA</CompanyLink>)
    expect(screen.queryByRole('link')).toBeNull()
  })

  it('opens the company at its top, and leaves a modified click to the browser', () => {
    const scrollTo = vi.fn()
    vi.stubGlobal('scrollTo', scrollTo)
    vi.stubGlobal('requestAnimationFrame', (cb: FrameRequestCallback) => {
      cb(0)
      return 0
    })
    render(withLinks(<CompanyLink securityId={11}>NVIDIA</CompanyLink>))
    const link = screen.getByRole('link', { name: 'NVIDIA' })

    fireEvent.click(link, { ctrlKey: true })
    expect(scrollTo).not.toHaveBeenCalled()

    fireEvent.click(link)
    expect(scrollTo).toHaveBeenCalledWith({ top: 0 })
  })
})
