"""
Redact secrets from anything that reaches a public surface.

The Flex protocol sends the token as a URL query parameter (`...&t=<token>&q=...`),
and `requests` transport errors stringify with the full request URL — so a plain
`str(e)` from a failed SendRequest carries the token. Those strings used to be
recorded verbatim into `sync_runs.message` and re-served forever by the
unauthenticated `/api/scheduler/status` and `/history` endpoints; production
really did leak the token that way (found and scrubbed 2026-07-28).

The CoinStats key and share token, the CoinGecko key, the logo.dev key, the OpenFIGI
key and the admin token are masked the same way. They travel in request headers,
which transport errors do not stringify, so this is the second line of defence rather
than the first — a secret that never enters a message cannot leak from one, and one
that somehow does is still caught here. The Alpha Vantage key travels as an `apikey=`
URL parameter, so it is masked by pattern as well as by value.

Every error string that can be stored in `sync_runs` or returned by a router goes
through `redact_secrets`, and the read path redacts again so rows written before
this module existed (or restored from a backup) cannot leak either.

The log is the third surface, and `RedactingFormatter` covers it at the one place
every line passes through: a formatted record includes the traceback `exc_info`
appends, chained exceptions and all, which no call-site `redact_secrets(str(e))`
ever sees (GitHub issue #14). `app/logging_setup.configure_logging` installs it.
"""
import logging
import re
from typing import Any, List, Optional

from app.config import settings

_REDACTED = "[REDACTED]"

# The Flex `t=` and Alpha Vantage `apikey=` query parameters wherever a URL was
# stringified into a message. The query id (`q=`) is left readable: it is already
# public in the repo docs and useless without the token, and keeping it makes the
# stored error still debuggable.
_TOKEN_PARAM = re.compile(r"([?&](?:t|apikey)=)[^&\s'\"]+")

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
    "logo_dev_secret_key",
    "alpha_vantage_api_key",
    "openfigi_api_key",
    "api_admin_token",
    # Not a secret, but it carries the operator's email address (docs/company-financials.md).
    "edgar_user_agent",
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


class RedactingFormatter(logging.Formatter):
    """
    Redacts the fully formatted record: message, traceback (`exc_text`, chained
    exceptions included) and `stack_info` alike.

    A `logging.Filter` rewriting `record.msg` was the rejected alternative: the
    traceback is rendered later, by the formatter, so a filter never sees it.

    `inner` wraps a formatter configured by someone else (uvicorn's default and access
    formatters) so its layout is kept and only the redaction is added.
    """

    def __init__(self, *args: Any, inner: Optional[logging.Formatter] = None, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self._inner = inner

    def format(self, record: logging.LogRecord) -> str:
        text = self._inner.format(record) if self._inner else super().format(record)
        return redact_secrets(text)
