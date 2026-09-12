"""
Offline tests for the IBKR Flex download retry policy.

IBKR allows one request per second and 10 per minute per token, and a single
ibflex download() already issues several HTTP requests internally. Retrying
eagerly therefore trips the per-minute cap and then `Code=1025: Too many failed
attempts`, an undocumented lockout that blocks *all* syncing for hours. These
tests pin the policy that prevents that: few attempts, long gaps, never retry a
lockout, and — most importantly — never re-issue SendRequest just because the
statement isn't ready yet. No network: ibflex's client functions are monkeypatched.
"""
from types import SimpleNamespace

import pytest

from ibflex.client import ResponseCodeError, BadResponseError

from app.services import ibkr_service as ibkr_module
from app.services.ibkr_service import (
    IBKRService,
    FLEX_RETRY_DELAYS_PATIENT,
    _FLEX_RETRY_DELAYS,
)


@pytest.fixture
def no_sleep(monkeypatch):
    """Record backoff durations instead of actually waiting."""
    slept = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(ibkr_module.asyncio, 'sleep', fake_sleep)
    return slept


def _always_raise(monkeypatch, exc):
    """
    Fail at step 1 (SendRequest). That's the only step the outer retry loop may
    re-issue, so patching it here is what exercises that budget — and it keeps the
    tests offline now that fetch_flex_data drives the 2-step protocol itself rather
    than calling client.download().
    """
    calls = {'n': 0}

    def boom(token, query_id):
        calls['n'] += 1
        raise exc

    # The seam is our own single-GET SendRequest, not `client.request_statement`, which
    # re-sends on a timeout and is deliberately no longer called (see the guard test).
    monkeypatch.setattr(ibkr_module, 'send_flex_request', boom)
    return calls


def test_interactive_budget_is_small():
    """A user-facing sync must not spend the token's per-minute allowance."""
    assert len(_FLEX_RETRY_DELAYS) + 1 == 2
    assert min(_FLEX_RETRY_DELAYS) >= 20


def test_patient_budget_is_spread_out():
    assert all(delay >= 60 for delay in FLEX_RETRY_DELAYS_PATIENT)


@pytest.mark.asyncio
async def test_lockout_1025_is_never_retried(monkeypatch, no_sleep):
    """Retrying a 1025 can extend the lockout, so it must fail on the first hit."""
    calls = _always_raise(
        monkeypatch,
        ResponseCodeError('1025', 'Too many failed attempts. Please review your configuration.'),
    )

    with pytest.raises(RuntimeError, match='temporarily locked'):
        await IBKRService(token='t', query_id='q').fetch_flex_data()

    assert calls['n'] == 1  # no retry
    assert no_sleep == []  # and no backoff wait


@pytest.mark.asyncio
async def test_1001_at_the_request_step_is_never_re_requested(monkeypatch, no_sleep):
    """
    A 1001 from SendRequest means IBKR *tried to generate and failed* — precisely what
    `Code=1025` counts. This test exists because the opposite behaviour was pinned here
    and it locked the token on 2026-07-26:

        Code=1001 ...; attempt 1/4, re-requesting in 120s  ->  Code=1025

    With five scheduled IBKR jobs a day, giving up costs hours of freshness at worst;
    re-requesting costs a lockout of every sync. So: exactly one SendRequest, no backoff.
    Note this is the *request* step only — a 1001 while polling must keep retrieving, which
    test_not_ready_statement_is_re_retrieved_never_re_requested covers.
    """
    calls = _always_raise(
        monkeypatch,
        ResponseCodeError('1001', 'Statement could not be generated at this time.'),
    )

    with pytest.raises(RuntimeError, match='Not re-requesting'):
        await IBKRService(token='t', query_id='q').fetch_flex_data()

    assert calls['n'] == 1
    assert no_sleep == []


@pytest.mark.asyncio
async def test_1001_is_not_re_requested_even_on_the_patient_budget(monkeypatch, no_sleep):
    """The scheduled jobs pass their own delays; that must not re-enable re-requesting."""
    calls = _always_raise(
        monkeypatch,
        ResponseCodeError('1001', 'Statement could not be generated at this time.'),
    )

    with pytest.raises(RuntimeError, match='Not re-requesting'):
        await IBKRService(token='t', query_id='q').fetch_flex_data(
            retry_delays=FLEX_RETRY_DELAYS_PATIENT
        )

    assert calls['n'] == 1
    assert no_sleep == []


