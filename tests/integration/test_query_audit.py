from datetime import UTC, datetime

from sqlalchemy import select

from rates.adapters.db.tables import audit_events
from rates.clock import FixedClock
from rates.services.query_audit import QueryAuditService, QueryOutcome

NOW = datetime(2026, 11, 24, 8, 0, tzinfo=UTC)
BODY = {
    "rateId": "c2726261-6e4f-4692-8c33-b039a7f8b13b",
    "baseCurrency": "USD",
    "quoteCurrency": "ILS",
    "rate": "3.343",
    "publicationDate": "2026-11-23",
    "eligibility": {
        "status": "ELIGIBLE",
        "reason": None,
        "requiredPublicationDate": "2026-11-23",
        "evaluatedAt": "2026-11-24T08:00:00Z",
        "policyVersion": 1,
    },
    "source": {"name": "BANK_OF_ISRAEL"},
    "retrievedAt": "2026-11-23T13:25:00Z",
}


async def stored(engine):
    async with engine.connect() as conn:
        return (await conn.execute(select(audit_events))).mappings().one()


async def test_returned_rate_record(engine):
    await QueryAuditService(engine, FixedClock(NOW)).record(
        "demo", {"base": "usd", "quote": "ils"}, QueryOutcome(200, response=BODY)
    )

    row = await stored(engine)
    assert row["event_type"] == "RATE_QUERIED"
    assert row["actor"] == "demo"
    assert row["occurred_at"] == NOW
    assert row["entity_type"] == "rate_observation"
    assert row["entity_id"] == BODY["rateId"]
    assert row["after"]["request"] == {"base": "usd", "quote": "ils"}
    response = row["after"]["response"]
    assert response["status"] == 200 and response["code"] is None
    for field in ("rateId", "rate", "publicationDate", "eligibility", "baseCurrency", "quoteCurrency"):
        assert response[field] == BODY[field]
    assert "source" not in response  # only the fields that matter for the decision


async def test_rejected_query_record(engine):
    long_value = "X" * 500
    await QueryAuditService(engine, FixedClock(NOW)).record(
        "demo", {"base": long_value}, QueryOutcome(400, code="INVALID_REQUEST")
    )

    row = await stored(engine)
    assert row["entity_type"] == "rate_query"
    assert row["entity_id"] == str(row["id"])
    assert row["after"]["request"] == {"base": "X" * 64, "quote": None}
    assert row["after"]["response"] == {"status": 400, "code": "INVALID_REQUEST", "rateId": None}
