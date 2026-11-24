"""Response models. Decimals are JSON strings; instants are UTC with a 'Z' suffix."""

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, PlainSerializer, WithJsonSchema
from pydantic.alias_generators import to_camel

from rates.domain.decimals import decimal_to_plain_string
from rates.domain.models import EligibilityReason, EligibilityStatus
from rates.services.rate_query import RateView


def _utc_z(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


DecimalString = Annotated[
    Decimal,
    PlainSerializer(decimal_to_plain_string, return_type=str),
    WithJsonSchema(
        {"type": "string", "pattern": r"^\d+(\.\d+)?$", "examples": ["3.343"]},
        mode="serialization",
    ),
]

UtcInstant = Annotated[
    datetime,
    PlainSerializer(_utc_z, return_type=str),
    WithJsonSchema(
        {"type": "string", "format": "date-time", "examples": ["2026-11-23T13:21:10.710489Z"]},
        mode="serialization",
    ),
]


class CamelModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, frozen=True)


class EligibilityOut(CamelModel):
    status: EligibilityStatus
    reason: EligibilityReason | None
    required_publication_date: date | None
    evaluated_at: UtcInstant
    policy_version: int


class SourceOut(CamelModel):
    name: Literal["BANK_OF_ISRAEL"] = "BANK_OF_ISRAEL"
    type: Literal["REPRESENTATIVE"] = "REPRESENTATIVE"
    last_update: UtcInstant
    rate: DecimalString
    unit: int


class ExchangeRateOut(CamelModel):
    """A BoI representative rate with provenance and an eligibility assessment.

    Consumers MUST check ``eligibility.status == "ELIGIBLE"`` before using ``rate``
    in a financial operation. A 200 response does not by itself mean the rate is usable.
    """

    rate_id: UUID
    base_currency: str
    quote_currency: str
    rate: DecimalString
    publication_date: date
    eligibility: EligibilityOut
    source: SourceOut
    retrieved_at: UtcInstant

    @classmethod
    def from_view(cls, view: RateView) -> "ExchangeRateOut":
        o, e = view.observation, view.eligibility
        return cls(
            rate_id=o.id,
            base_currency=o.base_currency,
            quote_currency=o.quote_currency,
            rate=o.rate,
            publication_date=o.publication_date,
            eligibility=EligibilityOut(
                status=e.status,
                reason=e.reason,
                required_publication_date=e.required_publication_date,
                evaluated_at=e.evaluated_at,
                policy_version=e.policy_version,
            ),
            source=SourceOut(last_update=o.source_last_update, rate=o.source_rate, unit=o.source_unit),
            retrieved_at=o.retrieved_at,
        )


class ReadinessOut(CamelModel):
    status: Literal["ready"] = "ready"
    database: Literal["ok"] = "ok"
    latest_retrieved_at: UtcInstant | None
