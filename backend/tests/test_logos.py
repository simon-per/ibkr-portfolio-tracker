"""
Holding logos (docs/logos.md): the refuse-whole check, the bounded logo.dev pass, the
manual import, and the two read routes.

Offline: logo.dev is an `httpx.MockTransport`, the database is in memory, and the pacing
sleep is recorded rather than waited.
"""
import asyncio
import hashlib
from datetime import date, timedelta
from decimal import Decimal

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401  register all mappers
from app.clock import utcnow
from app.config import settings
from app.database import Base, get_db
from app.models.security import Security
from app.models.security_logo import LOGO_SOURCE_LOGO_DEV, LOGO_SOURCE_MANUAL, SecurityLogo
from app.models.sync_run import SyncRun
from app.models.taxlot import TaxLot
from app.repositories.security_logo_repository import SecurityLogoRepository
from app.services import logo_service
from app.services.logo_service import (
    MAX_LOGO_BYTES,
    MAX_LOOKUPS_PER_PASS,
    LogoDevClient,
    LogoDevError,
    LogoRefused,
    LogoService,
    validate_logo,
)

KEY = "sk_test_logo_dev_key_0123456789"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
WEBP = b"RIFF\x24\x00\x00\x00WEBPVP8 " + b"\x00" * 64
SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
SIGNED = "https://img.logo.example/signed/abc?signature=s3cr3t"


# --------------------------------------------------------------------- refuse whole

@pytest.mark.parametrize("data,declared,expected", [
    (PNG, "image/png", "image/png"),
    (JPEG, "image/jpeg; charset=binary", "image/jpeg"),
    (WEBP, "image/webp", "image/webp"),
    (PNG, None, "image/png"),          # a file on disk: the magic bytes decide
], ids=["png", "jpeg", "webp", "file"])
def test_a_whitelisted_image_is_accepted(data, declared, expected):
    assert validate_logo(data, declared) == expected


@pytest.mark.parametrize("data,declared", [
    (SVG, "image/svg+xml"),            # never SVG: same-origin script
    (SVG, None),
    (SVG, "image/png"),                # an SVG claiming to be a PNG
    (JPEG, "image/png"),               # wrong magic for the declared type
    (b"<html></html>", "text/html"),
    (b"", "image/png"),
    (PNG, ""),
], ids=["svg", "svg-file", "svg-as-png", "jpeg-as-png", "html", "empty", "no-type"])
def test_anything_else_is_refused(data, declared):
    with pytest.raises(LogoRefused):
        validate_logo(data, declared)


def test_an_oversized_image_is_refused():
    with pytest.raises(LogoRefused, match="limit"):
        validate_logo(PNG + b"\x00" * MAX_LOGO_BYTES, "image/png")


# --------------------------------------------------------------------- fixtures

