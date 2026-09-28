import { Bitcoin, ChartCandlestick } from 'lucide-react'
import { SegmentedControl } from '@/components/ui/SegmentedControl'
import { useMode, type PortfolioMode } from '@/lib/portfolioMode'

/**
 * Stocks | Crypto, beside the theme toggle. Swaps the whole page under the header; the
 * two books share no figure (see `lib/portfolioMode.tsx`).
 *
 * Labels hide below `sm` the way the Sync button's does — the icons carry the meaning on
 * a phone, and the accessible names carry it for a screen reader at every width. Sized to
 * the header's 36px controls: 30px segments inside the 2px padding and 1px border.
 */
export function ModeToggle() {
  const { mode, setMode } = useMode()
  return (
    <SegmentedControl<PortfolioMode>
      label="Portfolio"
      value={mode}
      onChange={setMode}
      className="h-9 items-center"
      buttonClassName="inline-flex h-[30px] items-center px-2.5 sm:px-3"
      options={[
        {
          value: 'stocks',
          ariaLabel: 'Stocks',
          title: 'Stocks — the IBKR and pillar 3a portfolio',
          label: (
            <>
              <ChartCandlestick className="h-4 w-4 sm:mr-1.5" aria-hidden="true" />
              <span className="hidden sm:inline">Stocks</span>
            </>
          ),
        },
        {
          value: 'crypto',
          ariaLabel: 'Crypto',
          title: 'Crypto — the CoinStats portfolio, kept apart from the stocks',
          label: (
            <>
              <Bitcoin className="h-4 w-4 sm:mr-1.5" aria-hidden="true" />
              <span className="hidden sm:inline">Crypto</span>
            </>
          ),
        },
      ]}
    />
  )
}
