"""
Every API client paces and sends through `app/services/paced_http.PacedGet`.

`CoinStatsClient` and `CoinGeckoClient` carried the same `_pace` and the same transport
half of `_get`, line for line; the logo.dev client is the third user. The family test: no
module under `app/` defines its own `_pace` again, and every client takes the shared one.

A `_pace` is only half of the pacing. The other half is the stamp — `_last_request_at`
set when a request *finished* — and `LogoDevClient.download` carried its own copy of it
(and of the `await self._pace()` before it) until GitHub issue #44, unseen by a test that
only looked for a second `def _pace`. So the stamp is a family too: nothing outside
`paced_http.py` assigns `_last_request_at`, apart from a constructor initialising it to
`None`.
"""
import ast
from pathlib import Path

from app.services.coingecko_client import CoinGeckoClient
from app.services.coinstats_client import CoinStatsClient
from app.services.identity_service import OpenFigiClient
from app.services.logo_service import LogoDevClient
from app.services.paced_http import PacedGet

APP_DIR = Path(__file__).resolve().parents[1] / "app"


def test_no_module_paces_requests_by_its_own_copy():
    offenders = [
        f"{path.relative_to(APP_DIR)}:{node.lineno}"
        for path in sorted(APP_DIR.rglob("*.py"))
        if path.name != "paced_http.py"
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "_pace"
    ]
    assert offenders == [], f"use paced_http.PacedGet: {offenders}"


def _hand_stamps(tree: ast.AST) -> list:
    """
    Line numbers of every assignment to an attribute `_last_request_at`, except a
    constructor initialising it to `None` — the client's own state, not a copy.
    """
    constructor_inits = {
        id(node)
        for fn in ast.walk(tree)
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) and fn.name == "__init__"
        for node in ast.walk(fn)
        if isinstance(node, (ast.Assign, ast.AnnAssign))
        and isinstance(node.value, ast.Constant) and node.value.value is None
    }
    lines = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            targets = [node.target]
        else:
            continue
        if id(node) in constructor_inits:
            continue
        if any(isinstance(sub, ast.Attribute) and sub.attr == "_last_request_at"
               for target in targets for sub in ast.walk(target)):
            lines.append(node.lineno)
    return sorted(lines)


def test_no_module_stamps_a_request_by_its_own_copy():
    """The stamp half of the pace: one `_last_request_at = self._clock()`, in `_paced`."""
    offenders = [
        f"{path.relative_to(APP_DIR)}:{line}"
        for path in sorted(APP_DIR.rglob("*.py"))
        if path.name != "paced_http.py"
        for line in _hand_stamps(ast.parse(path.read_text(encoding="utf-8")))
    ]
    assert offenders == [], f"stamp through paced_http.PacedGet._paced: {offenders}"


def test_the_stamp_walk_flags_a_stamp_and_spares_a_constructor():
    """A walk that matches nothing passes vacuously; it must flag the shape it hunts."""
    source = (
        "class C:\n"
        "    def __init__(self):\n"
        "        self._last_request_at = None\n"
        "    async def download(self):\n"
        "        self._last_request_at = self._clock()\n"
        "    def reset(self):\n"
        "        self._last_request_at: float = 0.0\n"
    )
    assert _hand_stamps(ast.parse(source)) == [5, 7]
    # And the shared module itself is exactly what the walk would flag.
    paced = APP_DIR / "services" / "paced_http.py"
    assert len(_hand_stamps(ast.parse(paced.read_text(encoding="utf-8")))) == 1


def test_every_client_takes_the_shared_transport():
    for client in (CoinStatsClient, CoinGeckoClient, LogoDevClient, OpenFigiClient):
        assert issubclass(client, PacedGet), client
        assert client._pace is PacedGet._pace
        assert client._paced is PacedGet._paced
        assert client._send is PacedGet._send