@pytest_asyncio.fixture
async def engine():
    eng = create_async_engine(
        "sqlite+aiosqlite:///:memory:", poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def db(engine):
    session = AsyncSession(engine, expire_on_commit=False)
    try:
        yield session
    finally:
        await session.close()


async def _hold(db, n=1, isin_prefix="US", open_=True, start_id=1):
    """`n` securities with an open (or closed) lot each; returns their ISINs."""
    isins = []
    for k in range(start_id, start_id + n):
        isin = f"{isin_prefix}{k:09d}0"
        db.add(Security(id=k, isin=isin, symbol=f"SYM{k}", description=f"Co {k}",
                        currency="USD", conid=1000 + k, asset_category="STK",
                        exchange="NASDAQ"))
        db.add(TaxLot(security_id=k, open_date=date(2026, 1, 2), quantity=Decimal("1"),
                      cost_basis=Decimal("10"), cost_basis_eur=Decimal("10"),
                      price_per_unit=Decimal("10"), currency="USD", is_open=open_,
                      close_date=None if open_ else date(2026, 3, 2)))
        isins.append(isin)
    await db.commit()
    return isins


class FakeLogoDev:
    """A scripted logo.dev: `answers` maps (field, value) -> status; default 404."""

    def __init__(self, answers=None, image=PNG, content_type="image/png",
                 download_status=200, stop_after=None):
        self.answers = answers or {}
        self.image = image
        self.content_type = content_type
        self.download_status = download_status
        self.stop_after = stop_after
        self.lookups = []
        self.downloads = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.logo.dev":
            assert request.headers["authorization"] == f"Bearer {KEY}"
            field, value = next(iter(request.url.params.items()))
            self.lookups.append((field, value))
            if self.stop_after is not None and len(self.lookups) > self.stop_after:
                return httpx.Response(429)
            status = self.answers.get((field, value), 404)
            if status == 200:
                return httpx.Response(200, json={
                    "data": {"object": "image", "status": "ready", "url": SIGNED, "etag": "e1"},
                    "metadata": {"request_id": "r1"},
                })
            return httpx.Response(status, json={"error": "x"})
        # The signed download: another host, and the key must never travel there.
        assert "authorization" not in request.headers
        self.downloads.append(str(request.url))
        return httpx.Response(self.download_status, content=self.image,
                              headers={"content-type": self.content_type})

    @property
    def transport(self):
        return httpx.MockTransport(self.handler)


@pytest.fixture
def keyed(monkeypatch):
    monkeypatch.setattr(settings, "logo_dev_secret_key", KEY, raising=False)


def _service(db, fake, sleeps=None):
    async def sleep(seconds):
        if sleeps is not None:
            sleeps.append(seconds)
    return LogoService(db, transport=fake.transport, sleep=sleep)


# --------------------------------------------------------------------- the pass

@pytest.mark.asyncio
async def test_no_key_means_skipped_with_no_warning_and_no_request(db):
    await _hold(db)
    fake = FakeLogoDev()
    result = await _service(db, fake).refresh()
    assert result == {"status": "skipped", "message": "skipped: not configured"}
    assert fake.lookups == [] and fake.downloads == []


@pytest.mark.asyncio
async def test_a_found_logo_is_downloaded_validated_and_stored(db, keyed):
    [isin] = await _hold(db)
    fake = FakeLogoDev({("isin", isin): 200})
    sleeps = []
    result = await _service(db, fake, sleeps).refresh()

    assert result["stored"] == 1 and "warnings" not in result
    assert fake.lookups == [("isin", isin)]
    # Asked at 128 px PNG, the signature kept.
    assert "size=128" in fake.downloads[0] and "format=png" in fake.downloads[0]
    assert "signature=s3cr3t" in fake.downloads[0]
    # Paced: the download waited for the lookup.
    assert sleeps and all(0 < s <= logo_service.MIN_INTERVAL_S for s in sleeps)
    row = (await db.execute(select(SecurityLogo))).scalar_one()
    assert row.image == PNG and row.content_type == "image/png"
    assert row.sha256 == hashlib.sha256(PNG).hexdigest()
    assert row.source == LOGO_SOURCE_LOGO_DEV and row.fetched_at and row.checked_at


@pytest.mark.asyncio
async def test_an_unknown_isin_falls_back_to_the_price_paths_ticker(db, keyed):
    [isin] = await _hold(db)
    fake = FakeLogoDev({("ticker", "SYM1"): 200})
    result = await _service(db, fake).refresh()
    assert fake.lookups == [("isin", isin), ("ticker", "SYM1")]
    assert result["stored"] == 1


@pytest.mark.asyncio
async def test_sold_securities_are_not_asked_about(db, keyed):
    await _hold(db, open_=False)
    fake = FakeLogoDev()
    result = await _service(db, fake).refresh()
    assert fake.lookups == [] and result["message"] == "Logos: nothing due"


@pytest.mark.asyncio
async def test_a_pass_makes_at_most_thirty_lookups(db, keyed):
    await _hold(db, n=40)
    fake = FakeLogoDev()           # every ISIN and ticker unknown: two lookups each
    result = await _service(db, fake).refresh()
    assert len(fake.lookups) == MAX_LOOKUPS_PER_PASS == result["lookups"]
    assert result["not_found"] == MAX_LOOKUPS_PER_PASS // 2
    assert result["deferred"] == 40 - MAX_LOOKUPS_PER_PASS // 2


@pytest.mark.asyncio
async def test_a_429_stops_the_pass_and_names_it(db, keyed):
    await _hold(db, n=5)
    fake = FakeLogoDev(stop_after=2)
    result = await _service(db, fake).refresh()
    assert len(fake.lookups) == 3            # the third was refused, nothing after it
    assert result["stopped"] == 429
    assert any("HTTP 429" in w for w in result["warnings"])
    # Only the one fully answered ISIN is remembered; the rest wait for the next pass.
    assert len((await db.execute(select(SecurityLogo))).scalars().all()) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 402, 403])