@pytest.mark.asyncio
async def test_network_failure_before_a_reference_exists_is_retried(monkeypatch, no_sleep):
    """
    A DNS/connection failure never reached IBKR, so nothing was generated and nothing
    counted against the token — retrying is free. A blip like this cost the whole 20:00
    job on 2026-07-25.
    """
    import requests

    calls = _always_raise(
        monkeypatch,
        requests.exceptions.ConnectionError('Failed to resolve gdcdyn.interactivebrokers.com'),
    )

    with pytest.raises(requests.exceptions.ConnectionError):
        await IBKRService(token='t', query_id='q').fetch_flex_data()

    assert calls['n'] == len(_FLEX_RETRY_DELAYS) + 1
    assert no_sleep == _FLEX_RETRY_DELAYS


@pytest.mark.asyncio
async def test_rate_limit_1018_backs_off_past_the_minute_window(monkeypatch, no_sleep):
    """1018 *is* the per-minute cap, so a short delay would just hit it again."""
    _always_raise(
        monkeypatch,
        ResponseCodeError('1018', 'Too many requests have been made from this token.'),
    )

    with pytest.raises(ResponseCodeError):
        await IBKRService(token='t', query_id='q').fetch_flex_data(retry_delays=[5, 5])

    assert no_sleep == [60, 60]


@pytest.mark.asyncio
async def test_permanent_error_is_not_retried(monkeypatch, no_sleep):
    calls = _always_raise(
        monkeypatch,
        ResponseCodeError('1020', 'Invalid token.'),
    )

    with pytest.raises(ResponseCodeError):
        await IBKRService(token='t', query_id='q').fetch_flex_data()

    assert calls['n'] == 1
    assert no_sleep == []


#
# The 2-step protocol: SendRequest once, then poll the SAME reference code.
#

class _FakeFlexServer:
    """
    Stands in for IBKR's two endpoints. `retrieve_results` is consumed one per poll;
    each item is either an exception to raise or a bytes payload to return.
    """

    def __init__(self, retrieve_results):
        self.retrieve_results = list(retrieve_results)
        self.request_calls = 0
        self.retrieve_calls = 0

    def install(self, monkeypatch):
        def request_statement(token, query_id):
            self.request_calls += 1
            return SimpleNamespace(ReferenceCode="REF123", Url=None)

        def submit_request(url, token, query):
            self.retrieve_calls += 1
            assert query == "REF123", "must poll the same reference code"
            outcome = self.retrieve_results.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return SimpleNamespace(content=outcome)

        monkeypatch.setattr(ibkr_module, 'send_flex_request', request_statement)
        monkeypatch.setattr(ibkr_module.client, 'submit_request', submit_request)
        # check_statement_response only sees successful payloads here; pending states are
        # delivered as ResponseCodeError from submit_request, matching ibflex's behaviour.
        monkeypatch.setattr(ibkr_module.client, 'check_statement_response', lambda r: True)
        monkeypatch.setattr(ibkr_module.time, 'sleep', lambda s: None)
        return self


def test_not_ready_statement_is_re_retrieved_never_re_requested(monkeypatch):
    """
    THE regression this refactor exists for. IBKR: "you should not re-initiate the Flex
    request, but instead keep trying to retrieve the statement." A 1001 while polling
    must NOT start a second generation job — that is what earned two 1025 lockouts.
    """
    server = _FakeFlexServer([
        ResponseCodeError('1001', 'Statement could not be generated at this time.'),
        ResponseCodeError('1019', 'Statement generation in progress.'),
        b'<FlexQueryResponse></FlexQueryResponse>',
    ]).install(monkeypatch)

    data = IBKRService(token='t', query_id='q')._download_statement(60)

    assert data == b'<FlexQueryResponse></FlexQueryResponse>'
    assert server.request_calls == 1        # <-- the whole point
    assert server.retrieve_calls == 3


