import * as React from 'react'
import { ArrowDown, ArrowUp, Info } from 'lucide-react'
import { cn } from '@/lib/utils'
import { useIsCompact } from '@/lib/useMediaQuery'
import { ScrollableTable } from './ScrollableTable'
import { SortableTh, type SortDirection } from './SortableTh'
import { Popover, PopoverContent, PopoverTrigger } from './popover'

/**
 * One table, described once, rendered as a table on a laptop and as a list of cards on
 * a phone.
 *
 * The app has ten of these, from four columns to eighteen. At 390px a card interior is
 * ~324px, so Watchlist's intrinsic 1,350px was roughly four screens of sideways
 * scrolling and Fundamentals' 750px was two — and because `ScrollableTable` applies no
 * minimum width, what actually happened was worse than scrolling: the browser squeezed
 * every column to its intrinsic minimum and the table became unreadable in place.
 *
 * The obvious fix is a hand-written mobile block per tab behind `hidden sm:block`.
 * That is thirteen tables times two renderings — twenty-six places a column can be
 * added to one and not the other, which is verbatim the failure mode CLAUDE.md opens
 * with. It is also worse than the backend instances it lists: a diverging growth
 * calculation produces a wrong number someone eventually notices, whereas a mobile card
 * missing a column produces nothing at all, on a device the author is not looking at.
 *
 * So both renderings come from one `Column[]`, `mobile` defaults to `'detail'` (a new
 * column reaches the phone unless someone explicitly hides it — the direction the
 * mistake should fall), and `DataTable.test.tsx` drives both modes from a single
 * fixture and asserts every non-hidden column appears in each.
 *
 * It deliberately does NOT own sort state. The three sorting tables have genuinely
 * bespoke comparators — nulls-last over a string|number union, a rating consensus
 * mapped through a score table, a memoised portfolio total — and all three already
 * memoise correctly. Pulling them in would need a `sortValue` per column, i.e. a fourth
 * thing to keep in step with `cell`. Rows arrive already sorted.
 */

export type Align = 'left' | 'right' | 'center'

/**
 * Where a column lands in the phone card.
 *
 *   title   the row's identity — exactly one per table (symbol, currency, country)
 *   meta    a muted line under the title; several stack in declaration order
 *   value   the headline figure, right-aligned on the title line
 *   delta   a smaller right-aligned figure under `value`, usually toned
 *   badge   a chip; several wrap onto one row beneath the title block
 *   detail  a label/value pair in the wrapped grid — THE DEFAULT, deliberately
 *   hide    not rendered on the phone
 */
export type MobileSlot = 'title' | 'meta' | 'value' | 'delta' | 'badge' | 'detail' | 'hide'

/**
 * Which rendering a `cell` is being asked for.
 *
 * It exists because the desktop tables stack a secondary line inside a cell — symbol
 * over exchange, description over ISIN — where the card wants those as separate lines
 * or detail pairs. One function still owns the column in both modes, so a column
 * cannot exist in one and not the other; only the *layout* differs.
 *
 * A `cell` must render the same information either way. Dropping a value in one view
 * is what `mobile: 'hide'` / `desktop: 'hide'` are for, and those are the forms the
 * tests can see.
 */
export type CellView = 'table' | 'cards'

export interface ColumnHint {
  description: string
  formula?: string
}

export interface Column<Row, SortKey extends string = never> {
  /** Stable identity for React keys, footer spans, and the tests that walk this array. */
  key: string
  header: React.ReactNode
  /**
   * Plain-text form of `header`. Required when the column is sortable, because the
   * phone's sort control is a native `<select>` and an `<option>` cannot hold JSX.
   */
  shortHeader?: string
  align?: Align
  cell: (row: Row, view: CellView) => React.ReactNode
  /**
   * Semantic colour, applied in BOTH modes. Layout belongs in `cellClassName`/`align`;
   * a green/red that lives only on the `<td>` is exactly the drift this split prevents
   * — gains and losses would render monochrome on the phone, which reads as a fact.
   */
  tone?: (row: Row) => string | undefined
  /** Desktop `<td>` only: truncation, max-widths, per-column padding. */
  cellClassName?: string
  /** Desktop `<th>` only. */
  headerClassName?: string
  /** Present ⇒ sortable. Handed straight back to the caller's own `onSort`. */
  sortKey?: SortKey
  /**
   * What the column means. Feeds the `<th>`'s native `title=` AND the tap-reachable
   * glossary, from one source — so a definition cannot exist on the desktop header and
   * be missing on the phone, which is where every `title=` in this app used to end.
   */
  hint?: ColumnHint
  mobile?: MobileSlot
  desktop?: 'show' | 'hide'
}

