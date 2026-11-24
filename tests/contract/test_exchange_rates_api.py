"""Contract tests for specs/exchange-rate-query, through the real ASGI app and database."""

import json
from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest
import respx
from sqlalchemy import update

from rates.adapters.db.session import create_engine
from rates.adapters.db.tables import currency_pairs
from rates.api.app import create_app
from rates.clock import FixedClock
from rates.domain.models import BOI_TIMEZONE
from rates.services.ingestion import IngestionService
from rates.settings import Settings
from tests.fixtures import boi_fixture

PROBLEM = "application/problem+json"


def il(*args) -> datetime:
    return datetime(*args, tzinfo=BOI_TIMEZONE).astimezone(UTC)


class FixtureSource:
    def __init__(self, name: str) -> None:
        self._name = name

    async def fetch(self):
        return json.loads(boi_fixture(self._name), parse_float=Decimal)


async def ingest(engine, fixture="snapshot_2026-11-23.json", at=il(2026, 11, 23, 15, 25)):
    await IngestionService(engine, FixtureSource(fixture), FixedClock(at)).run_once()


@pytest.fixture
async def make_client():
    """Factory: an HTTP client over the app, with an injected engine and clock."""
    stack = []

    async def factory(engine, now: datetime):
        clock = FixedClock(now)
        app = create_app(settings=Settings(_env_file=None), engine=engine, clock=clock)
        lifespan = app.router.lifespan_context(app)
        await lifespan.__aenter__()
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
        stack.append((lifespan, client))
        return client

    yield factory
    for lifespan, client in reversed(stack):
        await client.aclose()
        await lifespan.__aexit__(None, None, None)


async def get(client, query: str) -> httpx.Response:
    return await client.get(f"/api/v1/exchange-rates?{query}")


# --- Successful reads -------------------------------------------------------

async def test_eligible_read(engine, make_client):
    await ingest(engine)
    client = await make_client(engine, il(2026, 11, 24, 10, 0))

    response = await get(client, "base=USD&quote=ILS")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    body = response.json()
    assert body["baseCurrency"] == "USD" and body["quoteCurrency"] == "ILS"
    assert body["rate"] == "3.343"
    assert body["publicationDate"] == "2026-11-23"
    assert body["eligibility"]["status"] == "ELIGIBLE"
    assert body["eligibility"]["policyVersion"] == 1
    assert body["eligibility"]["requiredPublicationDate"] == "2026-11-23"
    assert body["eligibility"]["evaluatedAt"].endswith("Z")
    assert body["retrievedAt"].endswith("Z")
    assert body["source"]["name"] == "BANK_OF_ISRAEL"
    assert body["source"]["type"] == "REPRESENTATIVE"
    assert body["source"]["rate"] == "3.343" and body["source"]["unit"] == 1


async def test_not_eligible_rate_is_returned_with_200(engine, make_client):
    await ingest(engine)
    client = await make_client(engine, il(2026, 11, 24, 16, 1))

    response = await get(client, "base=USD&quote=ILS")

    assert response.status_code == 200
    body = response.json()
    assert body["eligibility"]["status"] == "NOT_ELIGIBLE"
    assert body["eligibility"]["reason"] == "PUBLICATION_OVERDUE"
    assert body["eligibility"]["requiredPublicationDate"] == "2026-11-24"
    assert body["publicationDate"] == "2026-11-23"
    assert body["rate"] == "3.343"


async def test_lowercase_codes_and_stable_rate_id(engine, make_client):
    await ingest(engine)
    client = await make_client(engine, il(2026, 11, 24, 10, 0))

    upper = (await get(client, "base=USD&quote=ILS")).json()
    lower = (await get(client, "base=usd&quote=ils")).json()

    assert upper == lower
    assert upper["rateId"]


async def test_reads_never_contact_boi(engine, make_client):
    await ingest(engine)
    client = await make_client(engine, il(2026, 11, 24, 10, 0))
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        response = await get(client, "base=EUR&quote=ILS")
    assert response.status_code == 200
    assert router.calls.call_count == 0


# --- Errors -------------------------------------------------------------------

