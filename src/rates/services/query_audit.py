"""Audit trail of rate queries: who asked, what they asked, what they were told.

Each record is written in its own short transaction, separate from the read. The
caller decides what a failure means (fail closed for a returned rate).
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncEngine

from rates.adapters.db.repos import SqlAuditRepository
from rates.clock import Clock
from rates.domain.models import AuditEvent

EVENT_RATE_QUERIED = "RATE_QUERIED"
MAX_RAW_PARAM_LENGTH = 64
RESPONSE_FIELDS = (
    "rateId",
    "baseCurrency",
    "quoteCurrency",
    "rate",
    "publicationDate",
    "eligibility",
)


@dataclass(frozen=True)
class QueryOutcome:
    status: int
    code: str | None = None
    """Problem-details code for an error response; None for a returned rate."""
    response: Mapping[str, Any] | None = None
    """The JSON body returned to the caller, for a returned rate."""

    @property
    def rate_id(self) -> str | None:
        return str(self.response["rateId"]) if self.response else None


def _raw(query: Mapping[str, str], name: str) -> str | None:
    value = query.get(name)
    return None if value is None else value[:MAX_RAW_PARAM_LENGTH]


class QueryAuditService:
    def __init__(self, engine: AsyncEngine, clock: Clock) -> None:
        self._engine = engine
        self._clock = clock

    async def record(self, actor: str, query: Mapping[str, str], outcome: QueryOutcome) -> None:
        """Write one ``RATE_QUERIED`` event. Raises on any storage failure."""
        event_id = uuid4()
        response: dict[str, Any] = {"status": outcome.status, "code": outcome.code, "rateId": outcome.rate_id}
        if outcome.response:
            response |= {k: outcome.response[k] for k in RESPONSE_FIELDS if k in outcome.response}
        event = AuditEvent(
            id=event_id,
            occurred_at=self._clock.now(),
            actor=actor,
            event_type=EVENT_RATE_QUERIED,
            entity_type="rate_observation" if outcome.rate_id else "rate_query",
            entity_id=outcome.rate_id or str(event_id),
            before=None,
            after={
                "request": {"base": _raw(query, "base"), "quote": _raw(query, "quote")},
                "response": response,
            },
            reason=None,
        )
        async with self._engine.begin() as conn:
            await SqlAuditRepository(conn).record(event)