/**
 * The sort contract every table in this app already implements, written down because
 * nothing else states it: calling `onSort` with the ACTIVE column flips the direction;
 * calling it with a different one switches column and resets. The phone's direction
 * button relies on the first half.
 */
export interface SortState<SortKey extends string> {
  column: SortKey | null
  direction: SortDirection
  onSort: (column: SortKey) => void
}

export interface FooterCell {
  key: string
  /**
   * Column keys this cell covers. Drives `colSpan` on desktop — better than the literal
   * `colSpan={3}` the tax tables carry today, which lies silently once a column is
   * added. On the phone a cell spanning several columns is the footer row's heading and
   * a cell spanning one becomes a label/value pair.
   */
  spans: readonly string[]
  content: React.ReactNode
  /** Overrides the phone label otherwise derived from the spanned column. */
  label?: React.ReactNode
  align?: Align
  tone?: string
}

/**
 * Row padding, named after what the ten tables actually use rather than an invented
 * scale, so each conversion can keep its current desktop spacing exactly.
 */
export type Density = 'compact' | 'normal' | 'roomy' | 'comfortable'

const DENSITY: Record<Density, string> = {
  compact: 'px-2 py-1.5',
  normal: 'px-2 py-2',
  roomy: 'px-3 py-2',
  comfortable: 'px-2 py-3',
}

const ALIGN_CELL: Record<Align, string> = {
  left: 'text-left',
  right: 'text-right',
  center: 'text-center',
}

export interface DataTableProps<Row, SortKey extends string = never> {
  rows: readonly Row[]
  columns: readonly Column<Row, SortKey>[]
  getRowKey: (row: Row, index: number) => React.Key
  /** Names the scroll region, the card list and the glossary button. */
  label: string
  /** Screen-reader-only `<caption>`; mirrored as sr-only text above the card list. */
  caption?: React.ReactNode
  sort?: SortState<SortKey>
  /**
   * A designed width for the desktop table. `ScrollableTable` applies none and every
   * table is `w-full`, which is why eighteen columns got squeezed rather than scrolled.
   */
  minWidthClassName?: string
  footer?: readonly FooterCell[]
  rowActions?: (row: Row) => React.ReactNode
  /**
   * Content rendered directly BENEATH a row. Return `null`/`undefined` for a row that is
   * not expanded — the caller owns which one is, since it already owns the control in
   * `rowActions` that toggles it.
   *
   * Shaped like `rowActions` on purpose: one function, both renderings, so a breakdown
   * cannot reach the table and miss the phone. It exists because there was nowhere to put
   * one — the Look-through tab's company breakdown had to be rendered *outside* the table
   * entirely, which put it below every row plus the footnotes, one to three screens from
   * the button that opened it.
   */
  expandedRow?: (row: Row) => React.ReactNode
  /** Detail pairs shown before a "Show all N" disclosure. Omit to show all of them. */
  detailLimit?: number
  /** Wraps BOTH modes, so a caller's section divider survives on the phone. */
  className?: string
  density?: Density
  /**
   * Test seam. Consumers must not pass this: it exists so DataTable.test.tsx can render
   * the same descriptors both ways in one assertion, which is the anti-drift mechanism.
   */
  mode?: 'auto' | 'table' | 'cards'
}

function columnLabel<Row, SortKey extends string>(
  column: Column<Row, SortKey> | undefined,
): React.ReactNode {
  if (!column) return null
  return column.shortHeader ?? column.header
}

