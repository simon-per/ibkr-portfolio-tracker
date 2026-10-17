import { useContext } from 'react'
import { QueryClientContext } from '@tanstack/react-query'
import { TickerAvatar } from '@/components/ui/TickerAvatar'
import { useLogos } from '@/lib/useLogos'
import { avatarLabel, logoIsin } from '@/lib/lookthroughIdentity'

interface ConstituentAvatarProps {
  /** Every ISIN folded into the company; the first with a stored logo is shown. */
  isins: readonly string[]
  listings: readonly string[]
  name: string
  className?: string
}

function WithLogos({ isins, listings, name, className }: ConstituentAvatarProps) {
  const { isins: stored } = useLogos()
  return <TickerAvatar symbol={avatarLabel(listings, name)} isin={logoIsin(isins, stored)} className={className} />
}

/**
 * A look-through company's avatar: `TickerAvatar` by ISIN, since a constituent held only
 * through a fund has no security of its own. Outside a QueryClientProvider (a table's own
 * unit test) it is the monogram, as `TickerAvatar` itself is — decoration never breaks a table.
 */
export function ConstituentAvatar(props: ConstituentAvatarProps) {
  const hasQueryClient = useContext(QueryClientContext) !== undefined
  if (!hasQueryClient) {
    return <TickerAvatar symbol={avatarLabel(props.listings, props.name)} className={props.className} />
  }
  return <WithLogos {...props} />
}