async def test_a_refused_key_or_spent_credits_stop_the_pass(db, keyed, status):
    isins = await _hold(db, n=3)
    fake = FakeLogoDev({("isin", isins[0]): status})
    result = await _service(db, fake).refresh()
    assert len(fake.lookups) == 1 and result["stopped"] == status


@pytest.mark.asyncio
async def test_checked_at_blocks_a_re_ask_until_ninety_days(db, keyed, monkeypatch):
    await _hold(db)
    fake = FakeLogoDev()
    await _service(db, fake).refresh()
    assert len(fake.lookups) == 2

    await _service(db, fake).refresh()
    assert len(fake.lookups) == 2            # asked, nothing there: not asked again

    later = utcnow() + timedelta(days=91)
    monkeypatch.setattr(logo_service, "utcnow", lambda: later)
    await _service(db, fake).refresh()
    assert len(fake.lookups) == 4


@pytest.mark.asyncio
async def test_not_indexed_leaves_the_isin_for_the_next_pass_without_waiting(db, keyed):
    [isin] = await _hold(db)
    fake = FakeLogoDev({("isin", isin): 202})
    sleeps = []
    result = await _service(db, fake, sleeps).refresh()
    assert result["not_indexed"] == 1 and fake.lookups == [("isin", isin)]
    assert sleeps == []                      # never sleeps on Retry-After
    assert (await db.execute(select(SecurityLogo))).first() is None
    await _service(db, fake).refresh()
    assert len(fake.lookups) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("image,content_type", [
    (SVG, "image/svg+xml"),
    (JPEG, "image/png"),
    (PNG + b"\x00" * MAX_LOGO_BYTES, "image/png"),
], ids=["svg", "wrong-magic", "oversize"])
async def test_a_refused_image_stores_nothing_and_is_not_rebought(db, keyed, image, content_type):
    [isin] = await _hold(db)
    fake = FakeLogoDev({("isin", isin): 200}, image=image, content_type=content_type)
    result = await _service(db, fake).refresh()
    assert result["refused"] == 1 and result["stored"] == 0
    assert any("refused" in w for w in result["warnings"])
    row = (await db.execute(select(SecurityLogo))).scalar_one()
    assert row.image is None and row.checked_at is not None
    await _service(db, fake).refresh()
    assert len(fake.lookups) == 1


@pytest.mark.asyncio
async def test_a_failed_download_is_retried_next_pass(db, keyed):
    [isin] = await _hold(db)
    fake = FakeLogoDev({("isin", isin): 200}, download_status=500)
    result = await _service(db, fake).refresh()
    assert result["errors"] == 1 and result["warnings"]
    assert (await db.execute(select(SecurityLogo))).first() is None
    await _service(db, fake).refresh()
    assert len(fake.lookups) == 2


@pytest.mark.asyncio
async def test_a_stored_logo_survives_a_later_not_found(db, keyed, monkeypatch):
    [isin] = await _hold(db)
    await _service(db, FakeLogoDev({("isin", isin): 200})).refresh()
    later = utcnow() + timedelta(days=91)
    monkeypatch.setattr(logo_service, "utcnow", lambda: later)
    await _service(db, FakeLogoDev()).refresh()
    row = (await db.execute(select(SecurityLogo))).scalar_one()
    assert row.image == PNG and row.checked_at == later