export function DataTable<Row, SortKey extends string = never>({
  rows,
  columns,
  getRowKey,
  label,
  caption,
  sort,
  minWidthClassName,
  footer,
  rowActions,
  expandedRow,
  detailLimit,
  className,
  density = 'normal',
  mode = 'auto',
}: DataTableProps<Row, SortKey>) {
  const isCompact = useIsCompact()
  const asCards = mode === 'cards' || (mode === 'auto' && isCompact)

  const hinted = columns.filter(c => c.hint)
  const glossary = hinted.length > 0 ? <Glossary label={label} columns={hinted} /> : null

  return (
    <div className={className}>
      {asCards ? (
        <>
          {/* Sort and glossary share one row: on their own lines they read as two
              unrelated controls with a band of dead space between them and the first
              card. */}
          {(sort || glossary) && (
            <div className="mb-3 flex items-center gap-2">
              {sort && <CompactSortControl label={label} columns={columns} sort={sort} />}
              {glossary}
            </div>
          )}
          {caption && <p className="sr-only">{caption}</p>}
          <ul aria-label={label} className="divide-y divide-border">
            {rows.map((row, index) => (
              <CompactRow
                key={getRowKey(row, index)}
                row={row}
                columns={columns}
                detailLimit={detailLimit}
                actions={rowActions?.(row)}
                expanded={expandedRow?.(row)}
              />
            ))}
            {footer && footer.length > 0 && (
              <CompactFooterRow footer={footer} columns={columns} />
            )}
          </ul>
        </>
      ) : (
        <>
          {glossary && <div className="mb-2 flex justify-end">{glossary}</div>}
          <WideTable
            rows={rows}
            columns={columns}
            getRowKey={getRowKey}
            label={label}
            caption={caption}
            sort={sort}
            minWidthClassName={minWidthClassName}
            footer={footer}
            rowActions={rowActions}
            expandedRow={expandedRow}
            density={density}
          />
        </>
      )}
    </div>
  )
}

/* -------------------------------------------------------------- desktop */

function WideTable<Row, SortKey extends string>({
  rows,
  columns,
  getRowKey,
  label,
  caption,
  sort,
  minWidthClassName,
  footer,
  rowActions,
  expandedRow,
  density,
}: Required<Pick<DataTableProps<Row, SortKey>, 'rows' | 'columns' | 'getRowKey' | 'label' | 'density'>> &
  Pick<
    DataTableProps<Row, SortKey>,
    'caption' | 'sort' | 'minWidthClassName' | 'footer' | 'rowActions' | 'expandedRow'
  >) {
  const visible = columns.filter(c => c.desktop !== 'hide')
  const pad = DENSITY[density]
  const columnCount = visible.length + (rowActions ? 1 : 0)

  return (
    <ScrollableTable label={label}>
      <table className={cn('w-full text-sm', minWidthClassName)}>
        {caption && <caption className="sr-only">{caption}</caption>}
        <thead>
          <tr className="border-b text-muted-foreground">
            {visible.map(col => {
              const align = col.align ?? 'left'
              if (col.sortKey && sort) {
                return (
                  <SortableTh
                    key={col.key}
                    column={col.sortKey}
                    label={col.header}
                    activeColumn={sort.column}
                    direction={sort.direction}
                    onSort={sort.onSort}
                    align={align}
                    title={col.hint?.description}
                    className={cn(pad, 'whitespace-nowrap', col.headerClassName)}
                    iconClassName="h-3 w-3"
                  />
                )
              }
              return (
                <th
                  key={col.key}
                  scope="col"
                  title={col.hint?.description}
                  className={cn(
                    'font-medium',
                    pad,
                    ALIGN_CELL[align],
                    col.headerClassName,
                  )}
                >
                  {col.header}
                </th>
              )
            })}
            {rowActions && <th scope="col" className={cn(pad, 'w-10')}><span className="sr-only">Actions</span></th>}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, index) => {
            const key = getRowKey(row, index)
            const expansion = expandedRow?.(row)
            return (
              <React.Fragment key={key}>
                {/* The border moves to the expansion when there is one, so the panel
                    reads as part of its row rather than as a detached band below a rule. */}
                <tr className={cn(!expansion && 'border-b last:border-0')}>
                  {visible.map(col => (
                    <td
                      key={col.key}
                      className={cn(
                        pad,
                        ALIGN_CELL[col.align ?? 'left'],
                        (col.align ?? 'left') === 'right' && 'tabular-nums',
                        col.tone?.(row),
                        col.cellClassName,
                      )}
                    >
                      {col.cell(row, 'table')}
                    </td>
                  ))}
                  {rowActions && <td className={pad}>{rowActions(row)}</td>}
                </tr>
                {expansion && (
                  <tr className="border-b last:border-0">
                    {/* `columnCount` rather than a literal: it already counts the
                        rowActions column, and a hardcoded span is what the footer code
                        below records as lying silently once a column is added. */}
                    <td colSpan={columnCount} className={cn(pad, 'bg-muted/30')}>
                      {expansion}
                    </td>
                  </tr>
                )}
              </React.Fragment>
            )
          })}
        </tbody>
        {footer && footer.length > 0 && (
          <tfoot>
            <tr className="border-t font-medium">
              {footer.map(cell => (
                <td
                  key={cell.key}
                  colSpan={cell.spans.length}
                  className={cn(
                    pad,
                    ALIGN_CELL[cell.align ?? 'left'],
                    (cell.align ?? 'left') === 'right' && 'tabular-nums',
                    cell.tone,
                  )}
                >
                  {cell.content}
                </td>
              ))}
              {/* Pads the row out to the full width when the spans do not cover every
                  column, which is what the tax tables' trailing empty `<td>` was. */}
              {footer.reduce((n, c) => n + c.spans.length, 0) < columnCount && (
                <td
                  className={pad}
                  colSpan={columnCount - footer.reduce((n, c) => n + c.spans.length, 0)}
                />
              )}
            </tr>
          </tfoot>
        )}
      </table>
    </ScrollableTable>
  )
}

