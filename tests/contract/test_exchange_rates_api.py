"""Contract tests for specs/exchange-rate-query, through the real ASGI app and database."""

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import httpx
import jwt
import pytest
import respx
from sqlalchemy import select, update

from rates.adapters.db.repos import SqlAuditRepository, SqlRefreshStatusRepository
from rates.adapters.db.session import create_engine
from rates.adapters.db.tables import audit_events, currency_pairs, non_publication_days, rate_observations
from rates.api.app import create_app
from rates.api.auth import Principal
from rates.clock import FixedClock
from rates.domain.models import BOI_TIMEZONE
from rates.services.ingestion import IngestionService
from rates.settings import Settings
from tests.fixtures import boi_fixture

PROBLEM = "application/problem+json"
JWT_SECRET = "contract-test-secret-0123456789abcdef"
DEMO_USER = "demo"
DEMO_PASSWORD = "demo-password"


def auth_settings() -> Settings:
    return Settings(_env_file=None, jwt_secret=JWT_SECRET, demo_password=DEMO_PASSWORD)


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

    async def factory(engine, now: datetime, authenticated: bool = True):
        """``authenticated``: send a valid demo-user bearer token on every request."""
        clock = FixedClock(now)
        app = create_app(settings=auth_settings(), engine=engine, clock=clock)
        lifespan = app.router.lifespan_context(app)
        await lifespan.__aenter__()
        headers = {}
        if authenticated:
            token = app.state.token_authority.issue(Principal(DEMO_USER)).access_token
            headers["Authorization"] = f"Bearer {token}"
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test", headers=headers
        )
        client.app = app  # for tests that need the clock or the authority
        client.clock = clock
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


async def test_calendar_gap_returns_policy_not_evaluable(engine, make_client):
    # Implausible calendar: every day 2026-11-17..26 listed as non-publication.
    await ingest(engine, "snapshot_2026-11-25.json", at=il(2026, 11, 25, 15, 25))
    async with engine.begin() as conn:
        await conn.execute(
            non_publication_days.insert(),
            [
                {
                    "day": date(2026, 11, 17) + timedelta(days=i),
                    "reason": "test: wrong calendar",
                    "origin": "OPERATOR",
                    "created_by": "test",
                    "created_at": il(2026, 11, 26, 9, 0),
                }
                for i in range(10)
            ],
        )
    client = await make_client(engine, il(2026, 11, 26, 18, 0))

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


async def test_readiness_reports_last_successful_ingestion(engine, make_client):
    client = await make_client(engine, il(2026, 11, 24, 11, 0))
    ready = await client.get("/health/ready")
    assert ready.status_code == 200
    assert ready.json()["lastSuccessfulIngestionAt"] is None  # no success yet

    await ingest(engine)  # stored at 2026-11-23 13:25Z
    async with engine.begin() as conn:  # a later successful poll that found nothing new
        await SqlRefreshStatusRepository(conn).record_success(il(2026, 11, 24, 11, 0))
        await SqlRefreshStatusRepository(conn).record_failure(il(2026, 11, 24, 11, 5), "boi_unavailable", 1)

    ready = (await client.get("/health/ready")).json()
    assert ready["lastSuccessfulIngestionAt"] == "2026-11-24T09:00:00Z"
    assert ready["latestRetrievedAt"] == "2026-11-23T13:25:00Z"


async def test_readiness_lists_only_the_overdue_pair(engine, make_client):
    await ingest(engine, "usd_late_2026-11-24.json", at=il(2026, 11, 24, 15, 25))
    client = await make_client(engine, il(2026, 11, 24, 16, 30))  # after Tuesday's 16:00 deadline

    ready = await client.get("/health/ready")

    assert ready.status_code == 200
    assert ready.json()["notEligiblePairs"] == [
        {"pair": "USD/ILS", "reason": "PUBLICATION_OVERDUE", "publicationDate": "2026-11-23"}
    ]


async def test_readiness_all_pairs_eligible(engine, make_client):
    await ingest(engine)
    client = await make_client(engine, il(2026, 11, 24, 10, 0))
    assert (await client.get("/health/ready")).json()["notEligiblePairs"] == []


