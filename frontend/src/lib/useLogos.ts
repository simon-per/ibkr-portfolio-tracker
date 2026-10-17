import { useQuery } from '@tanstack/react-query'
import { api } from './api'

const NONE: Record<string, string> = {}

/**
 * Which securities have a stored logo, and its version (docs/logos.md): `security_id` ->
 * version. One small request shared by every avatar on the page, so the browser never asks
 * for a logo that does not exist; the logos change at most daily, hence the hour.
 */
export function useLogos(): Record<string, string> {
  const { data } = useQuery({
    queryKey: ['logos'],
    queryFn: () => api.getLogos(),
    staleTime: 60 * 60 * 1000,
  })
  return data?.logos ?? NONE
}
