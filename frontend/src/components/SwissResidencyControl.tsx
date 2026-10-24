import { useState, type FormEvent } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { api } from '@/lib/api'
import { formatDate } from '@/lib/utils'
import { Button } from '@/components/ui/button'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'

interface SwissResidencyControlProps {
  /** ISO date the owner became a Swiss taxpayer; null when resident throughout. */
  from: string | null
}

/**
 * The first day of Swiss tax residence. The tax report counts nothing before it — the
 * income and sales of the previous country's period belong to that country's return.
 */
export function SwissResidencyControl({ from }: SwissResidencyControlProps) {
  const queryClient = useQueryClient()
  const [open, setOpen] = useState(false)
  const [draft, setDraft] = useState(from ?? '')

  const mutation = useMutation({
    mutationFn: (next: string | null) => api.updateSwissResidency(next),
    onSuccess: async (updated) => {
      queryClient.setQueryData(['settings'], updated)
      await queryClient.invalidateQueries({ queryKey: ['tax'] })
      setOpen(false)
    },
  })

  const valid = /^\d{4}-\d{2}-\d{2}$/.test(draft) && !Number.isNaN(Date.parse(draft))
  const changed = valid && draft !== from

  const onOpenChange = (next: boolean) => {
    setOpen(next)
    if (next) {
      setDraft(from ?? '')
      mutation.reset()
    }
  }

  const save = (event: FormEvent) => {
    event.preventDefault()
    if (valid && changed) mutation.mutate(draft)
  }

  return (
    <Popover open={open} onOpenChange={onOpenChange}>
      <PopoverTrigger asChild>
        <Button
          type="button"
          variant="outline"
          size="sm"
          aria-label="Edit Swiss residency date"
          title="Swiss tax residence from"
        >
          {from ? `Swiss since ${formatDate(from)}` : 'Swiss residency'}
        </Button>
      </PopoverTrigger>
      <PopoverContent className="w-80 space-y-3" align="end">
        <div className="text-sm font-medium">Swiss tax residence</div>
        <form className="space-y-3" onSubmit={save}>
          <label className="block text-xs font-medium" htmlFor="swiss-residency-from">
            First day as a Swiss taxpayer
          </label>
          <input
            id="swiss-residency-from"
            type="date"
            value={draft}
            max={new Date().toISOString().slice(0, 10)}
            onChange={(event) => setDraft(event.target.value)}
            aria-invalid={draft !== '' && !valid}
            className="h-9 w-full rounded-lg border border-input bg-card px-3 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          />
          {mutation.isError && (
            <p className="text-xs text-destructive" role="alert">
              {mutation.error instanceof Error
                ? mutation.error.message
                : 'Could not save the residency date.'}
            </p>
          )}
          <div className="flex justify-between">
            <Button
              type="button"
              variant="ghost"
              size="sm"
              disabled={from === null || mutation.isPending}
              onClick={() => mutation.mutate(null)}
            >
              Clear
            </Button>
            <Button type="submit" size="sm" disabled={!changed || mutation.isPending}>
              {mutation.isPending ? 'Saving…' : 'Save'}
            </Button>
          </div>
        </form>
      </PopoverContent>
    </Popover>
  )
}
