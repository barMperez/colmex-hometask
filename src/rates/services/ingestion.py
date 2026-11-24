"""Fetch -> validate each currency -> one transaction: insert new observations + audit."""

import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Protocol
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncEngine

from rates.adapters.boi.parser import RejectedCurrency, Restamp, build_ingestion, parse_payload
from rates.adapters.db.repos import (
    Correction,
    SqlAuditRepository,
    SqlCalendarRepository,
    SqlPairRepository,
    SqlPolicyRepository,
    SqlRateRepository,
)
from rates.clock import Clock
from rates.domain.errors import SnapshotRejected
from rates.domain.models import BOI_TIMEZONE, AuditEvent, RateObservation

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
    """Latest publication date across the observations this snapshot leaves current."""
    latest_source_last_update: datetime
    """Latest BoI ``lastUpdate`` across the observations this snapshot leaves current."""
    rejected: list[RejectedCurrency] = field(default_factory=list)
    """Enabled currencies rejected by validation (the others were accepted)."""
    corrections: list[Correction] = field(default_factory=list)
    """Same-timestamp restatements by BoI, stored as new revisions."""
    restamped: list[Restamp] = field(default_factory=list)
    """Entries whose ``lastUpdate`` moved without new content: nothing was stored."""

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
        An entry that only re-stamps the pair's current observation is logged and
        skipped (``IngestionResult.restamped``); it is neither stored nor rejected.
        """
        payload = await self._source.fetch()
        retrieved_at = self._clock.now()
        today_local = retrieved_at.astimezone(BOI_TIMEZONE).date()
        snapshot = parse_payload(payload)

        async with self._engine.begin() as conn:
            pairs = await SqlPairRepository(conn).enabled_pairs()
            policy = await SqlPolicyRepository(conn).active()
            if policy is None:
                raise SnapshotRejected("No active freshness policy: cannot attribute publication dates")
            calendar = await SqlCalendarRepository(conn).load_view()
            rates = SqlRateRepository(conn)
            current: dict[tuple[str, str], RateObservation] = {}
            for pair in pairs:
                observation = await rates.latest_for_pair(pair.base, pair.quote)
                if observation is not None:
                    current[(pair.base, pair.quote)] = observation
            batch = build_ingestion(
                snapshot, pairs, today_local, retrieved_at, calendar, policy, current
            )
            if not batch.accepted and not batch.restamped:
                raise SnapshotRejected(
                    "No enabled currency is valid: "
                    + "; ".join(f"{r.currency}: {r.reason}" for r in batch.rejected)
                )
            for r in batch.restamped:
                log.warning(
                    "rates.restamp_ignored currency=%s last_update=%s current_last_update=%s "
                    "publication_date=%s rate=%s change=%s",
                    r.currency,
                    r.last_update.isoformat(),
                    r.current.source_last_update.isoformat(),
                    r.current.publication_date.isoformat(),
                    r.current.source_rate,
                    r.current.source_change,
                )
            observations = batch.accepted
            stored_batch = await rates.store_batch(observations)
            inserted = stored_batch.inserted
            for c in stored_batch.corrections:
                log.warning(
                    "rates.same_timestamp_correction pair=%s source_last_update=%s from=%s to=%s "
                    "unit_from=%d unit_to=%d revision=%d",
                    c.pair,
                    c.source_last_update.isoformat(),
                    c.previous_rate,
                    c.new_rate,
                    c.previous_unit,
                    c.new_unit,
                    c.revision,
                )
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
                                    "revision": o.revision,
                                }
                                for o in inserted
                            ],
                            "corrections": [
                                {
                                    "pair": c.pair,
                                    "source_last_update": c.source_last_update.isoformat(),
                                    "previous_rate": str(c.previous_rate),
                                    "previous_unit": c.previous_unit,
                                    "new_rate": str(c.new_rate),
                                    "new_unit": c.new_unit,
                                    "revision": c.revision,
                                }
                                for c in stored_batch.corrections
                            ],
                            "rejected": [
                                {"currency": r.currency, "reason": r.reason}
                                for r in batch.rejected
                            ],
                        },
                        reason=None,
                    )
                )

        effective = observations + [r.current for r in batch.restamped]
        return IngestionResult(
            inserted=len(inserted),
            latest_publication_date=max(o.publication_date for o in effective),
            latest_source_last_update=max(o.source_last_update for o in effective),
            rejected=batch.rejected,
            corrections=stored_batch.corrections,
            restamped=batch.restamped,
        )
