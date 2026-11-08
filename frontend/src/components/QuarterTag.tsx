/**
 * The short tag on a growth figure that compares the latest quarter with the same one a
 * year before rather than the trailing twelve months (review item Y1): Yahoo's, where no
 * SEC filing is stored. A tag on the figure, like `est.`, never a footnote.
 */
export function QuarterTag() {
  return <span className="ml-1 text-[10px] font-normal text-muted-foreground">q</span>
}