@pytest.mark.asyncio
async def test_a_manual_logo_is_never_asked_about_nor_overwritten(db, keyed):
    [isin] = await _hold(db)
    repo = SecurityLogoRepository(db)
    await repo.store(isin, JPEG, "image/jpeg", LOGO_SOURCE_MANUAL, utcnow())
    await db.commit()

    fake = FakeLogoDev({("isin", isin): 200})
    await _service(db, fake).refresh(force=True)
    assert fake.lookups == []

    # Even a write already in flight cannot replace it.
    assert await repo.store(isin, PNG, "image/png", LOGO_SOURCE_LOGO_DEV, utcnow()) is False
    await repo.mark_checked(isin, LOGO_SOURCE_LOGO_DEV, utcnow())
    await db.commit()
    db.expire_all()
    row = (await db.execute(select(SecurityLogo))).scalar_one()
    assert row.image == JPEG and row.source == LOGO_SOURCE_MANUAL


@pytest.mark.asyncio
async def test_a_dry_run_makes_no_request_and_writes_nothing(db, keyed):
    await _hold(db, n=2)
    fake = FakeLogoDev()
    result = await _service(db, fake).refresh(dry_run=True)
    assert result["due"] == 2 and len(result["outcomes"]) == 2
    assert fake.lookups == []
    assert (await db.execute(select(SecurityLogo))).first() is None


# --------------------------------------------------------------------- the CLIs

@pytest.fixture
def cli_sessions(engine, monkeypatch):
    factory = async_sessionmaker(engine, expire_on_commit=False)
    import app.cli.fetch_logos as fetch_cli
    import app.cli.import_logo as import_cli
    monkeypatch.setattr(import_cli, "AsyncSessionLocal", factory)
    monkeypatch.setattr(fetch_cli, "AsyncSessionLocal", factory)
    return factory


async def _runs(db, sync_type):
    return (await db.execute(select(SyncRun).where(SyncRun.sync_type == sync_type))).scalars().all()


@pytest.mark.asyncio
async def test_import_logo_stores_a_manual_row_and_records_a_run(db, cli_sessions, tmp_path):
    from app.cli.import_logo import import_logo
    [isin] = await _hold(db)
    path = tmp_path / "logo.png"
    path.write_bytes(PNG)

    assert await import_logo(isin, path, dry_run=True) == 0
    assert (await db.execute(select(SecurityLogo))).first() is None
    assert await _runs(db, "manual_logo_import") == []

    assert await import_logo(isin.lower(), path) == 0
    row = (await db.execute(select(SecurityLogo))).scalar_one()
    assert row.source == LOGO_SOURCE_MANUAL and row.image == PNG
    [run] = await _runs(db, "manual_logo_import")
    assert run.status == "success"


@pytest.mark.asyncio
@pytest.mark.parametrize("content,isin", [
    (SVG, None),                                 # never SVG
    (PNG + b"\x00" * MAX_LOGO_BYTES, None),      # oversize
    (PNG, "US0000000000"),                       # no such security
    (PNG, "NOT-AN-ISIN"),
], ids=["svg", "oversize", "unknown-isin", "not-an-isin"])
async def test_import_logo_refuses_whole(db, cli_sessions, tmp_path, content, isin):
    from app.cli.import_logo import import_logo
    [held_isin] = await _hold(db)
    path = tmp_path / "logo.png"
    path.write_bytes(content)
    assert await import_logo(isin or held_isin, path) == 1
    assert (await db.execute(select(SecurityLogo))).first() is None


@pytest.mark.asyncio
async def test_fetch_logos_records_a_run_and_its_dry_run_does_not(db, cli_sessions, keyed,
                                                                  monkeypatch):
    import app.cli.fetch_logos as fetch_cli
    [isin] = await _hold(db)
    fake = FakeLogoDev({("isin", isin): 200})
    monkeypatch.setattr(fetch_cli, "LogoService",
                        lambda session: _service(session, fake))

    assert await fetch_cli.fetch_logos(dry_run=True) == 0
    assert fake.lookups == [] and await _runs(db, "manual_logo_fetch") == []

    assert await fetch_cli.fetch_logos() == 0
    [run] = await _runs(db, "manual_logo_fetch")
    assert run.status == "success" and run.details["stored"] == 1


@pytest.mark.asyncio
async def test_fetch_logos_without_a_key_does_nothing(db, cli_sessions):
    import app.cli.fetch_logos as fetch_cli
    await _hold(db)
    assert await fetch_cli.fetch_logos() == 1
    assert await _runs(db, "manual_logo_fetch") == []


