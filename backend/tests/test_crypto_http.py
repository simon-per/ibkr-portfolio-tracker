"""
Both crypto clients pace and send through `app/services/crypto_http.PacedGet`.

`CoinStatsClient` and `CoinGeckoClient` carried the same `_pace` and the same transport
half of `_get`, line for line. The family test: no module under `app/` defines its own
`_pace` again, and both clients take the shared one.
"""
import ast
from pathlib import Path

from app.services.coingecko_client import CoinGeckoClient
from app.services.coinstats_client import CoinStatsClient
from app.services.crypto_http import PacedGet

APP_DIR = Path(__file__).resolve().parents[1] / "app"


def test_no_module_paces_requests_by_its_own_copy():
    offenders = [
        f"{path.relative_to(APP_DIR)}:{node.lineno}"
        for path in sorted(APP_DIR.rglob("*.py"))
        if path.name != "crypto_http.py"
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "_pace"
    ]
    assert offenders == [], f"use crypto_http.PacedGet: {offenders}"


def test_both_clients_take_the_shared_transport():
    for client in (CoinStatsClient, CoinGeckoClient):
        assert issubclass(client, PacedGet), client
        assert client._pace is PacedGet._pace
        assert client._send is PacedGet._send
