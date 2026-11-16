"""
A filing's list of files when EDGAR's JSON listing is down (2026-11-16).

`index.json` is EDGAR's directory service; for an hour on 2026-11-16 it answered 503 to most
asks while every document and every filing's `-index.htm` page answered 200. Every filing
read starts at the listing, so after its bounded retries the client reads the index page
and takes the files it links inside the filing's own folder. Offline: a fake SEC.
"""

import httpx
import pytest

from app.services.edgar_client import EdgarClient, EdgarError, filing_url

CIK, ACCN = 1045810, "0001045810-18-000010"
PAGE = f"""<html><body><table>
<tr><td><a href="/Archives/edgar/data/{CIK}/000104581018000010/nvda-2018x10k.htm">10-K</a></td></tr>
<tr><td><a href="/Archives/edgar/data/{CIK}/000104581018000010/nvda-20180128_lab.xml">LAB</a></td></tr>
<tr><td><a href="/Archives/edgar/data/{CIK}/000104581018000010/nvda-20180128.xml">INS</a></td></tr>
<tr><td><a href="/Archives/edgar/data/{CIK}/000104581018000010/nvda-20180128.xml">again</a></td></tr>
<tr><td><a href="/Archives/edgar/data/999/000000000018000001/other-20180128.xml">elsewhere</a></td></tr>
<tr><td><a href="/cgi-bin/browse-edgar?action=getcompany">company</a></td></tr>
</table></body></html>"""


def _client(index_status, page_status=200):
    asked = []

    def handler(request):
        url = str(request.url)
        asked.append(url)
        if url == filing_url(CIK, ACCN, "index.json"):
            return httpx.Response(index_status, json={"directory": {"item": [{"name": "x.xml"}]}})
        if url == filing_url(CIK, ACCN, f"{ACCN}-index.htm"):
            return httpx.Response(page_status, text=PAGE)
        return httpx.Response(404)

    async def no_sleep(_s):
        return None

    return EdgarClient("test agent", transport=httpx.MockTransport(handler),
                       min_interval_s=0, sleep=no_sleep), asked


@pytest.mark.asyncio
async def test_the_json_listing_is_read_when_it_answers():
    client, asked = _client(200)
    async with client:
        assert await client.filing_index(CIK, ACCN) == {"directory": {"item": [{"name": "x.xml"}]}}
    assert len(asked) == 1


@pytest.mark.asyncio
async def test_a_failing_json_listing_falls_back_to_the_index_page():
    client, asked = _client(503)
    async with client:
        listing = await client.filing_index(CIK, ACCN)
    assert [i["name"] for i in listing["directory"]["item"]] == [
        "nvda-2018x10k.htm", "nvda-20180128_lab.xml", "nvda-20180128.xml"]
    assert asked[-1] == filing_url(CIK, ACCN, f"{ACCN}-index.htm")


@pytest.mark.asyncio
async def test_both_failing_is_the_listings_error():
    client, _ = _client(503, page_status=503)
    async with client:
        with pytest.raises(EdgarError, match="index.json"):
            await client.filing_index(CIK, ACCN)


@pytest.mark.asyncio
async def test_a_missing_filing_is_still_none():
    client, asked = _client(404)
    async with client:
        assert await client.filing_index(CIK, ACCN) is None
    assert len(asked) == 1
