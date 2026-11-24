from decimal import Decimal

import httpx
import pytest
import respx
from tenacity import wait_none

from rates.adapters.boi.client import BoiClient, BoiFetchError
from rates.domain.errors import SnapshotRejected
from tests.fixtures import boi_fixture

URL = "https://boi.test/PublicApi/GetExchangeRates"


def client(max_attempts: int = 3) -> BoiClient:
    return BoiClient(URL, timeout_seconds=1, max_attempts=max_attempts, wait=wait_none())


@respx.mock
async def test_success_parses_numbers_as_decimal():
    respx.get(URL).respond(200, content=boi_fixture("snapshot_2026-11-23.json"))

    payload = await client().fetch()

    usd = next(e for e in payload["exchangeRates"] if e["key"] == "USD")
    assert isinstance(usd["currentExchangeRate"], Decimal)
    assert usd["currentExchangeRate"] == Decimal("3.343")
    floats = [v for e in payload["exchangeRates"] for v in e.values() if isinstance(v, float)]
    assert floats == []


@respx.mock
async def test_timeout_is_retried_then_fails():
    route = respx.get(URL).mock(side_effect=httpx.ConnectTimeout("timeout"))
    with pytest.raises(BoiFetchError):
        await client(max_attempts=3).fetch()
    assert route.call_count == 3


@respx.mock
async def test_transient_error_then_success():
    route = respx.get(URL).mock(side_effect=[
        httpx.ConnectError("boom"),
        httpx.Response(503),
        httpx.Response(200, content=boi_fixture("snapshot_2026-11-23.json")),
    ])
    payload = await client().fetch()
    assert route.call_count == 3
    assert "exchangeRates" in payload


@respx.mock
async def test_client_error_not_retried():
    route = respx.get(URL).respond(404)
    with pytest.raises(BoiFetchError):
        await client().fetch()
    assert route.call_count == 1


@respx.mock
async def test_malformed_body_rejected_without_retry():
    route = respx.get(URL).respond(200, content=boi_fixture("malformed.txt"))
    with pytest.raises(SnapshotRejected):
        await client().fetch()
    assert route.call_count == 1
