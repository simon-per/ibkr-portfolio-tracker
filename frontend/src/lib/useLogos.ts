import { useQuery } from '@tanstack/react-query'
import { api, type LogoManifest } from './api'

const NONE: LogoManifest = { logos: {}, isins: {} }

/**
 * Which logos are stored, and their versions (docs/logos.md): by `security_id`, and by ISIN
 * for rows that carry no security. One small request shared by every avatar on the page, so
 * the browser never asks for a logo that does not exist; the logos change at most daily,
 * hence the hour.
 */
export function useLogos(): LogoManifest {
  const { data } = useQuery({
    queryKey: ['logos'],
    queryFn: () => api.getLogos(),
    staleTime: 60 * 60 * 1000,
  })
  return data ?? NONE
}