async def test_readiness_fresh_install_lists_rate_not_found(engine, make_client):
    client = await make_client(engine, il(2026, 11, 24, 10, 0))
    pairs = (await client.get("/health/ready")).json()["notEligiblePairs"]
    assert sorted(p["pair"] for p in pairs) == ["EUR/ILS", "GBP/ILS", "USD/ILS"]
    assert {(p["reason"], p["publicationDate"]) for p in pairs} == {("RATE_NOT_FOUND", None)}


async def test_readiness_policy_not_evaluable_lists_every_pair(engine, make_client):
    await ingest(engine)
    client = await make_client(engine, il(2028, 1, 4, 10, 0))  # no 2028 calendar

    ready = await client.get("/health/ready")

    assert ready.status_code == 200
    pairs = ready.json()["notEligiblePairs"]
    assert len(pairs) == 3
    assert {p["reason"] for p in pairs} == {"POLICY_NOT_EVALUABLE"}


@pytest.mark.parametrize(
    ("now", "ends_on", "warning"),
    [
        (il(2026, 11, 24, 10, 0), "2027-12-31", False),
        (il(2027, 11, 2, 10, 0), "2027-12-31", True),   # 59 days left, threshold 60
        (il(2028, 1, 4, 10, 0), None, True),            # current year not loaded
    ],
)
async def test_readiness_reports_calendar_expiry(engine, make_client, now, ends_on, warning):
    client = await make_client(engine, now)
    body = (await client.get("/health/ready")).json()
    assert body["calendarCoverageEndsOn"] == ends_on
    assert body["calendarExpiryWarning"] is warning


async def test_openapi_documents_readiness_fields(engine, make_client):
    client = await make_client(engine, il(2026, 11, 24, 10, 0))
    spec = (await client.get("/openapi.json")).json()
    props = spec["components"]["schemas"]["ReadinessOut"]["properties"]
    assert "lastSuccessfulIngestionAt" in props
    assert props["lastSuccessfulIngestionAt"]["description"]
    assert props["notEligiblePairs"]["description"]
    assert props["calendarExpiryWarning"]["description"]


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


# --- Authentication: token endpoint ---------------------------------------------

async def login(client, username=DEMO_USER, password=DEMO_PASSWORD):
    data = {"username": username, "password": password}
    return await client.post("/api/v1/auth/token", data=data)


async def test_login_issues_bearer_token(engine, make_client, caplog):
    client = await make_client(engine, il(2026, 11, 24, 10, 0), authenticated=False)

    with caplog.at_level("DEBUG"):
        response = await login(client)

    assert response.status_code == 200
    body = response.json()
    assert body["token_type"] == "bearer"
    assert body["expires_in"] == 3600
    claims = jwt.decode(body["access_token"], options={"verify_signature": False})
    assert claims["sub"] == "demo"
    assert DEMO_PASSWORD not in caplog.text
    assert body["access_token"] not in caplog.text


@pytest.mark.parametrize(("username", "password"), [(DEMO_USER, "wrong"), ("mallory", DEMO_PASSWORD)])
async def test_login_rejects_bad_credentials(engine, make_client, caplog, username, password):
    client = await make_client(engine, il(2026, 11, 24, 10, 0), authenticated=False)

    with caplog.at_level("DEBUG"):
        response = await login(client, username, password)

    assert response.status_code == 401
    assert response.headers["content-type"] == PROBLEM
    assert response.json()["code"] == "INVALID_CREDENTIALS"
    assert "access_token" not in response.json()
    assert password not in caplog.text


async def test_login_missing_password_is_invalid_request(engine, make_client):
    client = await make_client(engine, il(2026, 11, 24, 10, 0), authenticated=False)
    response = await client.post("/api/v1/auth/token", data={"username": DEMO_USER})
    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_REQUEST"
    assert "ISO 4217" not in response.json()["detail"]


# --- Authentication: protected rates endpoint ------------------------------------

async def test_read_without_token_is_401(engine, make_client):
    await ingest(engine)
    client = await make_client(engine, il(2026, 11, 24, 10, 0), authenticated=False)

    response = await get(client, "base=USD&quote=ILS")

    assert response.status_code == 401
    assert response.headers["content-type"] == PROBLEM
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json()["code"] == "AUTHENTICATION_REQUIRED"
    assert "rate" not in response.json()


async def test_authentication_is_checked_before_parameters(engine, make_client):
    client = await make_client(engine, il(2026, 11, 24, 10, 0), authenticated=False)
    response = await get(client, "base=US1")
    assert response.status_code == 401


