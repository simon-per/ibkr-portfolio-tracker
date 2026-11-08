import { useQuery } from '@tanstack/react-query'
import { Gauge, PieChart, Scale, TrendingUp } from 'lucide-react'
import { Card, CardContent } from '@/components/ui/card'
import { CollapsibleCardHeader } from '@/components/ui/CollapsibleCardHeader'
import { KpiCard, KpiPanel } from '@/components/ui/KpiCard'
import { lookthroughFundamentalsQuery } from '@/lib/queryCache'
import { useRememberedOpen } from '@/lib/storage'
import { formatCount, formatFractionPercent, formatMultiple, formatSigned } from '@/lib/utils'
import type { Tone } from '@/lib/tone'

const signed = (v: number | null) => (v == null ? null : formatSigned(v, (n) => formatFractionPercent(n)))
const growthTone = (v: number | null): Tone =>
  v == null || Math.abs(v) < 0.0005 ? 'neutral' : v > 0 ? 'positive' : 'negative'

/**
 * The fundamentals of what the account owns, through its funds as well (review item R2):
 * share-equivalent P/E, growth and margins of the look-through companies with SEC figures,
 * and how much of the companies' value they cover. Collapsed; asks the server once opened.
 */
export function LookthroughFundamentalsCard() {
  const [open, setOpen] = useRememberedOpen('lookthrough-fundamentals')
  const { data, isError } = useQuery({ ...lookthroughFundamentalsQuery, enabled: open })

  return (
    <Card>
      <CollapsibleCardHeader
        open={open}
        onToggle={() => setOpen((o) => !o)}
        title="Fundamentals of what you own"
        icon={Gauge}
        contentId="lookthrough-fundamentals"
      />
      {open && (
        <CardContent id="lookthrough-fundamentals" data-testid="lookthrough-fundamentals">
          {isError ? (
            <p className="py-4 text-sm text-muted-foreground">Couldn't load the fundamentals.</p>
          ) : (
            <KpiPanel columns={3}>
              <KpiCard tile icon={Scale} label="P/E" value={data?.pe_ratio == null ? null : formatMultiple(data.pe_ratio)} />
              <KpiCard
                tile
                icon={TrendingUp}
                label="Revenue YoY"
                value={signed(data?.revenue_growth ?? null)}
                tone={growthTone(data?.revenue_growth ?? null)}
              />
              <KpiCard
                tile
                icon={TrendingUp}
                label="EPS YoY"
                value={signed(data?.earnings_growth ?? null)}
                tone={growthTone(data?.earnings_growth ?? null)}
              />
              <KpiCard
                tile
                icon={Gauge}
                label="Op margin"
                value={data?.operating_margin == null ? null : formatFractionPercent(data.operating_margin)}
              />
              <KpiCard
                tile
                icon={Gauge}
                label="FCF margin"
                value={data?.fcf_margin == null ? null : formatFractionPercent(data.fcf_margin)}
              />
              <KpiCard
                tile
                icon={PieChart}
                label="Coverage"
                value={data?.coverage_pct == null ? null : `${data.coverage_pct.toFixed(0)}%`}
                sub={data ? `${formatCount(data.companies_covered)} of ${formatCount(data.companies)} companies` : undefined}
              />
            </KpiPanel>
          )}
        </CardContent>
      )}
    </Card>
  )
}