/* --------------------------------------------------------------- phone */

function slotOf<Row, SortKey extends string>(col: Column<Row, SortKey>): MobileSlot {
  return col.mobile ?? 'detail'
}

function CompactRow<Row, SortKey extends string>({
  row,
  columns,
  detailLimit,
  actions,
  expanded: expansion,
}: {
  row: Row
  columns: readonly Column<Row, SortKey>[]
  detailLimit?: number
  actions?: React.ReactNode
  expanded?: React.ReactNode
}) {
  const [expanded, setExpanded] = React.useState(false)

  const pick = (slot: MobileSlot) => columns.filter(c => slotOf(c) === slot)
  const title = pick('title')[0]
  const value = pick('value')[0]
  const delta = pick('delta')[0]
  const metas = pick('meta')
  const badges = pick('badge')
  const details = pick('detail')

  const shownDetails =
    detailLimit != null && !expanded ? details.slice(0, detailLimit) : details
  const hiddenCount = details.length - shownDetails.length

  return (
    <li className="py-3">
      {/* The getquin row: identity left, headline figure right. `min-w-0` is what lets
          `truncate` bind on the title block, and `shrink-0` is what stops a long
          company name squeezing the figure to nothing — the same squeeze
          DividendYearComparison records for its bar at this width. */}
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0 flex-1">
          {title && (
            <div className="truncate font-medium leading-tight">{title.cell(row, 'cards')}</div>
          )}
          {metas.map(col => (
            <div key={col.key} className="truncate text-xs text-muted-foreground">
              {col.cell(row, 'cards')}
            </div>
          ))}
        </div>
        {(value || delta) && (
          <div className="shrink-0 text-right tabular-nums">
            {value && (
              <div className={cn('font-medium leading-tight', value.tone?.(row))}>
                {value.cell(row, 'cards')}
              </div>
            )}
            {delta && (
              <div className={cn('text-xs', delta.tone?.(row))}>{delta.cell(row, 'cards')}</div>
            )}
          </div>
        )}
      </div>

      {badges.length > 0 && (
        <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
          {badges.map(col => (
            <React.Fragment key={col.key}>{col.cell(row, 'cards')}</React.Fragment>
          ))}
        </div>
      )}

      {shownDetails.length > 0 && (
        <dl className="mt-2 grid grid-cols-2 gap-x-3 gap-y-1 text-xs">
          {shownDetails.map(col => (
            <div key={col.key} className="flex min-w-0 items-baseline justify-between gap-2">
              <dt className="truncate text-muted-foreground">{columnLabel(col)}</dt>
              <dd className={cn('shrink-0 tabular-nums', col.tone?.(row))}>
                {col.cell(row, 'cards')}
              </dd>
            </div>
          ))}
        </dl>
      )}

      {(hiddenCount > 0 || expanded) && detailLimit != null && details.length > detailLimit && (
        <button
          type="button"
          onClick={() => setExpanded(v => !v)}
          aria-expanded={expanded}
          className="mt-2 inline-flex h-9 items-center rounded-md px-2 text-xs font-medium text-muted-foreground ring-offset-background hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
        >
          {expanded ? 'Show less' : `Show all ${details.length} metrics`}
        </button>
      )}

      {actions && <div className="mt-2 flex justify-end gap-2">{actions}</div>}

      {/* Below the actions, so the control that opened it stays adjacent to its own card
          rather than being pushed away by the panel it toggles. */}
      {expansion && (
        <div className="mt-2 rounded-md bg-muted/30 px-3 py-2">{expansion}</div>
      )}
    </li>
  )
}