async def test_expired_token_is_401_invalid_token(engine, make_client):
    await ingest(engine)
    client = await make_client(engine, il(2026, 11, 24, 10, 0))  # token issued at 10:00, 1 h lifetime
    client.clock.set(il(2026, 11, 24, 11, 0, 1))

    response = await get(client, "base=USD&quote=ILS")

    assert response.status_code == 401
    assert response.json()["code"] == "INVALID_TOKEN"
    assert response.headers["www-authenticate"] == "Bearer"


async def test_tampered_token_is_401_invalid_token(engine, make_client):
    await ingest(engine)
    client = await make_client(engine, il(2026, 11, 24, 10, 0), authenticated=False)
    forged = jwt.encode(
        {"sub": "demo", "iat": 0, "exp": 4_000_000_000, "iss": "rates-api", "aud": "rates-api"},
        "attacker-secret-0123456789abcdefghij",
        algorithm="HS256",
    )

    response = await client.get(
        "/api/v1/exchange-rates?base=USD&quote=ILS", headers={"Authorization": f"Bearer {forged}"}
    )

    assert response.status_code == 401
    assert response.json()["code"] == "INVALID_TOKEN"


async def test_token_from_login_works_for_reads(engine, make_client):
    await ingest(engine)
    client = await make_client(engine, il(2026, 11, 24, 10, 0), authenticated=False)
    token = (await login(client)).json()["access_token"]

    response = await client.get(
        "/api/v1/exchange-rates?base=USD&quote=ILS", headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == 200
    assert response.json()["rate"] == "3.343"


@pytest.mark.parametrize("path", ["/health/live", "/health/ready", "/openapi.json", "/docs"])
async def test_operational_endpoints_stay_open(engine, make_client, path):
    client = await make_client(engine, il(2026, 11, 24, 10, 0), authenticated=False)
    assert (await client.get(path)).status_code == 200


async def test_openapi_declares_bearer_security(engine, make_client):
    client = await make_client(engine, il(2026, 11, 24, 10, 0), authenticated=False)
    spec = (await client.get("/openapi.json")).json()

    schemes = spec["components"]["securitySchemes"]
    (name, scheme), = schemes.items()
    assert scheme["type"] == "oauth2"
    assert scheme["flows"]["password"]["tokenUrl"] == "/api/v1/auth/token"

    op = spec["paths"]["/api/v1/exchange-rates"]["get"]
    assert {name: []} in op["security"]
    assert "401" in op["responses"]
    assert "AUDIT_UNAVAILABLE" in op["responses"]["503"]["description"]
    assert "/api/v1/auth/token" in spec["paths"]
    assert "security" not in spec["paths"]["/health/ready"]["get"]


# --- Rate-query audit -------------------------------------------------------------

async def audit_rows(engine):
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                select(audit_events)
                .where(audit_events.c.event_type == "RATE_QUERIED")
                .order_by(audit_events.c.occurred_at)
            )
        ).mappings().all()
    return [dict(r) for r in rows]


async def test_successful_read_is_audited_with_what_the_user_was_told(engine, make_client):
    await ingest(engine)
    client = await make_client(engine, il(2026, 11, 24, 16, 30))  # USD overdue: NOT_ELIGIBLE

    response = await get(client, "base=usd&quote=ils")

    assert response.status_code == 200
    body = response.json()
    (row,) = await audit_rows(engine)
    assert row["actor"] == "demo"
    assert row["entity_type"] == "rate_observation"
    assert row["entity_id"] == body["rateId"]
    assert row["after"]["request"] == {"base": "usd", "quote": "ils"}  # as sent
    recorded = row["after"]["response"]
    assert recorded["status"] == 200
    for field in ("rateId", "rate", "publicationDate", "eligibility"):
        assert recorded[field] == body[field]
    assert recorded["eligibility"]["status"] == "NOT_ELIGIBLE"
    async with engine.connect() as conn:
        ids = {str(i) for i in (await conn.execute(select(rate_observations.c.id))).scalars()}
    assert recorded["rateId"] in ids


