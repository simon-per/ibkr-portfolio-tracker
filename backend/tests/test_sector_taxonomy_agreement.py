"""
The canonical sector vocabulary is written down in two languages and must agree.

`app/services/sector_taxonomy.py` decides what a company's sector *is*; `frontend/src/lib/
sectorColors.ts` decides what it *looks like*, and does so by position — slot 1 is the first
canonical sector, and only the first four get a hue at all. Two copies of one ordered list, in
files that cannot import each other. Same shape as `test_range_limit_agreement.py` and
`test_deploy_guard_hours.py`, and here for the same reason: it drifts silently.

**The order is the part that matters, not just the membership.** Insert a sector at the front
of the Python tuple and every hue shifts one place: Technology turns yellow, Communications
turns magenta, and nothing anywhere fails. That is colour-follows-rank arriving by the back
door — the exact defect the fixed assignment exists to prevent — so equality of the *sequence*
is what is pinned, not set equality.

The client's `HUED_SECTORS` must also be exactly the first four, because four is the largest
set that clears the colourblind gate all-pairs and a fifth entry would silently ask for a
`--viz-sector-5` that `index.css` does not define.
"""

import re
from pathlib import Path

from app.services.sector_taxonomy import CANONICAL_SECTORS

REPO = Path(__file__).resolve().parents[2]
SECTOR_COLORS_TS = REPO / "frontend" / "src" / "lib" / "sectorColors.ts"
INDEX_CSS = REPO / "frontend" / "src" / "index.css"

# The client caps hued sectors at this many; `index.css` must define exactly this many slots.
HUED_SLOTS = 4


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


def test_the_client_hues_exactly_the_first_four_canonical_sectors():
    source = SECTOR_COLORS_TS.read_text(encoding="utf-8")
    hued = _ts_string_array(source, "HUED_SECTORS")

    assert hued == list(CANONICAL_SECTORS[:HUED_SLOTS]), (
        f"HUED_SECTORS must be the first {HUED_SLOTS} canonical sectors in order; got {hued}"
    )


def test_index_css_defines_a_slot_for_every_hued_sector_and_no_more():
    """
    A fifth `HUED_SECTORS` entry would resolve `var(--viz-sector-5)` to nothing and render an
    unpainted tile — invisible in a unit test, and invisible in jsdom, which loads no CSS.
    """
    css = INDEX_CSS.read_text(encoding="utf-8")
    slots = {int(n) for n in re.findall(r"--viz-sector-(\d+):", css)}
    inks = {int(n) for n in re.findall(r"--viz-sector-(\d+)-ink:", css)}

    assert slots == set(range(1, HUED_SLOTS + 1)), (
        f"index.css defines sector slots {sorted(slots)}, expected 1..{HUED_SLOTS}"
    )
    assert inks == slots, (
        f"every sector slot needs its own label ink; fills {sorted(slots)} vs inks {sorted(inks)}"
    )