# --------------------------------------------------------------------- the routes

@pytest.fixture
def client():
    from app.routers import logos

    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:", poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    session = AsyncSession(engine, expire_on_commit=False)
    loop = asyncio.new_event_loop()

    async def _setup():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        # Two listings of one ISIN share a logo; a third security has none.
        for sid, isin, exchange in ((1, "NL0010273215", "NASDAQ"), (2, "NL0010273215", "AEB"),
                                    (3, "US0378331005", "NASDAQ")):
            session.add(Security(id=sid, isin=isin, symbol="X", description="X",
                                 currency="EUR", conid=sid, exchange=exchange))
        await SecurityLogoRepository(session).store(
            "NL0010273215", PNG, "image/png", LOGO_SOURCE_LOGO_DEV, utcnow())
        # Asked, nothing there: not in the manifest.
        await SecurityLogoRepository(session).mark_checked(
            "US0378331005", LOGO_SOURCE_LOGO_DEV, utcnow())
        await session.commit()

    loop.run_until_complete(_setup())
    api = FastAPI()
    api.include_router(logos.router, prefix="/api/logos")

    async def _override():
        yield session

    api.dependency_overrides[get_db] = _override
    yield TestClient(api)
    loop.run_until_complete(session.close())
    loop.run_until_complete(engine.dispose())
    loop.close()


VERSION = hashlib.sha256(PNG).hexdigest()[:12]


def test_the_manifest_lists_every_listing_with_an_image(client):
    assert client.get("/api/logos").json() == {"logos": {"1": VERSION, "2": VERSION}}


def test_a_versioned_image_is_served_immutable_and_unsniffable(client):
    r = client.get(f"/api/logos/2?v={VERSION}")
    assert r.status_code == 200 and r.content == PNG
    assert r.headers["content-type"] == "image/png"
    assert r.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["etag"] == f'"{hashlib.sha256(PNG).hexdigest()}"'


def test_a_stale_version_is_served_but_never_cached_for_good(client):
    r = client.get("/api/logos/1?v=000000000000")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-cache"


def test_a_matching_etag_answers_304(client):
    etag = client.get("/api/logos/1").headers["etag"]
    r = client.get("/api/logos/1", headers={"If-None-Match": etag})
    assert r.status_code == 304 and r.content == b""


@pytest.mark.parametrize("security_id", [3, 99])
def test_no_logo_is_a_404(client, security_id):
    assert client.get(f"/api/logos/{security_id}").status_code == 404


# --------------------------------------------------------------------- the reply shape

# logo.dev's documented 200 for `GET /v2/brands/logo` (its v2 reference, 2026-10-17): the
# image sits in a `{data, metadata}` envelope. The first live pass read a top-level `url`,
# found none, and stored nothing for 25 brands it had found.
DOCUMENTED_LOOKUP = {
    "data": {
        "object": "image", "kind": "logo", "brand_id": "brand_x", "status": "ready",
        "url": "https://img.logo.dev/assets/SEALED_TOKEN", "etag": "9f2c1e7a4b8d3f60",
        "source": {"width": 512, "height": 512, "format": "svg"},
        "expires_at": "2026-10-18T13:00:00Z",
    },
    "metadata": {"request_id": "r", "resolution": {"type": "isin", "value": "US0000000001"}},
}


@pytest.mark.asyncio
@pytest.mark.parametrize("body, expected", [
    (DOCUMENTED_LOOKUP, ("found", "https://img.logo.dev/assets/SEALED_TOKEN")),
    ({"data": {"object": "image", "status": "not_indexed"}}, ("not_indexed", None)),
])
async def test_the_lookup_reads_the_documented_envelope(body, expected):
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=body))
    async with LogoDevClient(KEY, transport=transport) as client:
        assert await client.lookup("isin", "US0000000001") == expected


@pytest.mark.asyncio
async def test_a_bare_top_level_url_is_not_the_documented_shape():
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json={"url": "https://img.logo.dev/x"})
    )
    async with LogoDevClient(KEY, transport=transport) as client:
        with pytest.raises(LogoDevError):
            await client.lookup("isin", "US0000000001")
