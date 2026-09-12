"""
IBKR Flex Query Service
Fetches and parses securities and tax lot data from Interactive Brokers.
Focuses on securities only - ignores dividends, cash, and other transactions.
"""
from typing import List, Dict, Optional, Tuple
from datetime import datetime, date
from decimal import Decimal
import asyncio
import logging
import time
import xml.etree.ElementTree as ET

import requests
from ibflex import client, parser, Types, AssetClass
from ibflex import enums as ibflex_enums
from ibflex.client import ResponseCodeError, BadResponseError

from app.config import settings

logger = logging.getLogger(__name__)

# Codes that mean "the statement isn't ready yet" during the *retrieve* step. IBKR's
# docs are explicit that these must be handled by re-fetching the SAME reference code:
#   "If statements are still being generated when you submit your request to retrieve
#    them, you should not re-initiate the Flex request, but instead keep trying to
#    retrieve the statement."
# ibflex only treats 1009/1019 (and 1018) this way, so 1001 aborts its download() and
# any outer retry re-runs SendRequest — starting a brand-new generation job each time.
# That is what earned two `1025` lockouts. 1003 is excluded: "Statement is not
# available" is terminal, not a wait.
_RETRIEVE_PENDING_CODES = {
    "1001", "1004", "1005", "1006", "1007", "1008", "1009", "1018", "1019", "1021"
}
# Codes worth re-issuing SendRequest for. Only reachable before a reference code
# exists, where there is nothing to poll and re-initiating is the sole option.
#
# `1001` is deliberately ABSENT even though it is transient. At the *request* step it
# means IBKR tried to start generating and couldn't — i.e. a failed generation, which
# is exactly what `1025` counts. Re-requesting after a 1001 is what locked the token on
# 2026-07-26 (and, by the same mechanism, twice before):
#     Code=1001 ... attempt 1/4, re-requesting in 120s  ->  Code=1025 Too many failed attempts
# With five scheduled IBKR attempts a day, giving up on a 1001 costs a few hours of
# freshness; re-requesting costs a multi-hour lockout of *every* sync. So we fail fast
# and let the next scheduled job start a clean generation.
#
# The codes that remain all mean "throttled or busy, no generation job was created"
# (1018 is the rate limit itself, 1009/1019/1021 are server-side transients), so for
# those a slow retry is safe.
# SendRequest's (connect, read) timeouts. A tuple, so a slow *answer* is not mistaken for
# an unreachable host — ibflex's own request_statement() uses a scalar 5 s that covers
# both, and re-sends on either. Generous on the read side: IBKR answers SendRequest with
# a reference code, but under the load that produces 1001s it can take its time, and a
# timeout here is treated as "may have been accepted" (see fetch_flex_data).
_SEND_REQUEST_TIMEOUT = (10, 60)


def send_flex_request(token: str, query_id: str) -> client.StatementAccess:
    """
    Step 1 of the Flex protocol — SendRequest — issued **exactly once**.

    Not `ibflex.client.request_statement()`. That delegates to `client.submit_request()`,
    which catches `requests.exceptions.Timeout` and re-sends the same GET up to three
    times with 5 s, 10 s and 15 s ceilings. A *read* timeout there is not "never reached
    IBKR": it is IBKR taking longer than five seconds to answer a request it has already
    accepted, so every re-send starts another statement generation — exactly what
    `Code=1025: Too many failed attempts` counts. With the outer retry budget on top,
    one scheduled slot could issue up to twelve SendRequests in about twenty minutes
    while `_download_statement`'s docstring promised "SendRequest once" and
    `tests/test_flex_retry_policy.py` pinned only the application's own budget. Found
    2026-09-12 by reading the installed ibflex 0.15 source; the re-send announces itself
    with a bare `print()`, so it never appeared under any logger in the container log.

    One GET, one attempt, a (connect, read) timeout pair; the reply goes through ibflex's
    own parser so callers see the same exception types as before (`ResponseCodeError`,
    `BadResponseError`, `requests.exceptions.RequestException`). The *poll* step keeps
    using `client.submit_request`: a repeated GetStatement retrieves the same reference,
    which is what IBKR asks for.
    """
    response = requests.get(
        client.REQUEST_URL,
        params={"v": "3", "t": token, "q": query_id},
        headers={"user-agent": "Java"},
        timeout=_SEND_REQUEST_TIMEOUT,
    )
    stmt_access = client.parse_stmt_response(response)
    if isinstance(stmt_access, client.StatementError):
        raise ResponseCodeError(stmt_access.ErrorCode, stmt_access.ErrorMessage)
    return stmt_access


_REQUEST_RETRYABLE_CODES = {
    "1004", "1005", "1006", "1007", "1008", "1009", "1018", "1019", "1021"
}
# How long to keep politely polling one reference code before giving up (seconds).
# The interactive path must stay under nginx's proxy_read_timeout 300.
_RETRIEVE_DEADLINE_INTERACTIVE = 120
_RETRIEVE_DEADLINE_PATIENT = 900
# Fallback wait between polls when ibflex doesn't hand us a delay.
_RETRIEVE_POLL_DELAY = 5
# IBKR limits a token to one request per second and 10 requests per minute (the text
# of Flex error 1018), and a single ibflex download() is already several HTTP requests:
# one to request the statement, then repeated polls until it's ready, each wrapped in
# its own 3-try loop inside ibflex.client.submit_request. So an eager outer retry loop
# blows through the per-minute cap and then trips the harsher, undocumented
# `Code=1025: Too many failed attempts`, which locks the token for *hours* and blocks
# all syncing. Retries must therefore be few and far apart.
#
# That inner 3-try loop is also why SendRequest is issued by `send_flex_request` below
# rather than by `client.request_statement`: on the request step a re-send is a new
# generation, not a harmless re-poll. Found 2026-09-12; see the function.
#
# Interactive default: one retry, since POST /api/sync/ibkr is synchronous and a user
# (and the reverse proxy) is waiting on it.
_FLEX_RETRY_DELAYS = [30]
# Scheduled jobs have nobody waiting, so they can be patient instead of pushy.
FLEX_RETRY_DELAYS_PATIENT = [120, 300, 600]
# Code=1018 *is* the per-minute cap being hit; never retry it quickly.
_FLEX_RATE_LIMIT_MIN_DELAY = 60

