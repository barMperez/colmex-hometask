"""Fetch -> validate each currency -> one transaction: insert new observations + audit."""

import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Protocol
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncEngine

from rates.adapters.boi.parser import RejectedCurrency, build_ingestion, parse_payload
from rates.adapters.db.repos import SqlAuditRepository, SqlPairRepository, SqlRateRepository
from rates.clock import Clock
from rates.domain.errors import SnapshotRejected
from rates.domain.models import BOI_TIMEZONE, AuditEvent

log = logging.getLogger(__name__)

EVENT_RATES_INGESTED = "RATES_INGESTED"
ACTOR = "refresh-process"
OUTCOME_SUCCESS = "success"
OUTCOME_PARTIAL = "partial"


class SnapshotSource(Protocol):
    async def fetch(self) -> dict[str, Any]: ...


@dataclass(frozen=True)
class IngestionResult:
    inserted: int
    latest_publication_date: date
    """Latest publication date across the snapshot's enabled entries."""
    latest_source_last_update: datetime
    """Latest BoI ``lastUpdate`` across the snapshot's enabled entries."""
    rejected: list[RejectedCurrency] = field(default_factory=list)
    """Enabled currencies rejected by validation (the others were accepted)."""

    @property
    def outcome(self) -> str:
        return OUTCOME_PARTIAL if self.rejected else OUTCOME_SUCCESS


class IngestionService:
    def __init__(self, engine: AsyncEngine, source: SnapshotSource, clock: Clock) -> None:
        self._engine = engine
        self._source = source
        self._clock = clock

    async def run_once(self) -> IngestionResult:
        """Ingest the current BoI snapshot.

        Valid currencies are stored even when others are rejected (see
        ``IngestionResult.rejected``). Raises ``BoiFetchError``, or ``SnapshotRejected``
        when the payload is unusable or no enabled currency is valid, without touching
        storage. Any failure inside the transaction rolls back every observation.
        """
        payload = await self._source.fetch()
        retrieved_at = self._clock.now()
        today_local = retrieved_at.astimezone(BOI_TIMEZONE).date()
        snapshot = parse_payload(payload)

        async with self._engine.begin() as conn:
            pairs = await SqlPairRepository(conn).enabled_pairs()
            batch = build_ingestion(snapshot, pairs, today_local, retrieved_at)
            if not batch.accepted:
                raise SnapshotRejected(
                    "No enabled currency is valid: "
                    + "; ".join(f"{r.currency}: {r.reason}" for r in batch.rejected)
                )
            observations = batch.accepted
            inserted = await SqlRateRepository(conn).insert_many(observations)
            if inserted:
                ingestion_id = uuid4()
                await SqlAuditRepository(conn).record(
                    AuditEvent(
                        id=ingestion_id,
                        occurred_at=retrieved_at,
                        actor=ACTOR,
                        event_type=EVENT_RATES_INGESTED,
                        entity_type="rate_ingestion",
                        entity_id=str(ingestion_id),
                        before=None,
                        after={
                            "observations": [
                                {
                                    "pair": f"{o.base_currency}/{o.quote_currency}",
                                    "id": str(o.id),
                                    "source_last_update": o.source_last_update.isoformat(),
                                    "publication_date": o.publication_date.isoformat(),
                                }
                                for o in inserted
                            ],
                            "rejected": [
                                {"currency": r.currency, "reason": r.reason}
                                for r in batch.rejected
                            ],
                        },
                        reason=None,
                    )
                )

        return IngestionResult(
            inserted=len(inserted),
            latest_publication_date=max(o.publication_date for o in observations),
            latest_source_last_update=max(o.source_last_update for o in observations),
            rejected=batch.rejected,
        )
