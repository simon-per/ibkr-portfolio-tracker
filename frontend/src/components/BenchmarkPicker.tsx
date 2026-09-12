import { useQuery } from '@tanstack/react-query'
import { api } from '@/lib/api'
import { Popover, PopoverTrigger, PopoverContent } from '@/components/ui/popover'
import { Plus } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { benchmarkColor } from '@/lib/benchmarkColors'

const MAX_BENCHMARKS = 3

interface BenchmarkPickerProps {
  selected: string[]
  onChange: (keys: string[]) => void
}

export function BenchmarkPicker({ selected, onChange }: BenchmarkPickerProps) {
  const { data: benchmarks } = useQuery({
    queryKey: ['portfolio', 'benchmarks'],
    queryFn: () => api.getAvailableBenchmarks(),
    staleTime: Infinity,
  })

  const toggle = (key: string) => {
    if (selected.includes(key)) {
      onChange(selected.filter(k => k !== key))
    } else if (selected.length < MAX_BENCHMARKS) {
      onChange([...selected, key])
    }
  }

  return (
    <Popover>
      <PopoverTrigger asChild>
        <Button variant="outline" size="sm" className="h-8 w-8 p-0">
          <Plus className="h-4 w-4" />
          <span className="sr-only">Add benchmark</span>
        </Button>
      </PopoverTrigger>
      <PopoverContent className="w-56 p-2">
        <div className="text-xs font-medium text-muted-foreground px-2 py-1.5">
          Compare with (max {MAX_BENCHMARKS})
        </div>
        {benchmarks?.map((b) => {
          const isSelected = selected.includes(b.key)
          // By the benchmark's place in the full list, not in the selection — the same
          // colour the chart draws it in, and one that survives deselecting a neighbour.
          const color = benchmarkColor(b.key, benchmarks)
          const disabled = !isSelected && selected.length >= MAX_BENCHMARKS

          return (
            <button
              key={b.key}
              onClick={() => toggle(b.key)}
              disabled={disabled}
              className={`w-full flex items-center gap-2 text-sm px-2 py-1.5 rounded-md transition-colors text-left ${
                disabled
                  ? 'text-muted-foreground/50 cursor-not-allowed'
                  : 'hover:bg-muted/50'
              }`}
            >
              <div
                className="w-3 h-3 rounded-sm border flex-shrink-0"
                style={{
                  backgroundColor: isSelected ? color : 'transparent',
                  borderColor: isSelected ? color : 'hsl(var(--border))',
                }}
              />
              <span className="flex-1">{b.name}</span>
              <span className="text-xs text-muted-foreground">{b.currency}</span>
            </button>
          )
        })}
      </PopoverContent>
    </Popover>
  )
}