@pytest.mark.parametrize("query", ["base=USD", "quote=ILS", "base=US1&quote=ILS", "base=USDX&quote=ILS", ""])
async def test_invalid_request(engine, make_client, query):
    client = await make_client(engine, il(2026, 11, 24, 10, 0))
    response = await get(client, query)
    assert response.status_code == 400
    assert response.headers["content-type"] == PROBLEM
    body = response.json()
    assert body["code"] == "INVALID_REQUEST"
    assert set(body) == {"type", "title", "status", "detail", "code"}
    assert body["status"] == 400


async def test_unsupported_and_disabled_pairs(engine, make_client):
    await ingest(engine)
    async with engine.begin() as conn:
        await conn.execute(update(currency_pairs).where(currency_pairs.c.base_currency == "GBP").values(enabled=False))
    client = await make_client(engine, il(2026, 11, 24, 10, 0))

    for query in ("base=ILS&quote=USD", "base=GBP&quote=ILS", "base=USD&quote=EUR"):
        response = await get(client, query)
        assert response.status_code == 422, query
        assert response.headers["content-type"] == PROBLEM
        assert response.json()["code"] == "UNSUPPORTED_CURRENCY_PAIR"


async def test_rate_not_found_before_first_ingestion(engine, make_client):
    client = await make_client(engine, il(2026, 11, 24, 10, 0))
    response = await get(client, "base=EUR&quote=ILS")
    assert response.status_code == 404
    assert response.json()["code"] == "RATE_NOT_FOUND"


async def test_policy_not_evaluable_returns_no_rate(engine, make_client):
    await ingest(engine)
    client = await make_client(engine, il(2028, 1, 4, 10, 0))  # no 2028 calendar loaded

    response = await get(client, "base=USD&quote=ILS")

    assert response.status_code == 503
    assert response.headers["content-type"] == PROBLEM
    body = response.json()
    assert body["code"] == "POLICY_NOT_EVALUABLE"
    assert "rate" not in body


async def test_storage_unavailable(tmp_path, make_client):
    missing = (tmp_path / "no-such-dir" / "rates.db").as_posix()
    broken = create_engine(f"sqlite+aiosqlite:///{missing}")
    try:
        client = await make_client(broken, il(2026, 11, 24, 10, 0))
        response = await get(client, "base=USD&quote=ILS")
        ready = await client.get("/health/ready")
    finally:
        await broken.dispose()

    assert response.status_code == 503
    assert response.json()["code"] == "STORAGE_UNAVAILABLE"
    assert ready.status_code == 503
    assert ready.headers["content-type"] == PROBLEM


async def test_unknown_route_is_problem(engine, make_client):
    client = await make_client(engine, il(2026, 11, 24, 10, 0))
    response = await client.get("/nope")
    assert response.status_code == 404
    assert response.headers["content-type"] == PROBLEM


# --- Health and OpenAPI -------------------------------------------------------

async def test_health(engine, make_client):
    client = await make_client(engine, il(2026, 11, 24, 10, 0))
    assert (await client.get("/health/live")).json() == {"status": "ok"}
    assert (await client.get("/health/ready")).json()["latestRetrievedAt"] is None

    await ingest(engine)
    ready = (await client.get("/health/ready")).json()
    assert ready["status"] == "ready"
    assert ready["latestRetrievedAt"] == "2026-11-23T13:25:00Z"


async def test_openapi_marks_eligibility_required(engine, make_client):
    client = await make_client(engine, il(2026, 11, 24, 10, 0))
    spec = (await client.get("/openapi.json")).json()
    schemas = spec["components"]["schemas"]

    rate_schema = next(s for s in schemas.values() if "rateId" in s.get("properties", {}))
    assert "eligibility" in rate_schema["required"]
    assert rate_schema["properties"]["rate"]["type"] == "string"

    eligibility_schema = next(s for s in schemas.values() if "policyVersion" in s.get("properties", {}))
    assert "status" in eligibility_schema["required"]

    responses = spec["paths"]["/api/v1/exchange-rates"]["get"]["responses"]
    assert PROBLEM in responses["503"]["content"]
    assert "Problem" in schemas
