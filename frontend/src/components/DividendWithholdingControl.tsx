import { useState, type FormEvent } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { api } from '@/lib/api'
import { formatDividendWithholdingPct } from '@/lib/dividendWithholding'
import { Button } from '@/components/ui/button'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'

interface DividendWithholdingControlProps {
  appliedPct: number
}

export function DividendWithholdingControl({ appliedPct }: DividendWithholdingControlProps) {
  const queryClient = useQueryClient()
  const [open, setOpen] = useState(false)
  const [draft, setDraft] = useState(() => formatDividendWithholdingPct(appliedPct))

  const mutation = useMutation({
    mutationFn: (withholdingPct: number) => api.updateDividendWithholding(withholdingPct),
    onSuccess: async (updated) => {
      queryClient.setQueryData(['settings'], updated)
      await queryClient.invalidateQueries({ queryKey: ['dividends', 'breakdown'] })
      setOpen(false)
    },
  })

  const parsed = Number(draft)
  const formatValid = /^(?:\d+|\d*\.\d{1,3})$/.test(draft.trim())
  const valid = formatValid && Number.isFinite(parsed) && parsed >= 0 && parsed <= 100
  const changed = valid && Math.abs(parsed - appliedPct) > 1e-9

  const onOpenChange = (next: boolean) => {
    setOpen(next)
    if (next) {
      setDraft(formatDividendWithholdingPct(appliedPct))
      mutation.reset()
    }
  }

  const save = (event: FormEvent) => {
    event.preventDefault()
    if (valid && changed) mutation.mutate(parsed)
  }

  return (
    <Popover open={open} onOpenChange={onOpenChange}>
      <PopoverTrigger asChild>
        <Button
          type="button"
          variant="outline"
          size="sm"
          aria-label="Edit forecast withholding"
          title="Withholding assumed for forecasts derived from gross dividends"
        >
          WHT {formatDividendWithholdingPct(appliedPct)}%
        </Button>
      </PopoverTrigger>
      <PopoverContent className="w-80 space-y-3" align="end">
        <div>
          <div className="text-sm font-medium">Forecast withholding</div>
          <p className="mt-1 text-xs text-muted-foreground">
            Applied only when a forecast comes from published gross dividends. Actual
            IBKR net payments and realized history never change.
          </p>
        </div>
        <form className="space-y-3" onSubmit={save}>
          <label className="block text-xs font-medium" htmlFor="dividend-withholding-pct">
            Withholding percentage
          </label>
          <div className="relative">
            <input
              id="dividend-withholding-pct"
              type="number"
              min="0"
              max="100"
              step="any"
              inputMode="decimal"
              value={draft}
              onChange={(event) => setDraft(event.target.value)}
              aria-invalid={!valid}
              aria-describedby="dividend-withholding-help"
              className="h-9 w-full rounded-md border border-input bg-background px-3 pr-8 text-sm tabular-nums focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            />
            <span className="pointer-events-none absolute inset-y-0 right-3 flex items-center text-sm text-muted-foreground">
              %
            </span>
          </div>
          <p id="dividend-withholding-help" className="text-xs text-muted-foreground">
            Enter 0% to 100%, with up to three decimal places. The current default is 15%.
          </p>
          {!valid && (
            <p className="text-xs text-destructive" role="alert">
              Enter 0 to 100 with up to three decimal places.
            </p>
          )}
          {mutation.isError && (
            <p className="text-xs text-destructive" role="alert">
              {mutation.error instanceof Error
                ? mutation.error.message
                : 'Could not save the withholding assumption.'}
            </p>
          )}
          <div className="flex justify-end">
            <Button type="submit" size="sm" disabled={!valid || !changed || mutation.isPending}>
              {mutation.isPending ? 'Saving…' : 'Save'}
            </Button>
          </div>
        </form>
      </PopoverContent>
    </Popover>
  )
}
