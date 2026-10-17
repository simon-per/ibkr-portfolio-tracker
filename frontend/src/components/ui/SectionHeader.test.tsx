// @vitest-environment jsdom
import { afterEach, describe, expect, it } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import { ChartPie } from 'lucide-react'
import { SectionHeader } from './SectionHeader'
import { CollapsibleCardHeader } from './CollapsibleCardHeader'

afterEach(cleanup)

describe('SectionHeader', () => {
  it('renders the title as the heading, with the icon decorative beside it', () => {
    render(<SectionHeader icon={ChartPie} title="Sectors" />)
    expect(screen.getByRole('heading', { name: 'Sectors' })).toBeTruthy()
    const well = document.querySelector('span[aria-hidden="true"]')
    expect(well?.querySelector('svg')).toBeTruthy()
    expect(well?.className).toContain('bg-info-soft')
  })

  it('places the aside on the right and the description under the title', () => {
    render(
      <SectionHeader
        title="Dividends"
        description="Received this year"
        aside={<span data-testid="aside">CHF 102.90</span>}
      />,
    )
    expect(screen.getByText('Received this year')).toBeTruthy()
    const aside = screen.getByTestId('aside').parentElement
    expect(aside?.className).toContain('justify-end')
  })

  it('pads like a card header by default and not at all when bare', () => {
    const { container, unmount } = render(<SectionHeader title="A" />)
    expect((container.firstElementChild as HTMLElement).className).toContain('p-[var(--card-padding)]')
    unmount()
    const bare = render(<SectionHeader title="A" variant="bare" />)
    expect((bare.container.firstElementChild as HTMLElement).className).not.toContain('p-[var(--card-padding)]')
  })

  it('draws no well, description or aside it was not given', () => {
    const { container } = render(<SectionHeader title="Plain" />)
    expect(container.querySelector('svg')).toBeNull()
    expect(container.querySelector('p')).toBeNull()
  })
})

describe('CollapsibleCardHeader with an icon and a figure', () => {
  it('keeps the toggle a button named by its title, with the icon hidden from it', () => {
    render(
      <CollapsibleCardHeader
        open={false}
        onToggle={() => {}}
        title="Currency exposure"
        icon={ChartPie}
        aside="93%"
        contentId="x"
      />,
    )
    const button = screen.getByRole('button', { expanded: false })
    expect(button.textContent).toContain('Currency exposure')
    expect(button.textContent).toContain('93%')
    expect(button.querySelector('span[aria-hidden="true"] svg')).toBeTruthy()
  })
})
