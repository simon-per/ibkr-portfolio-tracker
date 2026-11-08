/**
 * Round levels of the next-12-month forecast (review item FD3, 2026-11-08): the ones it
 * has crossed since the day growth is measured from, and the next one with how far away
 * it is. The forecast card marks each crossing on its line; the KPI tile prints the next.
 *
 * A level is a multiple of one step, picked by the figure's magnitude so there are three
 * to six of them below it: 1, 2 or 5 times a power of ten, the largest that is at most a
 * third of the figure (100 for 375, 500 for 1,500, 20 for 60). No date is projected for
 * the next one — that would be a forecast of a forecast.
 */

export interface MilestonePoint {
  date: string
  value: number
}

export interface MilestoneCrossing {
  level: number
  /** The first day the forecast stood at the level or above it. */
  date: string
  /** The forecast that day, where the mark sits on the line. */
  value: number
}

export interface Milestones {
  step: number
  /** Levels above the first day's figure that the line has reached, oldest first. */
  crossed: MilestoneCrossing[]
  /** The first level above the latest figure. */
  next: number
  toGo: number
}

const LADDER = [1, 2, 5]

/** The step for a figure of this size; null for one that is not positive. */
export function milestoneStep(value: number): number | null {
  if (!(value > 0) || !Number.isFinite(value)) return null
  const target = value / 3
  let power = 10 ** Math.floor(Math.log10(target))
  // log10 of an exact power of ten can land a hair under the integer.
  if (power * 10 <= target) power *= 10
  return Math.max(...LADDER.map((m) => m * power).filter((level) => level <= target))
}

/** Null without two days to compare or with a latest figure that is not positive. */
export function forecastMilestones(points: readonly MilestonePoint[]): Milestones | null {
  if (points.length < 2) return null
  const latest = points[points.length - 1].value
  const step = milestoneStep(latest)
  if (step == null) return null

  // Levels in units of the step, so float multiples cannot drift (0.1 x 3).
  const first = Math.floor(points[0].value / step + 1e-9) + 1
  const crossed: MilestoneCrossing[] = []
  let k = first
  for (const p of points) {
    while (p.value + 1e-9 >= k * step) {
      crossed.push({ level: k * step, date: p.date, value: p.value })
      k += 1
    }
  }
  const next = (Math.floor(latest / step + 1e-9) + 1) * step
  return { step, crossed, next, toGo: next - latest }
}