function CompactFooterRow<Row, SortKey extends string>({
  footer,
  columns,
}: {
  footer: readonly FooterCell[]
  columns: readonly Column<Row, SortKey>[]
}) {
  const byKey = new Map(columns.map(c => [c.key, c]))
  const heading = footer.filter(c => c.spans.length > 1)
  const pairs = footer.filter(c => c.spans.length <= 1)

  return (
    <li className="bg-muted/40 py-3 font-medium">
      {heading.map(cell => (
        <div key={cell.key} className={cn('text-sm', cell.tone)}>
          {cell.label ?? cell.content}
        </div>
      ))}
      <dl className={cn('grid grid-cols-2 gap-x-3 gap-y-1 text-xs', heading.length > 0 && 'mt-1.5')}>
        {pairs.map(cell => (
          <div key={cell.key} className="flex min-w-0 items-baseline justify-between gap-2">
            <dt className="truncate text-muted-foreground">
              {cell.label ?? columnLabel(byKey.get(cell.spans[0]))}
            </dt>
            <dd className={cn('shrink-0 tabular-nums', cell.tone)}>{cell.content}</dd>
          </div>
        ))}
      </dl>
    </li>
  )
}

function CompactSortControl<Row, SortKey extends string>({
  label,
  columns,
  sort,
}: {
  label: string
  columns: readonly Column<Row, SortKey>[]
  sort: SortState<SortKey>
}) {
  const id = React.useId()
  const sortable = columns.filter(c => c.sortKey)
  if (sortable.length === 0) return null

  return (
    /* There is no header row on a phone, so sorting needs its own control. A native
       `<select>` because every mobile OS renders it as a full-height sheet: a large
       target for free, no new primitive, and screen-reader semantics we do not have to
       invent. The button re-fires `onSort` with the ACTIVE column, which every table in
       this app already treats as a direction toggle. */
    <div className="flex min-w-0 flex-1 items-center gap-2">
      <label htmlFor={id} className="sr-only">
        Sort {label} by
      </label>
      <select
        id={id}
        value={sort.column ?? ''}
        onChange={e => sort.onSort(e.target.value as SortKey)}
        className="h-10 min-w-0 flex-1 rounded-md border border-input bg-background px-3 text-sm font-medium"
      >
        {sortable.map(col => (
          <option key={col.key} value={col.sortKey}>
            {col.shortHeader ?? (typeof col.header === 'string' ? col.header : col.key)}
          </option>
        ))}
      </select>
      <button
        type="button"
        onClick={() => sort.column && sort.onSort(sort.column)}
        aria-label={`Sort ${sort.direction === 'asc' ? 'descending' : 'ascending'}`}
        className="inline-flex h-10 w-10 shrink-0 items-center justify-center rounded-md border border-input bg-background ring-offset-background hover:bg-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
      >
        {sort.direction === 'asc' ? (
          <ArrowUp aria-hidden="true" className="h-4 w-4" />
        ) : (
          <ArrowDown aria-hidden="true" className="h-4 w-4" />
        )}
      </button>
    </div>
  )
}

function Glossary<Row, SortKey extends string>({
  label,
  columns,
}: {
  label: string
  columns: readonly Column<Row, SortKey>[]
}) {
  return (
    /* Rendered in both modes from the same `hint`s that feed the desktop `<th>`'s
       `title=`. Every metric definition in this app lived in a `title=` or a
       mouse-only tooltip, which no touch device can reach and one of which
       (Watchlist's Mkt Cap) was unreachable by keyboard too. Radix Popover opens on
       click, portals out of the scroll container and handles Escape and focus. */
    <Popover>
      <PopoverTrigger asChild>
        <button
          type="button"
          aria-label={`What the ${label} columns mean`}
          className="inline-flex h-9 w-9 items-center justify-center rounded-md text-muted-foreground ring-offset-background hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
        >
          <Info aria-hidden="true" className="h-4 w-4" />
        </button>
      </PopoverTrigger>
      <PopoverContent className="max-h-[70vh] w-72 overflow-y-auto" align="end">
        <dl className="space-y-2.5 text-xs">
          {columns.map(col => (
            <div key={col.key}>
              <dt className="font-medium text-foreground">{columnLabel(col)}</dt>
              <dd className="leading-relaxed text-muted-foreground">
                {col.hint!.description}
              </dd>
              {col.hint!.formula && (
                <dd className="mt-1 rounded-md bg-muted/50 px-2 py-1.5 font-mono text-[10px] text-muted-foreground/80">
                  {col.hint!.formula}
                </dd>
              )}
            </div>
          ))}
        </dl>
      </PopoverContent>
    </Popover>
  )
}