# Flex `levelOfDetail` values that are roll-ups of other rows in the same section
# (e.g. an ORDER row summarising its own EXECUTION fills, or a CLOSED_LOT row
# restating a sale). IBKR gives each row its own transactionID, so the idempotent
# upserts cannot dedupe them and ingesting both would double-count trades and
# realized P&L. ibflex's own docs say as much: "Trades: uncheck 'Symbol Summary',
# 'Asset Class', 'Orders'".
_AGGREGATE_LEVELS_OF_DETAIL = {
    "ORDER", "SYMBOL_SUMMARY", "ASSET_SUMMARY", "SUMMARY", "CLOSED_LOT",
}


# Every attribute the extractors below actually read, per element. This is what makes a
# dropped attribute *material* rather than cosmetic: ibflex 0.15 (2021) cannot model
# most of what IBKR now sends, and reporting all of it as a warning on every sync buried
# the one kind that matters — an attribute we read, dropped for an unparseable value,
# which silently changes a figure.
#
# Keep it exact rather than generous. Listing a field we do not read only re-creates the
# noise; omitting one we do read makes a real problem quiet, so
# `tests/test_flex_attr_coverage.py` derives the truth from the extractors' own source
# (an AST walk intersected with ibflex's dataclass fields) and fails if this drifts.
INGESTED_ATTRS: Dict[str, frozenset] = {
    "OpenPosition": frozenset({
        "assetCategory", "conid", "currency", "description", "isin", "listingExchange",
        "symbol", "costBasisMoney", "costBasisPrice", "openDateTime", "position",
        "reportDate",
    }),
    "SecurityInfo": frozenset({
        "assetCategory", "conid", "currency", "description", "isin", "listingExchange",
        "symbol",
    }),
    "Trade": frozenset({
        "assetCategory", "buySell", "conid", "currency", "fifoPnlRealized",
        "ibCommission", "proceeds", "quantity", "reportDate", "symbol", "tradeDate",
        "tradeID", "tradePrice", "transactionID",
    }),
    "CorporateAction": frozenset({
        "actionDescription", "assetCategory", "conid", "currency", "dateTime",
        "proceeds", "quantity", "reportDate", "symbol", "transactionID", "type", "value",
    }),
    "CashTransaction": frozenset({
        "amount", "conid", "currency", "dateTime", "description", "reportDate",
        "settleDate", "symbol", "transactionID", "type",
    }),
    "Transfer": frozenset({
        # `date` is real schema here, not a Python builtin — it is the transfer's own
        # date, which `_transfer_to_flow` prefers over reportDate. A first hand-written
        # pass at this map dropped it as noise; the coverage test caught it, which is
        # the entire reason that test derives its truth from the source.
        "cashTransfer", "company", "conid", "currency", "date", "direction",
        "positionAmount", "quantity", "reportDate", "symbol", "transactionID", "type",
    }),
    "EquitySummaryByReportDateInBase": frozenset({
        "reportDate", "cash", "stock", "total",
    }),
    "CashReportCurrency": frozenset({
        "currency", "endingCash", "levelOfDetail", "toDate",
    }),
    # Read only to establish the account's base currency — see _base_currency.
    "ConversionRate": frozenset({"toCurrency"}),
    "AccountInformation": frozenset({"currency"}),
    # Read by parse_flex_xml itself, not by an extractor.
    "FlexStatement": frozenset({"accountId", "fromDate", "toDate"}),
}


def _is_ingested_attr(key: str) -> bool:
    """
    Would dropping ``key`` ("Trade.notes") change what we ingest?

    An element we parse nothing from cannot be harmed by losing a field, so an unknown
    tag is cosmetic by definition — the only way that becomes wrong is a new extractor
    without a matching INGESTED_ATTRS entry, which is what the coverage test exists to
    catch.
    """
    tag, _, name = key.partition(".")
    return name in INGESTED_ATTRS.get(tag, frozenset())


def _dec(value) -> Optional[Decimal]:
    """Coerce an ibflex value to Decimal, tolerating None/blank."""
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (ValueError, ArithmeticError):
        return None


def _enum_name(value) -> Optional[str]:
    """Return a stable string for an ibflex enum member (its .name), else str()."""
    if value is None:
        return None
    return getattr(value, "name", None) or str(value)