def test_lockout_during_retrieve_is_not_retried(monkeypatch):
    server = _FakeFlexServer([
        ResponseCodeError('1025', 'Too many failed attempts. Please review your configuration.'),
    ]).install(monkeypatch)

    with pytest.raises(RuntimeError, match='temporarily locked'):
        IBKRService(token='t', query_id='q')._download_statement(60)

    assert server.request_calls == 1
    assert server.retrieve_calls == 1  # gave up immediately


def test_fatal_code_during_retrieve_does_not_re_request(monkeypatch):
    """An invalid token mid-poll is terminal; don't start another generation job."""
    server = _FakeFlexServer([
        ResponseCodeError('1015', 'Token is invalid.'),
    ]).install(monkeypatch)

    with pytest.raises(ResponseCodeError):
        IBKRService(token='t', query_id='q')._download_statement(60)

    assert server.request_calls == 1


def test_network_error_mid_poll_re_retrieves_the_same_reference(monkeypatch):
    """
    A transport failure while polling must not restart generation: the statement is
    already being built, and re-requesting is what trips 1025. Recover by polling the
    same reference code (the fake asserts the code never changes).
    """
    import requests

    server = _FakeFlexServer([
        requests.exceptions.ConnectionError('Temporary failure in name resolution'),
        requests.exceptions.ReadTimeout('timed out'),
        b'<FlexQueryResponse></FlexQueryResponse>',
    ]).install(monkeypatch)

    data = IBKRService(token='t', query_id='q')._download_statement(60)

    assert data == b'<FlexQueryResponse></FlexQueryResponse>'
    assert server.request_calls == 1
    assert server.retrieve_calls == 3


def test_unparseable_reply_mid_poll_re_retrieves_the_same_reference(monkeypatch):
    """
    ibflex raises BadResponseError for an empty/garbled body, which check_statement_response
    does *during* retrieve. It used to escape to the outer loop and re-issue SendRequest —
    a second generation job after a reference already existed, i.e. the 1025 mechanism.
    """
    server = _FakeFlexServer([
        BadResponseError(SimpleNamespace(content=b'')),
        b'<FlexQueryResponse></FlexQueryResponse>',
    ]).install(monkeypatch)

    data = IBKRService(token='t', query_id='q')._download_statement(60)

    assert data == b'<FlexQueryResponse></FlexQueryResponse>'
    assert server.request_calls == 1
    assert server.retrieve_calls == 2


def test_retrieve_gives_up_at_the_deadline(monkeypatch):
    """A statement that never becomes ready must time out, not poll forever."""
    server = _FakeFlexServer(
        [ResponseCodeError('1001', 'not ready')] * 500
    ).install(monkeypatch)

    with pytest.raises(TimeoutError, match='still not ready'):
        IBKRService(token='t', query_id='q')._download_statement(1)

    assert server.request_calls == 1
    assert server.retrieve_calls < 500  # stopped early


@pytest.mark.asyncio
async def test_custom_retry_delays_control_attempt_count(monkeypatch, no_sleep):
    # BadResponseError wraps a requests.Response, so hand it something with .content
    calls = _always_raise(monkeypatch, BadResponseError(SimpleNamespace(content=b'')))

    with pytest.raises(BadResponseError):
        await IBKRService(token='t', query_id='q').fetch_flex_data(retry_delays=[1, 2, 3])

    assert calls['n'] == 4
    assert no_sleep == [1, 2, 3]


# --- SendRequest is one HTTP request, whatever ibflex would do -------------------------
#
# `client.request_statement` delegates to `client.submit_request`, which re-sends the same
# GET up to three times on `requests.exceptions.Timeout` with 5/10/15 s ceilings. On the
# request step a re-send is a new statement generation — what 1025 counts — so with the
# outer budget on top one scheduled slot could issue twelve SendRequests. Found 2026-09-12
# by reading the installed library; every test above pinned only the application's own loop.


