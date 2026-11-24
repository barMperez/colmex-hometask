"""Fetch -> validate whole snapshot -> one transaction: insert new observations + audit."""

import logging
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Protocol
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncEngine

from rates.adapters.boi.parser import build_observations, parse_payload
from rates.adapters.db.repos import SqlAuditRepository, SqlPairRepository, SqlRateRepository
from rates.clock import Clock
from rates.domain.models import BOI_TIMEZONE, AuditEvent

log = logging.getLogger(__name__)

EVENT_RATES_INGESTED = "RATES_INGESTED"
ACTOR = "refresh-process"


class SnapshotSource(Protocol):
    async def fetch(self) -> dict[str, Any]: ...


@dataclass(frozen=True)
class IngestionResult:
    inserted: int
    publication_date: date
    source_last_update: datetime


class IngestionService:
    def __init__(self, engine: AsyncEngine, source: SnapshotSource, clock: Clock) -> None:
        self._engine = engine
        self._source = source
        self._clock = clock

    async def run_once(self) -> IngestionResult:
        """Ingest the current BoI snapshot.

        Raises ``BoiFetchError`` / ``SnapshotRejected`` without touching storage. Any
        failure inside the transaction rolls back every observation of the snapshot.
        """
        payload = await self._source.fetch()
        retrieved_at = self._clock.now()
        today_local = retrieved_at.astimezone(BOI_TIMEZONE).date()
        snapshot = parse_payload(payload)

        async with self._engine.begin() as conn:
            pairs = await SqlPairRepository(conn).enabled_pairs()
            observations = build_observations(snapshot, pairs, today_local, retrieved_at)
            inserted = await SqlRateRepository(conn).insert_many(observations)
            first = observations[0]
            if inserted:
                await SqlAuditRepository(conn).record(
                    AuditEvent(
                        id=uuid4(),
                        occurred_at=retrieved_at,
                        actor=ACTOR,
                        event_type=EVENT_RATES_INGESTED,
                        entity_type="rate_snapshot",
                        entity_id=first.source_last_update.isoformat(),
                        before=None,
                        after={
                            "publication_date": first.publication_date.isoformat(),
                            "source_last_update": first.source_last_update.isoformat(),
                            "pairs": [f"{o.base_currency}/{o.quote_currency}" for o in inserted],
                            "observation_ids": [str(o.id) for o in inserted],
                        },
                        reason=None,
                    )
                )

        return IngestionResult(len(inserted), first.publication_date, first.source_last_update)
