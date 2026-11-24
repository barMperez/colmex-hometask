"""Operator actions. The only runtime policy mutation in this release."""

from datetime import date
from uuid import uuid4

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from rates.adapters.db.repos import SqlAuditRepository, SqlCalendarRepository
from rates.clock import Clock
from rates.domain.errors import AlreadyDeclared, InvalidRequest
from rates.domain.models import AuditEvent

EVENT_NON_PUBLICATION_DAY_DECLARED = "NON_PUBLICATION_DAY_DECLARED"


async def declare_non_publication_day(
    engine: AsyncEngine, day: date, reason: str, actor: str, clock: Clock
) -> AuditEvent:
    """Declare ``day`` a non-publication day. Day and audit record are written atomically."""
    reason = (reason or "").strip()
    actor = (actor or "").strip()
    if not reason:
        raise InvalidRequest("A reason is required")
    if not actor:
        raise InvalidRequest("An actor is required")

    now = clock.now()
    event = AuditEvent(
        id=uuid4(),
        occurred_at=now,
        actor=actor,
        event_type=EVENT_NON_PUBLICATION_DAY_DECLARED,
        entity_type="non_publication_day",
        entity_id=day.isoformat(),
        before=None,
        after={"day": day.isoformat(), "origin": "OPERATOR", "reason": reason},
        reason=reason,
    )
    try:
        async with engine.begin() as conn:
            calendar = SqlCalendarRepository(conn)
            if await calendar.exists(day):
                raise AlreadyDeclared(f"{day.isoformat()} is already a non-publication day")
            await calendar.add(day, reason, "OPERATOR", actor, now)
            await SqlAuditRepository(conn).record(event)
    except IntegrityError as exc:  # concurrent declaration of the same day
        raise AlreadyDeclared(f"{day.isoformat()} is already a non-publication day") from exc
    return event
