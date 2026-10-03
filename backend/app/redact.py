"""
Redact secrets from anything that reaches a public surface.

The Flex protocol sends the token as a URL query parameter (`...&t=<token>&q=...`),
and `requests` transport errors stringify with the full request URL — so a plain
`str(e)` from a failed SendRequest carries the token. Those strings used to be
recorded verbatim into `sync_runs.message` and re-served forever by the
unauthenticated `/api/scheduler/status` and `/history` endpoints; production
really did leak the token that way (found and scrubbed 2026-07-28).

The CoinStats key and share token, and the CoinGecko key, are masked the same way. They travel in request
headers, which transport errors do not stringify, so this is the second line of
defence rather than the first — a secret that never enters a message cannot leak
from one, and one that somehow does is still caught here.

Every error string that can be stored in `sync_runs` or returned by a router goes
through `redact_secrets`, and the read path redacts again so rows written before
this module existed (or restored from a backup) cannot leak either.
"""
import re
from typing import Any, List

from app.config import settings

_REDACTED = "[REDACTED]"

# The `t=` query parameter wherever a URL was stringified into a message. The query
# id (`q=`) is left readable: it is already public in the repo docs and useless
# without the token, and keeping it makes the stored error still debuggable.
_TOKEN_PARAM = re.compile(r"([?&]t=)[^&\s'\"]+")

# Never substring-replace a trivially short secret value: a one-letter test token
# would corrupt every message it touches, and a six-digit share passcode would mangle
# every figure that happens to contain those digits. Real Flex tokens and CoinStats
# keys are far longer; a passcode is masked only when it is long enough to be safe to
# (CoinStats accepts password-style ones — seen 2026-09-28). It only ever travels in a
# request header either way.
_MIN_TOKEN_LEN = 8

# Settings whose literal value is masked wherever it appears.
_LITERAL_SECRET_SETTINGS = (
    "ibkr_token",
    "coin_stats_api_key",
    "coin_stats_share_token",
    "coin_stats_share_passcode",
    "coingecko_api_key",
)


def _literal_secrets() -> List[str]:
    """The configured secrets long enough to substring-replace safely. Read at call
    time so a test's monkeypatch, or a value that changes, is honoured."""
    values = (getattr(settings, name, "") or "" for name in _LITERAL_SECRET_SETTINGS)
    return [v for v in values if len(v) >= _MIN_TOKEN_LEN]


def redact_secrets(value: Any) -> Any:
    """
    Mask secrets in a string, or recursively inside dict/list payloads — scheduler
    job results nest step error messages under ``details``. Anything else passes
    through unchanged.
    """
    if isinstance(value, str):
        out = _TOKEN_PARAM.sub(rf"\1{_REDACTED}", value)
        for secret in _literal_secrets():
            out = out.replace(secret, _REDACTED)
        return out
    if isinstance(value, dict):
        return {k: redact_secrets(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_secrets(v) for v in value]
    return value