def _as_date(value) -> Optional[date]:
    """Normalize a date/datetime to a plain date."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return None


class IBKRService:
    """Service for interacting with IBKR Flex Query API"""

    def __init__(self, token: Optional[str] = None, query_id: Optional[str] = None):
        """
        Initialize IBKR service with credentials.

        Args:
            token: IBKR Flex Query token (defaults to settings)
            query_id: IBKR Flex Query ID (defaults to settings)
        """
        self.token = token or settings.ibkr_token
        self.query_id = query_id or settings.ibkr_query_id
        # Set by _sanitize_flex_xml and read by parse_flex_xml on the next line: the
        # inventory of harmless schema drift, which belongs in the sync record rather
        # than the warnings banner. A latch rather than a third return value because
        # eighteen call sites unpack a 2-tuple; same shape as
        # MarketDataService.rate_limited, and declared here so a service built via
        # __new__ in a test still has it.
        self.last_schema_notes: List[str] = []

    @staticmethod
    def _lockout_error(e: ResponseCodeError) -> RuntimeError:
        """Turn IBKR's undocumented Code=1025 into something actionable."""
        return RuntimeError(
            f"IBKR has temporarily locked this Flex token (Code=1025: {e.msg}). "
            "It follows too many *failed* statement generations in a short window — IBKR "
            "allows one request per second and 10 per minute per token. Wait before syncing "
            "again, as repeated attempts can extend the lockout; if it persists beyond ~24h "
            "with no further attempts, contact IBKR — the block may be account-scoped."
        )

    def _download_statement(self, deadline_seconds: int) -> bytes:
        """
        Download the Flex statement using IBKR's documented 2-step protocol, blocking.

        Step 1 (`SendRequest`) asks IBKR to *generate* a statement and returns a
        reference code. Step 2 (`GetStatement`) retrieves it, and until it's ready
        answers with a "try again shortly" code.

        The whole point of doing this ourselves rather than calling ibflex's
        `client.download()` is that IBKR explicitly says a not-ready statement must be
        handled by re-fetching the **same reference code** — never by re-issuing
        SendRequest. ibflex doesn't treat 1001 as retryable, so it aborts, and any outer
        retry starts a *new* generation job; enough of those and IBKR blocks the token
        with `1025: Too many failed attempts`. Here step 1 happens once and everything
        after it is a cheap retrieve against the same code.

        Raises the same exception types as before (ResponseCodeError / BadResponseError),
        so callers are unaffected.

        "Once" has to hold below this method too: `send_flex_request` issues the
        SendRequest GET itself, because `client.request_statement` re-sends on a timeout.
        """
        stmt_access = send_flex_request(self.token, self.query_id)
        url = stmt_access.Url or client.STMT_URL
        reference = stmt_access.ReferenceCode
        logger.info(f"IBKR Flex statement requested, reference code {reference}")

        deadline = time.monotonic() + deadline_seconds
        last_reason = "no response yet"
        while True:
            try:
                response = client.submit_request(url=url, token=self.token, query=reference)
                status = client.check_statement_response(response)
                if status is True:
                    return response.content
                delay = status if isinstance(status, (int, float)) else _RETRIEVE_POLL_DELAY
            except ResponseCodeError as e:
                if e.code == "1025":
                    raise self._lockout_error(e) from e
                if e.code not in _RETRIEVE_PENDING_CODES:
                    raise
                delay = _FLEX_RATE_LIMIT_MIN_DELAY if e.code == "1018" else _RETRIEVE_POLL_DELAY
                last_reason = f"Code={e.code}: {e.msg}"
                logger.info(
                    f"IBKR Flex statement {reference} not ready (Code={e.code}); "
                    f"re-retrieving the same reference code in {delay}s"
                )
            except (requests.exceptions.RequestException, BadResponseError) as e:
                # Either a transport failure (DNS hiccup, reset, timeout) that never
                # reached IBKR, or a reply we couldn't parse — ibflex raises
                # BadResponseError for an empty/garbled body, which is usually the server
                # being busy. Both happen *after* a reference code exists, so the
                # statement is still being generated: recover by polling the same
                # reference. Letting either escape to the outer loop would re-issue
                # SendRequest and start a second generation job, which is precisely how
                # the token gets locked with 1025.
                delay = _RETRIEVE_POLL_DELAY
                last_reason = f"{type(e).__name__}: {e}"
                logger.warning(
                    f"Recoverable failure retrieving IBKR Flex statement {reference} "
                    f"({last_reason}); re-retrieving the same reference "
                    f"code in {delay}s"
                )

            if time.monotonic() + delay > deadline:
                # Report what actually kept failing: "too large" is the usual cause, but a
                # persistent network fault looks identical from here unless we say so.
                raise TimeoutError(
                    f"IBKR Flex statement {reference} was still not ready after "
                    f"{deadline_seconds}s of polling (last: {last_reason}). If that is a "
                    "not-ready code the statement is probably too large — narrow the Flex "
                    "Query period or sections."
                )
            time.sleep(delay)

    def _fix_currency_codes(self, xml_content: bytes) -> bytes:
        """
        Fix non-standard currency codes in IBKR XML.

        IBKR sometimes uses non-ISO currency codes that the ibflex library rejects.
        This function replaces them with standard ISO codes.

        Args:
            xml_content: Raw XML bytes from IBKR

        Returns:
            Fixed XML bytes with standard currency codes
        """
        # Map of IBKR non-standard codes to ISO standard codes
        currency_fixes = {
            b'RUS': b'RUB',  # Russian Ruble
        }

        fixed_xml = xml_content
        for wrong_code, correct_code in currency_fixes.items():
            # Replace in currency attributes (fromCurrency, toCurrency, currency)
            fixed_xml = fixed_xml.replace(b'fromCurrency="' + wrong_code + b'"', b'fromCurrency="' + correct_code + b'"')
            fixed_xml = fixed_xml.replace(b'toCurrency="' + wrong_code + b'"', b'toCurrency="' + correct_code + b'"')
            fixed_xml = fixed_xml.replace(b'currency="' + wrong_code + b'"', b'currency="' + correct_code + b'"')

        return fixed_xml

    def _sanitize_flex_xml(self, xml_content: bytes) -> Tuple[bytes, List[str]]:
        """
        Make IBKR's XML digestible by the pinned ibflex, which is strict about schema.

        ibflex converts every XML attribute onto a frozen dataclass field and raises
        FlexParserError on the first one it can't handle, which aborts parsing of the
        *entire* document — so a single unrecognised field or value breaks the whole
        sync, open positions included. IBKR has drifted well past ibflex 0.15 (2021):
        `subCategory` on <Trade> is unmodelled, and enabling extra Cash Transaction
        types yields values like `type="Broker Fees"` that its CashAction enum has
        never heard of. Two passes:

        1. Drop aggregate duplicate rows (see _AGGREGATE_LEVELS_OF_DETAIL) when real
           per-transaction rows sit alongside them. Done here, at the XML layer,
           because ibflex discards `levelOfDetail` on some element types entirely,
           which makes filtering after parsing impossible.
        2. Drop every attribute ibflex would reject, decided by calling its own
           parse_element_attr() — so "acceptable" means exactly what ibflex accepts,
           covering unmodelled names, unknown enum values, unparseable dates/decimals
           and unknown currencies alike. Dropping degrades gracefully: an unknown
           CashTransaction `type` becomes None and the row is then skipped by
           extract_cash_transactions (which only wants dividends/withholding), and an
           unknown CorporateAction `type` lands as 'UNKNOWN' with its quantity intact.
           (A required field with an unparseable value would still fail in ibflex's
           constructor — but that failed before this pass existed too.)

        Returns (xml, warnings). The original bytes are returned untouched when
        there was nothing to drop, and this never raises: on any failure the input
        is returned unchanged so a sanitizer bug can't be worse than not having one.

        **Only drops that can change ingested data become warnings.** IBKR sends dozens
        of fields ibflex 0.15 never modelled — `figi`, `serialNumber`, `weight`,
        `commodityType` — and this account's every sync reported all 27 of them, in one
        unreadable line, for ever. A warning that is always present and never actionable
        is worse than no warning: it is what teaches the reader to skip the banner that
        also carries a skipped tax lot or an unconvertible dividend. So a drop is only
        loud when the attribute is one the extractors actually read (INGESTED_ATTRS);
        the rest land on `last_schema_notes` for the sync record and the log.
        """
        warnings: List[str] = []
        self.last_schema_notes = []
        try:
            root = ET.fromstring(xml_content)
            # Materialize the walk before mutating, so removals can't disturb it.
            all_elements = list(root.iter())

            # Pass 1: de-duplicate aggregate rows.
            dropped_rows: Dict[str, int] = {}
            for container in all_elements:
                children = list(container)
                if not children:
                    continue
                aggregates = [
                    child for child in children
                    if (child.get('levelOfDetail') or '').strip().upper() in _AGGREGATE_LEVELS_OF_DETAIL
                ]
                if not aggregates:
                    continue
                if len(aggregates) == len(children):
                    # Every row is an aggregate, so they're the only data we have.
                    # Keep them: silently emptying a populated section would be a
                    # worse failure than a possible double-count.
                    levels = sorted({(c.get('levelOfDetail') or '') for c in aggregates})
                    warnings.append(
                        f"<{container.tag}> holds only aggregate rows (levelOfDetail={', '.join(levels)}); "
                        f"keeping them — enable execution-level detail in the Flex Query for exact data"
                    )
                    continue
                for child in aggregates:
                    container.remove(child)
                    dropped_rows[child.tag] = dropped_rows.get(child.tag, 0) + 1

            for tag, count in sorted(dropped_rows.items()):
                warnings.append(
                    f"ignored {count} aggregate <{tag}> row(s) that duplicate execution-level rows"
                )

            # Pass 2: drop any attribute ibflex would refuse. Rather than re-implement
            # its rules, ask ibflex itself — parse_element_attr() is the exact function
            # that runs during parsing, so it rejects precisely what would abort the
            # document: an attribute it doesn't model, an enum value it doesn't know
            # (e.g. CashTransaction type="Broker Fees"), an unparseable date/decimal,
            # or an unknown currency code.
            #
            # Re-walk rather than reusing the pass-1 snapshot: that list still holds the
            # rows pass 1 detached, so scrubbing it would both waste work and inflate the
            # warning counts with attributes from rows nobody will ever parse.
            dropped_attrs: Dict[str, int] = {}
            drop_reasons: Dict[str, str] = {}
            for elem in root.iter():
                if not elem.attrib:
                    continue
                flex_class = getattr(Types, elem.tag, None)
                if not (isinstance(flex_class, type) and issubclass(flex_class, Types.FlexElement)):
                    # A container (<Trades>, <FlexStatements>, ...) or a tag ibflex
                    # doesn't model — leave it exactly as IBKR sent it.
                    continue
                for name, value in list(elem.attrib.items()):
                    try:
                        parser.parse_element_attr(flex_class, name, value)
                    except Exception as exc:
                        del elem.attrib[name]
                        key = f"{elem.tag}.{name}"
                        dropped_attrs[key] = dropped_attrs.get(key, 0) + 1
                        drop_reasons.setdefault(
                            key,
                            "not modelled by this ibflex version"
                            if isinstance(exc, KeyError) else str(exc),
                        )

            # Split by consequence, not by cause. Both kinds were dropped for the same
            # reason — ibflex can't model them — but only one kind can change a number.
            material = {k: v for k, v in dropped_attrs.items() if _is_ingested_attr(k)}
            cosmetic = {k: v for k, v in dropped_attrs.items() if k not in material}

            if material:
                detail = ', '.join(
                    f"{key} x{count} ({drop_reasons.get(key, 'rejected')})"
                    for key, count in sorted(material.items())
                )
                warnings.append(
                    f"dropped IBKR attribute(s) the ingest READS and this ibflex version "
                    f"can't parse — data may be affected: {detail}"
                )
            if cosmetic:
                # One line, and it names the tags rather than 27 fully-qualified fields:
                # the actionable question is "did a NEW kind of thing start being
                # dropped", and the full inventory is in the sync run's details.
                tags = sorted({k.split('.', 1)[0] for k in cosmetic})
                self.last_schema_notes = [
                    f"{len(cosmetic)} unmodelled IBKR attribute(s) on <{'>, <'.join(tags)}> "
                    f"dropped; none are fields the ingest reads, so nothing was affected: "
                    + ', '.join(f"{k} x{v}" for k, v in sorted(cosmetic.items()))
                ]

            for warning in warnings:
                logger.warning(f"Flex XML sanitizer: {warning}")

            if not dropped_rows and not dropped_attrs:
                return xml_content, warnings

            return ET.tostring(root, encoding='utf-8'), warnings
        except Exception as e:
            logger.warning(f"Flex XML sanitizer failed ({e}); parsing the original XML unchanged")
            return xml_content, []

    async def fetch_flex_data(self, retry_delays: Optional[List[int]] = None) -> Dict:
        """
        Fetch data from IBKR Flex Query API.

        Args:
            retry_delays: Seconds to wait between download attempts; the number of
                attempts is len(retry_delays) + 1. Defaults to the short interactive
                budget; scheduled jobs should pass FLEX_RETRY_DELAYS_PATIENT.

        Returns:
            Dict containing parsed statement data with securities, open positions, etc.

        Raises:
            Exception: If API request fails or parsing errors occur
        """
        # Download via IBKR's 2-step protocol in a thread pool (blocking `requests`).
        # _download_statement() issues SendRequest once and then polls the SAME reference
        # code, which is what IBKR's docs require; the retry loop here therefore only ever
        # re-runs SendRequest, and only for errors raised *before* a reference code exists.
        # Keeping that rare and slow is what stops us re-triggering a Code=1025 lockout.
        delays = list(_FLEX_RETRY_DELAYS if retry_delays is None else retry_delays)
        max_attempts = len(delays) + 1
        patient = retry_delays is not None
        deadline = _RETRIEVE_DEADLINE_PATIENT if patient else _RETRIEVE_DEADLINE_INTERACTIVE
        loop = asyncio.get_event_loop()
        response = None
        for attempt in range(max_attempts):
            try:
                response = await loop.run_in_executor(
                    None, self._download_statement, deadline
                )
                break
            except ResponseCodeError as e:
                if e.code == "1025":
                    raise self._lockout_error(e) from e
                if e.code == "1001":
                    # A failed *generation*, which is what 1025 counts. Re-requesting here
                    # is how we locked the token before, so stop and let the next
                    # scheduled job ask for a fresh statement.
                    raise RuntimeError(
                        f"IBKR could not generate the Flex statement right now (Code=1001: "
                        f"{e.msg}). Not re-requesting: repeating SendRequest after a 1001 is "
                        "what trips the Code=1025 token lockout. The next scheduled sync will "
                        "try again."
                    ) from e
                if e.code not in _REQUEST_RETRYABLE_CODES or attempt == max_attempts - 1:
                    raise
                delay = delays[attempt]
                if e.code == "1018":
                    # This code *is* the per-minute cap; back off past the window.
                    delay = max(delay, _FLEX_RATE_LIMIT_MIN_DELAY)
                logger.warning(
                    f"IBKR Flex could not start statement generation (Code={e.code}: {e.msg}); "
                    f"attempt {attempt + 1}/{max_attempts}, re-requesting in {delay}s"
                )
                await asyncio.sleep(delay)
            except BadResponseError as e:
                # Only reachable from SendRequest itself — a malformed reply *during*
                # polling is handled inside _download_statement against the same
                # reference code. Here no statement exists yet, so re-requesting is the
                # only option and is safe.
                if attempt == max_attempts - 1:
                    raise
                delay = delays[attempt]
                logger.warning(
                    f"IBKR Flex bad/empty response ({e}); "
                    f"attempt {attempt + 1}/{max_attempts}, retrying in {delay}s"
                )
                await asyncio.sleep(delay)
            except requests.exceptions.ReadTimeout as e:
                # The request reached IBKR and the answer did not come back in time, so a
                # generation may already be running — re-issuing SendRequest is exactly
                # what 1025 counts. Same treatment as a 1001: fail fast, and let the next
                # slot ask fresh. (A ConnectTimeout is a ConnectionError, not this: it
                # never reached IBKR and falls through to the handler below.)
                raise RuntimeError(
                    f"IBKR Flex did not answer SendRequest within "
                    f"{_SEND_REQUEST_TIMEOUT[1]}s ({type(e).__name__}). Not re-requesting: "
                    "the request may already have started a generation, and repeating "
                    "SendRequest is what trips the Code=1025 token lockout. The next "
                    "scheduled sync will try again."
                ) from e
            except requests.exceptions.RequestException as e:
                # Raised here only if SendRequest itself never reached IBKR — a DNS or
                # connection failure, or a connect timeout (a blip like this cost the
                # whole 20:00 job on 2026-07-25). No statement was generated and nothing
                # counted against the token, so retrying is free — unlike a 1001, and
                # unlike the read timeout handled above.
                if attempt == max_attempts - 1:
                    raise
                delay = delays[attempt]
                logger.warning(
                    f"Network error reaching IBKR Flex ({type(e).__name__}: {e}); no request "
                    f"reached IBKR, attempt {attempt + 1}/{max_attempts}, retrying in {delay}s"
                )
                await asyncio.sleep(delay)

        return self.parse_flex_xml(response)

    def parse_flex_xml(self, xml_content: bytes) -> Dict:
        """
        Turn raw Flex statement bytes into the `flex_data` dict the extractors consume.

        Split out from fetch_flex_data() so the same pipeline can ingest a statement
        downloaded by hand from IBKR's web UI — that path needs no token and so cannot
        trip a `1025` lockout, which makes it the escape hatch when the token is blocked
        and the only way to reach a prior tax year (a YTD query can't). See
        `app/cli/ingest_flex_xml.py`.

        Does no I/O of any kind.
        """
        # Fix non-standard currency codes before parsing
        fixed_response = self._fix_currency_codes(xml_content)

        # Remove XML the pinned ibflex can't model (attributes IBKR has added since
        # 0.15, aggregate duplicate rows) so a single schema addition can't abort the
        # entire sync.
        fixed_response, flex_warnings = self._sanitize_flex_xml(fixed_response)

        # Parse the XML response
        try:
            response_obj = parser.parse(fixed_response)
            logger.debug(f"Response type: {type(response_obj)}")

            # The response is a FlexQueryResponse, we need to get the FlexStatement from it
            if hasattr(response_obj, 'FlexStatements'):
                flex_statements = response_obj.FlexStatements
                logger.debug(f"FlexStatements found, count: {len(flex_statements) if flex_statements else 0}")
                statement = flex_statements[0] if flex_statements else None
            else:
                # Fallback: maybe response_obj is already a FlexStatement
                statement = response_obj

            logger.debug(f"Final statement type: {type(statement)}, has OpenPositions: {hasattr(statement, 'OpenPositions')}")

        except Exception as e:
            # The ibflex library can be strict about currency codes
            # If parsing still fails, provide a helpful error message
            error_msg = str(e)
            if 'Unknown currency' in error_msg:
                raise ValueError(
                    f"IBKR Flex Query contains unsupported currency codes that the ibflex library cannot parse. "
                    f"Error: {error_msg}. "
                    f"The automatic fix for known currency codes (RUS->RUB) did not resolve this. "
                    f"Please contact support or check for other non-standard currency codes."
                )
            # Re-raise other parsing errors
            raise

        return {
            'statement': statement,
            'account_id': statement.accountId if hasattr(statement, 'accountId') else None,
            'from_date': statement.fromDate if hasattr(statement, 'fromDate') else None,
            'to_date': statement.toDate if hasattr(statement, 'toDate') else None,
            'flex_warnings': flex_warnings,  # Schema drift that could affect ingested data
            # Drift that provably could not: fields no extractor reads. Recorded so
            # "did something NEW start being dropped" stays answerable, without putting
            # a permanent banner on a healthy sync.
            'flex_notes': getattr(self, 'last_schema_notes', []),
        }

    async def extract_securities(self, flex_data: Dict) -> List[Dict]:
        """
        Extract unique securities from Flex Query data.
        Only includes stocks (STK) - filters out options, futures, etc.

        Args:
            flex_data: Parsed flex query data from fetch_flex_data()

        Returns:
            List of security dictionaries with normalized data
        """
        statement = flex_data['statement']
        securities = {}

        logger.debug(f"Statement type: {type(statement)}, has OpenPositions: {hasattr(statement, 'OpenPositions')}")

        # Get securities info from the SecuritiesInfo section
        if hasattr(statement, 'SecuritiesInfo') and statement.SecuritiesInfo:
            for sec_info in statement.SecuritiesInfo:
                # Only process stocks (STK) - ignore options, futures, cash, etc.
                # assetCategory is an enum (AssetClass.STOCK), not a string
                if not hasattr(sec_info, 'assetCategory') or sec_info.assetCategory != AssetClass.STOCK:
                    continue

                # Use conid as unique identifier
                conid = sec_info.conid if hasattr(sec_info, 'conid') else None
                if not conid:
                    continue

                securities[conid] = {
                    'conid': conid,
                    'isin': sec_info.isin if hasattr(sec_info, 'isin') else None,
                    'symbol': sec_info.symbol if hasattr(sec_info, 'symbol') else '',
                    'description': sec_info.description if hasattr(sec_info, 'description') else '',
                    'currency': sec_info.currency if hasattr(sec_info, 'currency') else 'USD',
                    'asset_category': 'STK',  # Only stocks
                    'exchange': sec_info.listingExchange if hasattr(sec_info, 'listingExchange') else None,
                }

        # Also check open positions for any securities not in SecuritiesInfo
        if hasattr(statement, 'OpenPositions') and statement.OpenPositions:
            positions_list = list(statement.OpenPositions)
            logger.debug(f"Found {len(positions_list)} positions in OpenPositions")
            for position in statement.OpenPositions:
                # Only process stocks - assetCategory is an enum (AssetClass.STOCK)
                if not hasattr(position, 'assetCategory') or position.assetCategory != AssetClass.STOCK:
                    continue

                conid = position.conid if hasattr(position, 'conid') else None
                if not conid or conid in securities:
                    continue

                securities[conid] = {
                    'conid': conid,
                    'isin': position.isin if hasattr(position, 'isin') else None,
                    'symbol': position.symbol if hasattr(position, 'symbol') else '',
                    'description': position.description if hasattr(position, 'description') else '',
                    'currency': position.currency if hasattr(position, 'currency') else 'USD',
                    'asset_category': 'STK',
                    'exchange': position.listingExchange if hasattr(position, 'listingExchange') else None,
                }

        return list(securities.values())

    async def extract_taxlots(self, flex_data: Dict) -> List[Dict]:
        """
        Extract tax lot information from open positions.
        Tax lots represent individual purchases (with date, quantity, cost basis).
        Only includes stock positions - filters out other asset types.

        Args:
            flex_data: Parsed flex query data from fetch_flex_data()

        Returns:
            List of tax lot dictionaries with purchase details
        """
        statement = flex_data['statement']
        taxlots = []

        # Extract from OpenPositions
        if hasattr(statement, 'OpenPositions') and statement.OpenPositions:
            for position in statement.OpenPositions:
                # Only process stocks - assetCategory is an enum (AssetClass.STOCK)
                if not hasattr(position, 'assetCategory') or position.assetCategory != AssetClass.STOCK:
                    continue

                # Basic position info
                conid = position.conid if hasattr(position, 'conid') else None
                quantity = position.position if hasattr(position, 'position') else 0

                # Cost basis information
                cost_basis_money = position.costBasisMoney if hasattr(position, 'costBasisMoney') else 0
                cost_basis_price = position.costBasisPrice if hasattr(position, 'costBasisPrice') else 0

                # Get symbol for logging
                symbol = position.symbol if hasattr(position, 'symbol') else 'UNKNOWN'

                # openDateTime comes off the parsed row. This used to be scraped
                # from the raw XML into a separate list and matched back by
                # position index — but that list kept only STK rows carrying the
                # attribute, while the loop enumerated *every* OpenPosition, so a
                # single bond/option row or one lot without the attribute shifted
                # every index after it. The conid check caught the mismatch and
                # fell back to reportDate rather than realigning, which silently
                # stamped every later lot with the statement date: the wrong FX
                # date on cost_basis_eur, deployment collapsed into one month,
                # and XIRR booking every purchase on the same day.
                open_date = getattr(position, 'openDateTime', None)
                if isinstance(open_date, datetime):
                    open_date = open_date.date()

                if not isinstance(open_date, date):
                    # Fallback to reportDate if openDateTime isn't present
                    report_date = position.reportDate if hasattr(position, 'reportDate') and position.reportDate else None
                    if isinstance(report_date, date):
                        open_date = report_date
                        logger.warning(f"No openDateTime for {symbol}, using reportDate: {open_date}")
                    else:
                        open_date = date.today()
                        logger.warning(f"No openDateTime or reportDate for {symbol} (conid: {conid}), using today's date")

                if not conid or quantity == 0:
                    continue

                taxlot = {
                    'conid': conid,
                    'open_date': open_date,
                    'quantity': Decimal(str(quantity)),
                    'cost_basis': Decimal(str(abs(cost_basis_money))),  # Total cost
                    'price_per_unit': Decimal(str(abs(cost_basis_price))),  # Price per share
                    'currency': position.currency if hasattr(position, 'currency') else 'USD',
                    'is_open': True,
                }

                taxlots.append(taxlot)

        return taxlots

    async def extract_trades(self, flex_data: Dict) -> List[Dict]:
        """
        Extract executed BUY/SELL trades from the Flex Query <Trades> section.

        Tolerant: returns [] if the section is absent (the Flex Query hasn't been
        updated to include Trades yet), so the caller degrades to today's
        heuristic reconciliation. Only STK trades are kept. ``quantity`` is
        stored signed as IBKR reports it (buys positive, sells negative).
        """
        statement = flex_data['statement']
        trades_section = getattr(statement, 'Trades', None)
        if not trades_section:
            return []

        trades: List[Dict] = []
        for t in trades_section:
            if getattr(t, 'assetCategory', None) != AssetClass.STOCK:
                continue
            conid = getattr(t, 'conid', None)
            if not conid:
                continue

            ib_key = (
                getattr(t, 'transactionID', None)
                or getattr(t, 'tradeID', None)
                or f"{conid}-{getattr(t, 'tradeDate', '')}-{getattr(t, 'quantity', '')}-{getattr(t, 'tradePrice', '')}"
            )
            trade_date = _as_date(getattr(t, 'tradeDate', None)) or _as_date(getattr(t, 'reportDate', None))
            if not trade_date:
                logger.warning(f"Trade {ib_key} for conid {conid} has no usable date; skipping")
                continue

            trades.append({
                'ib_key': str(ib_key),
                'conid': str(conid),
                'symbol': getattr(t, 'symbol', None),
                'trade_date': trade_date,
                'buy_sell': _enum_name(getattr(t, 'buySell', None)) or 'UNKNOWN',
                'quantity': _dec(getattr(t, 'quantity', None)) or Decimal("0"),
                'price': _dec(getattr(t, 'tradePrice', None)),
                'proceeds': _dec(getattr(t, 'proceeds', None)),
                'commission': _dec(getattr(t, 'ibCommission', None)),
                'currency': getattr(t, 'currency', None),
                'realized_pnl': _dec(getattr(t, 'fifoPnlRealized', None)),
                'asset_category': 'STK',
            })

        logger.info(f"Extracted {len(trades)} STK trade(s) from Flex <Trades>")
        return trades

    async def extract_corporate_actions(self, flex_data: Dict) -> List[Dict]:
        """
        Extract corporate actions (splits, spinoffs, mergers, symbol changes...)
        from the Flex Query <CorporateActions> section.

        Tolerant: returns [] if the section is absent. Only STK actions are kept.
        """
        statement = flex_data['statement']
        ca_section = getattr(statement, 'CorporateActions', None)
        if not ca_section:
            return []

        actions: List[Dict] = []
        for ca in ca_section:
            if getattr(ca, 'assetCategory', None) != AssetClass.STOCK:
                continue
            conid = getattr(ca, 'conid', None)
            if not conid:
                continue

            action_date = _as_date(getattr(ca, 'dateTime', None)) or _as_date(getattr(ca, 'reportDate', None))
            if not action_date:
                continue

            ib_key = (
                getattr(ca, 'transactionID', None)
                or f"{conid}-{_enum_name(getattr(ca, 'type', None))}-{action_date}-{getattr(ca, 'quantity', '')}"
            )

            actions.append({
                'ib_key': str(ib_key),
                'conid': str(conid),
                'symbol': getattr(ca, 'symbol', None),
                'action_date': action_date,
                'action_type': _enum_name(getattr(ca, 'type', None)) or 'UNKNOWN',
                'quantity': _dec(getattr(ca, 'quantity', None)),
                'value': _dec(getattr(ca, 'value', None)),
                'proceeds': _dec(getattr(ca, 'proceeds', None)),
                'currency': getattr(ca, 'currency', None),
                'description': getattr(ca, 'actionDescription', None),
            })

        logger.info(f"Extracted {len(actions)} STK corporate action(s) from Flex <CorporateActions>")
        return actions

    async def extract_cash_transactions(self, flex_data: Dict) -> List[Dict]:
        """
        Extract dividend-related cash transactions (Dividends, Payment In Lieu,
        Withholding Tax) from the Flex Query <CashTransactions> section.

        Tolerant: returns [] if the section is absent. Consumed by the dividend
        service to compute real gross / withholding / net income. ``amount`` is
        signed as IBKR reports it (dividends positive, withholding negative).
        """
        statement = flex_data['statement']
        ct_section = getattr(statement, 'CashTransactions', None)
        if not ct_section:
            return []

        dividend_types = {
            ibflex_enums.CashAction.DIVIDEND,
            ibflex_enums.CashAction.PAYMENTINLIEU,
            ibflex_enums.CashAction.WHTAX,
        }

        txns: List[Dict] = []
        for ct in ct_section:
            ct_type = getattr(ct, 'type', None)
            if ct_type not in dividend_types:
                continue
            conid = getattr(ct, 'conid', None)
            if not conid:
                continue

            pay_date = _as_date(getattr(ct, 'settleDate', None)) or _as_date(getattr(ct, 'dateTime', None)) \
                or _as_date(getattr(ct, 'reportDate', None))
            if not pay_date:
                continue

            ib_key = (
                getattr(ct, 'transactionID', None)
                or f"{conid}-{_enum_name(ct_type)}-{pay_date}-{getattr(ct, 'amount', '')}"
            )

            txns.append({
                'ib_key': str(ib_key),
                'conid': str(conid),
                'symbol': getattr(ct, 'symbol', None),
                'pay_date': pay_date,
                'type': _enum_name(ct_type),  # DIVIDEND / PAYMENTINLIEU / WHTAX
                'amount': _dec(getattr(ct, 'amount', None)) or Decimal("0"),
                'currency': getattr(ct, 'currency', None),
                'description': getattr(ct, 'description', None),
            })

        logger.info(f"Extracted {len(txns)} dividend/withholding cash transaction(s) from Flex <CashTransactions>")
        return txns

    async def extract_cash_flows(self, flex_data: Dict) -> List[Dict]:
        """
        Extract external deposits and withdrawals from <CashTransactions>.

        A sibling of extract_cash_transactions rather than an extension of it,
        because that one requires a ``conid`` to link a dividend to a security and
        deposits have none — widening its filter would drop every row anyway.

        Needs 'Deposits & Withdrawals' enabled on the Flex Query; returns [] when
        the section is absent or the option is off, which is a normal state, not an
        error. ``amount`` keeps IBKR's sign: deposits positive, withdrawals
        negative. ``CashAction.DEPOSITWITHDRAW`` is a single enum value covering
        both directions, so that sign is the only thing telling them apart.
        """
        statement = flex_data['statement']
        ct_section = getattr(statement, 'CashTransactions', None)
        if not ct_section:
            return []

        flows: List[Dict] = []
        for ct in ct_section:
            ct_type = getattr(ct, 'type', None)
            if ct_type is not ibflex_enums.CashAction.DEPOSITWITHDRAW:
                continue

            # settleDate first: when the cash actually landed, not when it was announced.
            flow_date = _as_date(getattr(ct, 'settleDate', None)) or _as_date(getattr(ct, 'dateTime', None)) \
                or _as_date(getattr(ct, 'reportDate', None))
            if not flow_date:
                continue

            amount = _dec(getattr(ct, 'amount', None))
            if not amount:
                # A zero or missing amount moves no money; keeping it would only add
                # a row that every consumer has to skip.
                continue

            currency = getattr(ct, 'currency', None)
            ib_key = (
                getattr(ct, 'transactionID', None)
                or f"CF-{_enum_name(ct_type)}-{flow_date}-{amount}-{currency}"
            )

            flows.append({
                'ib_key': str(ib_key),
                'flow_date': flow_date,
                'flow_type': _enum_name(ct_type),  # DEPOSITWITHDRAW
                'amount': amount,
                'currency': currency,
                'description': getattr(ct, 'description', None),
            })

        logger.info(f"Extracted {len(flows)} deposit/withdrawal(s) from Flex <CashTransactions>")
        return flows

    async def extract_equity_summary(self, flex_data: Dict) -> List[Dict]:
        """
        IBKR's own end-of-day cash and securities balances, one row per report date,
        from the ``<EquitySummaryInBase>`` section.

        **Returns [] when the section is not enabled, which is the default state** — the
        Flex query has to be edited in the portal to deliver it. That is deliberately a
        supported answer rather than a fault: `CashService` derives a balance from the
        trade, deposit and dividend ledgers and falls back to it per day, so the feature
        works without this and gets more accurate with it. See the `CashBalance` model
        for what the derivation structurally cannot see.

        Amounts are in **IBKR's account base currency**, which need not be the app's
        display base. The currency is not an attribute of the row — the section is named
        "InBase" and the base lives on the account — so it is taken from the statement's
        own `AccountInformation` where present and left None otherwise, never guessed
        from the app's setting. A label a figure has not earned is how SBI was carried
        61% high.
        """
        statement = flex_data['statement']
        section = getattr(statement, 'EquitySummaryInBase', None)
        if not section:
            return []

        currency = self._base_currency(statement)

        rows: List[Dict] = []
        for row in section:
            report_date = _as_date(getattr(row, 'reportDate', None))
            cash = _dec(getattr(row, 'cash', None))
            # A row with no date or no cash figure carries nothing this table is for.
            # Skipped rather than defaulted, because a zero balance is a real answer and
            # must not be manufactured from a missing one.
            if not report_date or cash is None:
                continue
            rows.append({
                'report_date': report_date,
                'currency': currency,
                'cash': cash,
                'stock': _dec(getattr(row, 'stock', None)),
                'total': _dec(getattr(row, 'total', None)),
            })
        return rows

    @staticmethod
    def _base_currency(statement) -> Optional[str]:
        """
        The account's own base currency — what a `*InBase` figure is denominated in.

        It is **not** the app's `app_settings.base_currency`, and it must never be
        defaulted to one. A cash balance stamped with a currency it has not earned is
        the SBI failure exactly: 12,501.58 CHF read as EUR becomes ~13,400 CHF after
        projection, a 7% error on a figure that looks entirely reasonable.

        Two sources, because the obvious one is optional. `AccountInformation` carries
        it directly but is a **section that has to be enabled**, and this account's query
        does not have it — measured on the 2026-08-26 statement, which is exactly the
        configuration this had to work for. So the fallback reads `<ConversionRates>`,
        whose every row converts *into* the base: 1,014 rows on that statement, all
        `toCurrency="CHF"`, and it is already present because the query sets
        `Include Currency Rates? Yes`.

        **Unanimity is required**, not a majority or a first row. The section's whole
        premise is that one currency is the base, so a split answer means the premise is
        wrong and guessing which half to believe is how a plausible wrong number gets
        made. `None` then, and the caller drops the figure rather than labelling it.
        """
        account_info = getattr(statement, 'AccountInformation', None)
        stated = getattr(account_info, 'currency', None) if account_info else None
        if stated:
            return stated

        rates = getattr(statement, 'ConversionRates', None) or ()
        targets = {
            getattr(r, 'toCurrency', None) for r in rates
            if getattr(r, 'toCurrency', None)
        }
        if len(targets) == 1:
            return targets.pop()
        return None

    async def extract_cash_report(self, flex_data: Dict) -> List[Dict]:
        """
        Ending cash from the ``<CashReport>`` section — the *other* way IBKR will tell us
        a balance, and the one actually reachable in this account's Flex Query editor.

        It is coarser than `<EquitySummaryInBase>`: one row per **currency** covering the
        whole statement period, rather than one row per day. So it yields a single
        authoritative anchor dated `toDate` per sync instead of a daily series — which is
        enough, because syncs land about daily and `_apply_measured` interpolates with
        derived movement between anchors rather than holding the level flat.

        Two shapes come back depending on which portal option is ticked, and both are
        handled rather than one being required:

        - **Base Currency Summary** emits a single row with `currency="BASE_SUMMARY"`
          (and `levelOfDetail` naming the base), already summed into the account's base.
        - **Currency Breakout** emits one row per currency held. Those need converting
          and summing, which the caller does — this account trades in five currencies, so
          it is a real case rather than a defensive one.

        Ticking both emits both, so the base row is flagged and the caller prefers it;
        summing a breakout *and* its own summary would double the balance.

        `endingCash`, not `endingSettledCash`: unsettled sale proceeds are still the
        account's money, and the derived series books a trade on its trade date, so the
        settled figure would disagree with everything around it by a couple of days'
        activity.
        """
        statement = flex_data['statement']
        section = getattr(statement, 'CashReport', None)
        if not section:
            return []

        base_currency = self._base_currency(statement)

        rows: List[Dict] = []
        for row in section:
            to_date = _as_date(getattr(row, 'toDate', None))
            ending = _dec(getattr(row, 'endingCash', None))
            # No date or no figure carries nothing. Skipped rather than defaulted: a zero
            # balance is a real answer and must not be manufactured from a missing one.
            if not to_date or ending is None:
                continue
            currency = getattr(row, 'currency', None)
            level = getattr(row, 'levelOfDetail', None) or ''
            # Two independent tells, because neither is documented as stable and the cost
            # of misreading one is a doubled balance.
            is_base = (
                (currency or '').upper() == 'BASE_SUMMARY'
                or level.strip().lower().startswith('base')
            )
            rows.append({
                'report_date': to_date,
                # A base-summary row's "currency" is the literal BASE_SUMMARY, which is
                # not a currency — the account's own base is what it is denominated in.
                'currency': (base_currency if is_base else currency),
                'ending_cash': ending,
                'is_base_summary': is_base,
            })
        return rows

    async def extract_transfers(self, flex_data: Dict) -> List[Dict]:
        """
        Extract position transfers (ACATS / internal) from the Flex <Transfers> section.

        These are not money added — an incoming transfer moves capital that was
        already saved somewhere else, so counting it as a contribution would show a
        large fake deposit in a month with no new savings. They are recorded so that
        cash arriving this way can be positively identified and excluded, and so an
        in-kind transfer is still on the record at zero cash.

        Returns [] if the section isn't enabled. ``cash_transfer`` is the cash leg
        (often zero for an in-kind transfer); ``position_amount`` is the securities
        value, kept for the description rather than treated as cash.
        """
        statement = flex_data['statement']
        tr_section = getattr(statement, 'Transfers', None)
        if not tr_section:
            return []

        transfers: List[Dict] = []
        for tr in tr_section:
            transfer_date = _as_date(getattr(tr, 'date', None)) \
                or _as_date(getattr(tr, 'reportDate', None))
            if not transfer_date:
                continue

            symbol = getattr(tr, 'symbol', None)
            ib_key = (
                getattr(tr, 'transactionID', None)
                or f"TR-{transfer_date}-{symbol}-{getattr(tr, 'quantity', '')}"
            )

            transfers.append({
                'ib_key': str(ib_key),
                'transfer_date': transfer_date,
                'transfer_type': _enum_name(getattr(tr, 'type', None)) or 'UNKNOWN',  # INTERNAL / ACATS
                'direction': _enum_name(getattr(tr, 'direction', None)) or 'UNKNOWN',  # IN / OUT
                'cash_transfer': _dec(getattr(tr, 'cashTransfer', None)) or Decimal("0"),
                'position_amount': _dec(getattr(tr, 'positionAmount', None)),
                'quantity': _dec(getattr(tr, 'quantity', None)),
                'symbol': symbol,
                'conid': str(getattr(tr, 'conid', None) or '') or None,
                'currency': getattr(tr, 'currency', None),
                'company': getattr(tr, 'company', None),
            })

        logger.info(f"Extracted {len(transfers)} transfer(s) from Flex <Transfers>")
        return transfers

    async def get_portfolio_summary(self) -> Dict:
        """
        Get a quick summary of the portfolio from IBKR.

        Returns:
            Dict with portfolio summary statistics
        """
        flex_data = await self.fetch_flex_data()
        securities = await self.extract_securities(flex_data)
        taxlots = await self.extract_taxlots(flex_data)

        return {
            'account_id': flex_data['account_id'],
            'from_date': flex_data['from_date'],
            'to_date': flex_data['to_date'],
            'securities_count': len(securities),
            'taxlots_count': len(taxlots),
            'total_positions': sum(lot['quantity'] for lot in taxlots),
        }
