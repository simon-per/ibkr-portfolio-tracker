"""
The limiter's client key and what it counts (issue #25).

- The key is the X-Forwarded-For entry the trusted proxy appended — the rightmost, with
  the default `TRUSTED_PROXY_HOPS=1` — and the header is honoured only when the direct
  peer is loopback/private. Everything left of that entry was supplied by the client.
- Logo images (one request per avatar) do not share the bucket with the API calls the
  page needs to render; the manifest still does.
- A full key table counts new keys in one overflow bucket instead of waving them through.
"""
import pytest
from fastapi.testclient import TestClient

from app import rate_limit
from app.config import settings
from app.main import app

# What the backend sees in production: the host nginx reaches the container through the
# loopback-published port, so the peer is the Docker bridge gateway.
PROXY_PEER = "172.17.0.1"
REAL_PEER = "198.51.100.9"   # what the host proxy appended
CLAIMED = "203.0.113.7"      # what the client sent
# A peer reaching the backend directly. Not a documentation range: Python counts those
# as private, which would make it look like a proxy.
PUBLIC_PEER = "1.2.3.4"


@pytest.fixture
def client(monkeypatch):
    rate_limit.reset()
    monkeypatch.setattr(settings, "api_admin_token", "", raising=False)
    monkeypatch.setattr(settings, "trusted_proxy_hops", 1, raising=False)
    yield TestClient(app, raise_server_exceptions=False, client=(PROXY_PEER, 50000))
    rate_limit.reset()


def test_the_key_is_the_address_the_proxy_appended(client, monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_per_minute", 1, raising=False)
    first = client.get("/api/settings", headers={"X-Forwarded-For": f"{CLAIMED}-a, {REAL_PEER}"})
    second = client.get("/api/settings", headers={"X-Forwarded-For": f"{CLAIMED}-b, {REAL_PEER}"})
    assert first.status_code != 429
    assert second.status_code == 429, "same peer, different claimed first hop: not limited"


def test_a_claimed_first_hop_cannot_spend_another_clients_bucket(client, monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_per_minute", 1, raising=False)
    client.get("/api/settings", headers={"X-Forwarded-For": f"{CLAIMED}, {REAL_PEER}"})
    own = client.get("/api/settings", headers={"X-Forwarded-For": CLAIMED})
    assert own.status_code != 429, "another peer's request spent this client's window"


def test_a_public_peer_is_its_own_key_whatever_it_claims(monkeypatch):
    """No proxy in front means nothing trustworthy wrote the header."""
    rate_limit.reset()
    monkeypatch.setattr(settings, "api_admin_token", "", raising=False)
    monkeypatch.setattr(settings, "rate_limit_per_minute", 1, raising=False)
    direct = TestClient(app, raise_server_exceptions=False, client=(PUBLIC_PEER, 50000))
    direct.get("/api/settings", headers={"X-Forwarded-For": f"{CLAIMED}-a"})
    assert direct.get(
        "/api/settings", headers={"X-Forwarded-For": f"{CLAIMED}-b"}
    ).status_code == 429
    assert set(rate_limit._counters) == {PUBLIC_PEER}
    rate_limit.reset()


def test_the_hop_count_picks_the_entry_from_the_right(client, monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_per_minute", 5, raising=False)
    monkeypatch.setattr(settings, "trusted_proxy_hops", 2, raising=False)
    client.get("/api/settings", headers={"X-Forwarded-For": f"{CLAIMED}, {REAL_PEER}, 10.0.0.2"})
    # Fewer entries than hops: all were appended by trusted proxies, the leftmost is the client.
    client.get("/api/settings", headers={"X-Forwarded-For": "192.0.2.5"})
    monkeypatch.setattr(settings, "trusted_proxy_hops", 0, raising=False)
    client.get("/api/settings", headers={"X-Forwarded-For": CLAIMED})
    assert set(rate_limit._counters) == {REAL_PEER, "192.0.2.5", PROXY_PEER}


def test_logo_images_do_not_spend_the_api_budget(client, monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_per_minute", 3, raising=False)
    headers = {"X-Forwarded-For": REAL_PEER}
    for i in range(3):
        client.get(f"/api/logos/isin/US000000000{i}?v=abc", headers=headers)
        client.get(f"/api/logos/{i + 1}?v=abc", headers=headers)
    assert client.get("/api/settings", headers=headers).status_code != 429


def test_the_logo_manifest_is_still_counted(client, monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_per_minute", 1, raising=False)
    headers = {"X-Forwarded-For": REAL_PEER}
    client.get("/api/logos", headers=headers)
    assert client.get("/api/logos", headers=headers).status_code == 429


def test_a_full_table_does_not_wave_new_clients_through(monkeypatch):
    rate_limit.reset()
    monkeypatch.setattr(rate_limit, "_MAX_TRACKED_CLIENTS", 2)
    now = 1_000.0
    assert rate_limit.check("a", 1, now) == 0
    assert rate_limit.check("b", 1, now) == 0
    assert rate_limit.check("c", 1, now) == 0
    assert rate_limit.check("c", 1, now) != 0, "an untracked key is never limited once full"
    # The overflow bucket is shared: another new key is limited with it.
    assert rate_limit.check("d", 1, now) != 0
    # The next window prunes the closed one, so new keys get their own slots again.
    later = now + rate_limit.WINDOW_SECONDS
    assert rate_limit.check("e", 1, later) == 0
    assert rate_limit.check("f", 1, later) == 0
    rate_limit.reset()
