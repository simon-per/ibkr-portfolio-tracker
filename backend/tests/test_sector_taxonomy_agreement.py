"""
The canonical sector vocabulary is written down in two languages and must agree.

`app/services/sector_taxonomy.py` decides what a company's sector *is*; `frontend/src/lib/
sectorColors.ts` decides what it *looks like*, and does so by position — slot 1 is the first
canonical sector, through the app's categorical palette (`lib/chartPalette.ts`), and a sector
past the palette's last slot folds into Other. Two copies of one ordered list, in
files that cannot import each other. Same shape as `test_range_limit_agreement.py` and
`test_deploy_guard_hours.py`, and here for the same reason: it drifts silently.

**The order is the part that matters, not just the membership.** Insert a sector at the front
of the Python tuple and every hue shifts one place: Technology turns yellow, Communications
turns magenta, and nothing anywhere fails. That is colour-follows-rank arriving by the back
door — the exact defect the fixed assignment exists to prevent — so equality of the *sequence*
is what is pinned, not set equality.

Since 2026-10-17 the treemap and the Allocation donuts share that palette (docs/lookthrough.md
has the trade-off against the old four-hue set), so what must also hold is that `index.css`
defines every slot `CHART_SLOTS` promises, in both themes.
"""

import re
from pathlib import Path

from app.services.sector_taxonomy import CANONICAL_SECTORS

REPO = Path(__file__).resolve().parents[2]
SECTOR_COLORS_TS = REPO / "frontend" / "src" / "lib" / "sectorColors.ts"
CHART_PALETTE_TS = REPO / "frontend" / "src" / "lib" / "chartPalette.ts"
INDEX_CSS = REPO / "frontend" / "src" / "index.css"


def _ts_string_array(source: str, name: str) -> list:
    """The string literals of `export const <name> = [ ... ] as const`, in order."""
    match = re.search(
        rf"export const {name} = \[(.*?)\] as const", source, re.DOTALL
    )
    assert match, f"{name} not found in sectorColors.ts — was it renamed?"
    return re.findall(r"'([^']+)'", match.group(1))


def test_the_canonical_sector_order_matches_across_the_language_boundary():
    source = SECTOR_COLORS_TS.read_text(encoding="utf-8")
    client = _ts_string_array(source, "CANONICAL_SECTORS")

    assert client == list(CANONICAL_SECTORS), (
        "sectorColors.ts and sector_taxonomy.py disagree about the canonical sectors or their "
        f"order.\n  python: {list(CANONICAL_SECTORS)}\n  client: {client}\n"
        "Order is load-bearing: the client assigns hues by position, so a reordering silently "
        "recolours every chart."
    )


def test_index_css_defines_every_palette_slot_in_both_themes():
    """
    A sector in slot N is painted `var(--chart-N)`; a slot `index.css` does not define renders
    an unpainted tile — invisible in a unit test, and in jsdom, which loads no CSS.
    """
    match = re.search(r"export const CHART_SLOTS = (\d+)", CHART_PALETTE_TS.read_text(encoding="utf-8"))
    assert match, "CHART_SLOTS not found in chartPalette.ts — was it renamed?"
    slots = int(match.group(1))
    css = INDEX_CSS.read_text(encoding="utf-8")
    for slot in range(1, slots + 1):
        assert len(re.findall(rf"--chart-{slot}:", css)) == 2, (
            f"--chart-{slot} must be defined once per theme in index.css"
        )
    assert not re.search(rf"--chart-{slots + 1}:", css), "index.css defines more slots than CHART_SLOTS"
