import { useEffect, useState } from 'react'

/**
 * True once the page has scrolled past `threshold` px: the sticky section strip lifts off
 * the content (a shadow) only when there is content under it to lift off.
 *
 * A passive listener plus a state that only changes when the answer does, so scrolling
 * re-renders the strip twice per crossing rather than once per frame.
 */
export function useScrolled(threshold = 8): boolean {
  const [scrolled, setScrolled] = useState(false)
  useEffect(() => {
    if (typeof window === 'undefined') return
    const update = () => setScrolled(window.scrollY > threshold)
    update()
    window.addEventListener('scroll', update, { passive: true })
    return () => window.removeEventListener('scroll', update)
  }, [threshold])
  return scrolled
}
