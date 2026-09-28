import { useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { Lock, LockOpen } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Popover, PopoverTrigger, PopoverContent } from '@/components/ui/popover'
import { getApiKey, setApiKey } from '@/lib/api'

interface AdminKeyButtonProps {
  /** From /health. Undefined while it loads. */
  writeAuthEnabled?: boolean
}

/**
 * Set or clear the admin key that authorises writes — and, for the crypto book, reads too
 * (its routes are private: docs/crypto.md).
 *
 * The API is a single-tenant deployment behind a public nginx, so the key is a shared
 * secret rather than a per-user credential — there is no login to hang it off, and
 * inventing one for an account with a single owner would be ceremony. It is stored per
 * browser and sent as `X-API-Key`.
 *
 * **Hidden entirely when the backend has no key configured** (`API_ADMIN_TOKEN` unset,
 * which is the default). Showing a lock that protects nothing would be worse than
 * showing none: it implies a control that is not actually in force.
 */
export function AdminKeyButton({ writeAuthEnabled }: AdminKeyButtonProps) {
  const queryClient = useQueryClient()
  const [open, setOpen] = useState(false)
  const [draft, setDraft] = useState('')
  const [saved, setSaved] = useState(() => Boolean(getApiKey()))

  if (!writeAuthEnabled) return null

  const save = () => {
    setApiKey(draft.trim())
    setSaved(Boolean(draft.trim()))
    setDraft('')
    setOpen(false)
    // Mutations that failed 401 are cached as errors; drop them so the next attempt
    // actually goes out rather than replaying the refusal.
    queryClient.invalidateQueries()
  }

  const clear = () => {
    setApiKey('')
    setSaved(false)
    setDraft('')
    setOpen(false)
    // Private figures must not outlive the key that unlocked them: without this the crypto
    // book stays on screen, from the cache, until something happens to refetch it.
    queryClient.removeQueries({ queryKey: ['crypto'] })
  }

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <Button
          variant="outline"
          size="icon"
          aria-label={saved ? 'Admin key saved — manage write access' : 'Add admin key to enable changes'}
          title={saved ? 'Write access unlocked on this browser' : 'Writes are locked — add the admin key'}
        >
          {saved ? <LockOpen className="h-4 w-4" /> : <Lock className="h-4 w-4" />}
        </Button>
      </PopoverTrigger>
      <PopoverContent className="w-80 space-y-3">
        <div>
          <p className="text-sm font-medium">Admin key</p>
          <p className="mt-1 text-xs text-muted-foreground">
            Required to sync, change the base currency, edit the watchlist, or view the
            crypto book. Reading the stock portfolio never needs it.
          </p>
        </div>
        <form
          onSubmit={e => {
            e.preventDefault()
            save()
          }}
          className="space-y-2"
        >
          <label htmlFor="admin-key-input" className="sr-only">
            Admin key
          </label>
          <input
            id="admin-key-input"
            type="password"
            autoComplete="off"
            value={draft}
            onChange={e => setDraft(e.target.value)}
            placeholder={saved ? 'Replace the saved key' : 'Paste the key'}
            className="h-9 w-full rounded-md border border-input bg-background px-3 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
          />
          <div className="flex gap-2">
            <Button type="submit" size="sm" disabled={!draft.trim()}>
              Save
            </Button>
            {saved && (
              <Button type="button" size="sm" variant="ghost" onClick={clear}>
                Remove
              </Button>
            )}
          </div>
        </form>
        <p className="text-[11px] leading-tight text-muted-foreground">
          Stored in this browser only, and sent to this API alone.
        </p>
      </PopoverContent>
    </Popover>
  )
}
