import json
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID

from rates.api.schemas import ExchangeRateOut
from rates.domain.models import (
    Eligibility,
    EligibilityReason,
    EligibilityStatus,
    RateObservation,
)
from rates.services.rate_query import RateView

OBS = RateObservation(
    id=UUID("6f1c1d0e-2a7b-4a8e-9a55-0c3f3c7f2b11"),
    base_currency="JPY",
    quote_currency="ILS",
    rate=Decimal("0.021026"),
    source_rate=Decimal("2.1026"),
    source_unit=100,
    publication_date=date(2026, 11, 23),
    source_last_update=datetime(2026, 11, 23, 13, 21, 10, 710489, tzinfo=UTC),
    retrieved_at=datetime(2026, 11, 23, 13, 25, tzinfo=UTC),
)


def dump(eligibility: Eligibility) -> dict:
    return json.loads(ExchangeRateOut.from_view(RateView(OBS, eligibility)).model_dump_json(by_alias=True))


def test_serialisation_contract():
    body = dump(Eligibility(EligibilityStatus.ELIGIBLE, None, date(2026, 11, 23),
                            datetime(2026, 11, 24, 8, 0, tzinfo=UTC), 1))

    assert body["rate"] == "0.021026"
    assert body["source"] == {
        "name": "BANK_OF_ISRAEL",
        "type": "REPRESENTATIVE",
        "lastUpdate": "2026-11-23T13:21:10.710489Z",
        "rate": "2.1026",
        "unit": 100,
    }
    assert body["retrievedAt"] == "2026-11-23T13:25:00Z"
    assert body["publicationDate"] == "2026-11-23"
    assert body["rateId"] == "6f1c1d0e-2a7b-4a8e-9a55-0c3f3c7f2b11"
    assert body["eligibility"] == {
        "status": "ELIGIBLE",
        "reason": None,
        "requiredPublicationDate": "2026-11-23",
        "evaluatedAt": "2026-11-24T08:00:00Z",
        "policyVersion": 1,
    }


def test_not_eligible_serialisation():
    body = dump(Eligibility(EligibilityStatus.NOT_ELIGIBLE, EligibilityReason.PUBLICATION_OVERDUE,
                            date(2026, 11, 24), datetime(2026, 11, 24, 14, 1, tzinfo=UTC), 1))
    assert body["eligibility"]["status"] == "NOT_ELIGIBLE"
    assert body["eligibility"]["reason"] == "PUBLICATION_OVERDUE"


def test_tiny_decimal_never_uses_exponent():
    obs = RateObservation(**{**OBS.__dict__, "rate": Decimal("0.0000001")})
    out = ExchangeRateOut.from_view(RateView(obs, Eligibility(
        EligibilityStatus.ELIGIBLE, None, None, datetime(2026, 11, 24, tzinfo=UTC), 1)))
    assert json.loads(out.model_dump_json(by_alias=True))["rate"] == "0.0000001"