def test_send_request_is_a_single_get_even_when_it_times_out(monkeypatch):
    import requests

    calls = []

    def slow_get(url, **kwargs):
        calls.append(kwargs)
        raise requests.exceptions.ReadTimeout("IBKR took longer than the read ceiling")

    monkeypatch.setattr(ibkr_module.requests, 'get', slow_get)

    with pytest.raises(requests.exceptions.ReadTimeout):
        ibkr_module.send_flex_request('t', 'q')

    assert len(calls) == 1, "SendRequest was re-sent — ibflex's submit_request behaviour"
    # A (connect, read) pair, so a slow answer is not read as an unreachable host.
    connect, read = calls[0]['timeout']
    assert read >= 30
    assert calls[0]['params'] == {"v": "3", "t": "t", "q": "q"}


def test_send_request_parses_a_refusal_into_the_same_exception_type(monkeypatch):
    body = (
        b'<FlexStatementResponse timestamp="12 September, 2026 05:00 AM EDT">'
        b'<Status>Fail</Status><ErrorCode>1001</ErrorCode>'
        b'<ErrorMessage>Statement could not be generated at this time.</ErrorMessage>'
        b'</FlexStatementResponse>'
    )
    monkeypatch.setattr(
        ibkr_module.requests, 'get', lambda url, **kw: SimpleNamespace(content=body)
    )

    with pytest.raises(ResponseCodeError) as exc:
        ibkr_module.send_flex_request('t', 'q')
    assert exc.value.code == "1001"


def test_send_request_returns_the_reference_code(monkeypatch):
    body = (
        b'<FlexStatementResponse timestamp="12 September, 2026 05:00 AM EDT">'
        b'<Status>Success</Status><ReferenceCode>REF123</ReferenceCode>'
        b'<Url>https://gdcdyn.interactivebrokers.com/Universal/servlet/FlexStatementService.GetStatement</Url>'
        b'</FlexStatementResponse>'
    )
    monkeypatch.setattr(
        ibkr_module.requests, 'get', lambda url, **kw: SimpleNamespace(content=body)
    )

    assert ibkr_module.send_flex_request('t', 'q').ReferenceCode == "REF123"


@pytest.mark.asyncio
async def test_read_timeout_at_the_request_step_is_never_re_requested(monkeypatch, no_sleep):
    """
    A read timeout means IBKR accepted the request and did not answer in time — a
    generation may be running, so re-issuing SendRequest is the 1025 mistake. Same
    treatment as a 1001, on the patient budget too.
    """
    import requests

    calls = _always_raise(monkeypatch, requests.exceptions.ReadTimeout("slow answer"))

    with pytest.raises(RuntimeError, match="1025"):
        await IBKRService(token='t', query_id='q').fetch_flex_data(
            retry_delays=FLEX_RETRY_DELAYS_PATIENT
        )

    assert calls['n'] == 1
    assert no_sleep == []


@pytest.mark.asyncio
async def test_connect_timeout_before_a_reference_exists_is_still_retried(monkeypatch, no_sleep):
    """A connect timeout never reached IBKR; it keeps the DNS-blip treatment."""
    import requests

    calls = _always_raise(monkeypatch, requests.exceptions.ConnectTimeout("no route"))

    with pytest.raises(requests.exceptions.ConnectTimeout):
        await IBKRService(token='t', query_id='q').fetch_flex_data()

    assert calls['n'] == len(_FLEX_RETRY_DELAYS) + 1
    assert no_sleep == _FLEX_RETRY_DELAYS


def test_the_request_step_never_goes_through_ibflex_request_statement():
    """
    The guard for the whole section: `client.request_statement` re-sends on a timeout,
    so the application must not call it. `client.submit_request` stays legitimate for
    the *poll* step, where a repeat retrieves the same reference.
    """
    import ast
    from pathlib import Path

    tree = ast.parse(Path(ibkr_module.__file__).read_text(encoding="utf-8"))
    # Call sites only — the name is allowed in prose, where the docstrings explain why.
    offenders = [
        node.lineno for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "request_statement"
    ]
    assert offenders == [], (
        f"ibkr_service.py:{offenders} calls request_statement — SendRequest must be issued "
        "by send_flex_request, one GET; client.request_statement re-sends on a timeout"
    )
    assert callable(getattr(ibkr_module, "send_flex_request", None))
