"""
The paced GET every third-party API client makes, written once.

`CoinStatsClient` and `CoinGeckoClient` each carried the same `_pace` and the same first
half of `_get`, line for line: one request at a time, spaced from when the previous one
*finished* (a slow call must not let the next follow on its heels), and a transport
failure raised as the provider's own transient error with the type and message only — the
URL in it carries no secret by construction, the keys travel in headers. What follows a
response is genuinely each provider's: CoinStats answers 409 while a portfolio is still
syncing and charges credits per endpoint, CoinGecko's 4xx is a request error, logo.dev's
404 is "no such brand". So the status mapping stays in each client, and this module owns
only the half they shared.

Neutral on purpose (moved out of the crypto-only `crypto_http` on 2026-10-17, when the
logo.dev client became the third user): transport only, no table, no figure, so the crypto
book may import it (`tests/test_crypto_isolation.py`, `ALLOWED_SHARED`) and a stock-side
client may too without importing crypto code. `tests/test_crypto_http.py` fails any module
that defines its own `_pace` again.
"""
from typing import Any, Dict, Optional, Type

import httpx


class PacedGet:
    """
    Mixin for a client that sets `_client` (an `httpx.AsyncClient` while open),
    `_min_interval_s`, `_clock`, `_sleep` and `_last_request_at` in its constructor.
    """

    async def _pace(self) -> None:
        # `_last_request_at` is when the previous call *finished* (see `_send`).
        if self._last_request_at is not None:
            wait = self._min_interval_s - (self._clock() - self._last_request_at)
            if wait > 0:
                await self._sleep(wait)

    async def _send(
        self,
        path: str,
        *,
        params: Optional[Dict[str, Any]],
        headers: Dict[str, str],
        transient: Type[Exception],
    ) -> httpx.Response:
        await self._pace()
        try:
            return await self._client.get(path, params=params, headers=headers)
        except httpx.HTTPError as e:
            raise transient(f"{path}: {type(e).__name__}: {e}") from e
        finally:
            self._last_request_at = self._clock()


def json_body(path: str, response: httpx.Response, transient: Type[Exception]) -> Any:
    """The decoded body of a successful call; a body that is not JSON is transient."""
    try:
        return response.json()
    except ValueError as e:
        raise transient(f"{path}: the response was not JSON") from e
