// @vitest-environment jsdom
import { describe, it, expect, afterEach } from 'vitest'
import { render, screen, cleanup } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { Wallet } from 'lucide-react'
import { Tabs, TabsList, TabsTrigger, TabsContent } from './tabs'

/**
 * The tab strip had no ARIA at all and no arrow-key navigation, so a keyboard user had
 * to Tab through every trigger to reach the last one and assistive technology saw
 * unrelated buttons above an unrelated div. These pin the WAI-ARIA tabs pattern.
 */

afterEach(cleanup)

function Example() {
  return (
    <Tabs defaultValue="one">
      <TabsList label="Sections">
        <TabsTrigger value="one">One</TabsTrigger>
        <TabsTrigger value="two">Two</TabsTrigger>
        <TabsTrigger value="three">Three</TabsTrigger>
      </TabsList>
      <TabsContent value="one">Panel one</TabsContent>
      <TabsContent value="two">Panel two</TabsContent>
      <TabsContent value="three">Panel three</TabsContent>
    </Tabs>
  )
}

describe('Tabs semantics', () => {
  it('exposes a named tablist of tabs', () => {
    render(<Example />)
    expect(screen.getByRole('tablist', { name: 'Sections' })).toBeTruthy()
    expect(screen.getAllByRole('tab')).toHaveLength(3)
  })

  it('marks exactly one tab selected', () => {
    render(<Example />)
    const selected = screen.getAllByRole('tab').filter(
      t => t.getAttribute('aria-selected') === 'true'
    )
    expect(selected).toHaveLength(1)
    expect(selected[0].textContent).toBe('One')
  })

  it('ties each tab to the panel it controls', () => {
    render(<Example />)
    const tab = screen.getByRole('tab', { name: 'One' })
    const panel = screen.getByRole('tabpanel')

    expect(tab.getAttribute('aria-controls')).toBe(panel.id)
    expect(panel.getAttribute('aria-labelledby')).toBe(tab.id)
  })

  it('renders only the selected panel', () => {
    render(<Example />)
    expect(screen.getAllByRole('tabpanel')).toHaveLength(1)
    expect(screen.getByRole('tabpanel').textContent).toBe('Panel one')
  })

  it('gives two instances on one page distinct ids', () => {
    render(<><Example /><Example /></>)
    const ids = screen.getAllByRole('tab').map(t => t.id)
    expect(new Set(ids).size).toBe(ids.length)
  })
})

describe('Tabs keyboard operation', () => {
  it('puts the list on a single tab stop', () => {
    render(<Example />)
    const [one, two, three] = screen.getAllByRole('tab')

    // Roving tabIndex: only the selected trigger is reachable by Tab, and the
    // arrow keys move within the list.
    expect(one.tabIndex).toBe(0)
    expect(two.tabIndex).toBe(-1)
    expect(three.tabIndex).toBe(-1)
  })

  it('moves selection and focus with the arrow keys', async () => {
    const user = userEvent.setup()
    render(<Example />)
    const tabs = screen.getAllByRole('tab')

    tabs[0].focus()
    await user.keyboard('{ArrowRight}')

    expect(tabs[1].getAttribute('aria-selected')).toBe('true')
    // Focus has to follow, or the roving tabIndex strands the user on a -1 element.
    expect(document.activeElement).toBe(tabs[1])
    expect(screen.getByRole('tabpanel').textContent).toBe('Panel two')

    await user.keyboard('{ArrowLeft}')
    expect(tabs[0].getAttribute('aria-selected')).toBe('true')
  })

  it('wraps at both ends', async () => {
    const user = userEvent.setup()
    render(<Example />)
    const tabs = screen.getAllByRole('tab')

    tabs[0].focus()
    await user.keyboard('{ArrowLeft}')
    expect(tabs[2].getAttribute('aria-selected')).toBe('true')

    await user.keyboard('{ArrowRight}')
    expect(tabs[0].getAttribute('aria-selected')).toBe('true')
  })

  it('jumps to the ends with Home and End', async () => {
    const user = userEvent.setup()
    render(<Example />)
    const tabs = screen.getAllByRole('tab')

    tabs[0].focus()
    await user.keyboard('{End}')
    expect(tabs[2].getAttribute('aria-selected')).toBe('true')

    await user.keyboard('{Home}')
    expect(tabs[0].getAttribute('aria-selected')).toBe('true')
  })

  it('leaves other keys to the browser', async () => {
    const user = userEvent.setup()
    render(<Example />)
    const tabs = screen.getAllByRole('tab')

    tabs[0].focus()
    await user.keyboard('{ArrowDown}')
    // Horizontal orientation: vertical arrows must not steal the page scroll.
    expect(tabs[0].getAttribute('aria-selected')).toBe('true')
  })

  it('still selects on click', async () => {
    const user = userEvent.setup()
    render(<Example />)

    await user.click(screen.getByRole('tab', { name: 'Three' }))
    expect(screen.getByRole('tabpanel').textContent).toBe('Panel three')
  })
})

describe('a tab with an icon', () => {
  it('keeps the label as the name and the icon out of it', () => {
    render(
      <Tabs defaultValue="one">
        <TabsList>
          <TabsTrigger value="one" icon={Wallet}>One</TabsTrigger>
        </TabsList>
      </Tabs>,
    )
    const tab = screen.getByRole('tab', { name: 'One' })
    expect(tab.textContent).toBe('One')
    expect(tab.querySelector('svg')?.getAttribute('aria-hidden')).toBe('true')
  })
})