async def test_audit_failure_withholds_the_rate(engine, make_client, monkeypatch, caplog):
    await ingest(engine)
    client = await make_client(engine, il(2026, 11, 24, 10, 0))

    async def broken(self, event):
        raise RuntimeError("audit store down")

    monkeypatch.setattr(SqlAuditRepository, "record", broken)
    with caplog.at_level("ERROR"):
        response = await get(client, "base=USD&quote=ILS")

    assert response.status_code == 503
    assert response.headers["content-type"] == PROBLEM
    assert response.json()["code"] == "AUDIT_UNAVAILABLE"
    assert "rate" not in response.json()
    assert "audit_unavailable" in caplog.text


@pytest.mark.parametrize(
    ("query", "now", "status", "code", "request_params"),
    [
        ("base=ILS&quote=USD", il(2026, 11, 24, 10, 0), 422, "UNSUPPORTED_CURRENCY_PAIR",
         {"base": "ILS", "quote": "USD"}),
        ("base=USD", il(2026, 11, 24, 10, 0), 400, "INVALID_REQUEST", {"base": "USD", "quote": None}),
        ("base=USD&quote=ILS", il(2028, 1, 4, 10, 0), 503, "POLICY_NOT_EVALUABLE",
         {"base": "USD", "quote": "ILS"}),
    ],
    ids=["422", "400", "503-policy"],
)
async def test_rejected_reads_are_audited(engine, make_client, query, now, status, code, request_params):
    await ingest(engine)
    client = await make_client(engine, now)

    response = await get(client, query)

    assert response.status_code == status
    (row,) = await audit_rows(engine)
    assert row["actor"] == "demo"
    assert row["entity_type"] == "rate_query"
    assert row["after"]["request"] == request_params
    assert row["after"]["response"] == {"status": status, "code": code, "rateId": None}


async def test_fresh_install_404_is_audited(engine, make_client):
    client = await make_client(engine, il(2026, 11, 24, 10, 0))
    assert (await get(client, "base=EUR&quote=ILS")).status_code == 404
    (row,) = await audit_rows(engine)
    assert row["after"]["response"]["code"] == "RATE_NOT_FOUND"


async def test_unauthenticated_reads_are_not_audited(engine, make_client, caplog):
    await ingest(engine)
    client = await make_client(engine, il(2026, 11, 24, 10, 0), authenticated=False)

    with caplog.at_level("INFO"):
        assert (await get(client, "base=USD&quote=ILS")).status_code == 401

    assert await audit_rows(engine) == []
    assert "auth.rejected code=AUTHENTICATION_REQUIRED" in caplog.text


async def test_token_endpoint_errors_are_not_audited(engine, make_client):
    client = await make_client(engine, il(2026, 11, 24, 10, 0))  # holds a valid token
    assert (await client.post("/api/v1/auth/token", data={"username": "demo"})).status_code == 400
    assert (await login(client, password="wrong")).status_code == 401
    assert await audit_rows(engine) == []


async def test_failed_audit_on_rejection_keeps_the_response(engine, make_client, monkeypatch, caplog):
    client = await make_client(engine, il(2026, 11, 24, 10, 0))

    async def broken(self, event):
        raise RuntimeError("audit store down")

    monkeypatch.setattr(SqlAuditRepository, "record", broken)
    with caplog.at_level("ERROR"):
        response = await get(client, "base=ILS&quote=USD")

    assert response.status_code == 422
    assert response.json()["code"] == "UNSUPPORTED_CURRENCY_PAIR"
    assert "audit.write_failed status=422" in caplog.text


# --- Same-timestamp corrections -----------------------------------------------------

async def test_read_serves_same_timestamp_correction(engine, make_client):
    await ingest(engine)
    client = await make_client(engine, il(2026, 11, 24, 10, 0))
    before = (await get(client, "base=USD&quote=ILS")).json()

    restated = json.loads(boi_fixture("snapshot_2026-11-23.json"), parse_float=Decimal)
    next(e for e in restated["exchangeRates"] if e["key"] == "USD")["currentExchangeRate"] = Decimal("3.341")

    class Restated:
        async def fetch(self):
            return restated

    await IngestionService(engine, Restated(), FixedClock(il(2026, 11, 23, 16, 30))).run_once()
    after = (await get(client, "base=USD&quote=ILS")).json()

    assert before["rate"] == "3.343"
    assert after["rate"] == "3.341"
    assert after["rateId"] != before["rateId"]
    assert after["source"]["lastUpdate"] == before["source"]["lastUpdate"]  # same BoI timestamp
    assert after["eligibility"]["status"] == "ELIGIBLE"
