import subprocess
import sys
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from rates.adapters.db.repos import (
    SqlCalendarRepository,
    SqlPolicyRepository,
    SqlRateRepository,
)
from rates.adapters.db.tables import audit_events, non_publication_days
from rates.clock import FixedClock
from rates.domain.eligibility import evaluate
from rates.domain.errors import AlreadyDeclared, InvalidRequest
from rates.domain.models import BOI_TIMEZONE, EligibilityReason, EligibilityStatus, RateObservation
from rates.services.operator import declare_non_publication_day
from tests.conftest import ROOT

AFTER_DEADLINE = datetime(2026, 12, 2, 16, 30, tzinfo=BOI_TIMEZONE).astimezone(UTC)


async def _assess(engine, now):
    """Mirror of the read path: everything re-read from storage on each evaluation."""
    async with engine.connect() as conn:
        observation = await SqlRateRepository(conn).latest_for_pair("USD", "ILS")
        calendar = await SqlCalendarRepository(conn).load_view()
        policy = await SqlPolicyRepository(conn).active()
    return evaluate(observation, now, calendar, policy)


async def _count(engine, table):
    async with engine.connect() as conn:
        return (await conn.execute(select(func.count()).select_from(table))).scalar_one()


async def test_declaration_restores_eligibility_without_restart(engine):
    ts = datetime(2026, 12, 1, 13, 21, tzinfo=UTC)
    async with engine.begin() as conn:
        await SqlRateRepository(conn).insert_many([
            RateObservation(uuid4(), "USD", "ILS", Decimal("3.343"), Decimal("3.343"), 1,
                            date(2026, 12, 1), ts, ts)
        ])

    before = await _assess(engine, AFTER_DEADLINE)
    assert before.status is EligibilityStatus.NOT_ELIGIBLE
    assert before.reason is EligibilityReason.PUBLICATION_OVERDUE

    clock = FixedClock(AFTER_DEADLINE)
    await declare_non_publication_day(engine, date(2026, 12, 2), "BoI did not publish", "alice", clock)

    after = await _assess(engine, AFTER_DEADLINE)
    assert after.status is EligibilityStatus.ELIGIBLE
    assert after.required_publication_date == date(2026, 12, 1)

    async with engine.connect() as conn:
        audit = (await conn.execute(select(audit_events))).mappings().one()
        day = (await conn.execute(
            select(non_publication_days).where(non_publication_days.c.day == date(2026, 12, 2))
        )).mappings().one()
    assert audit["actor"] == "alice"
    assert audit["reason"] == "BoI did not publish"
    assert audit["entity_id"] == "2026-12-02"
    assert audit["occurred_at"] == AFTER_DEADLINE
    assert audit["event_type"] == "NON_PUBLICATION_DAY_DECLARED"
    assert day["origin"] == "OPERATOR"


@pytest.mark.parametrize("reason", ["", "   "])
async def test_declaration_without_reason_writes_nothing(engine, reason):
    days_before = await _count(engine, non_publication_days)
    with pytest.raises(InvalidRequest):
        await declare_non_publication_day(engine, date(2026, 12, 2), reason, "alice", FixedClock(AFTER_DEADLINE))
    assert await _count(engine, non_publication_days) == days_before
    assert await _count(engine, audit_events) == 0


async def test_duplicate_declaration_rejected(engine):
    with pytest.raises(AlreadyDeclared):
        # 2026-10-27 is already in the seeded BoI calendar.
        await declare_non_publication_day(engine, date(2026, 10, 27), "dup", "alice", FixedClock(AFTER_DEADLINE))
    assert await _count(engine, audit_events) == 0


def _script(url: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "declare_non_publication_day.py"),
         "--database-url", url, *args],
        capture_output=True, text=True, timeout=60,
    )


def test_script_exit_codes(migrated_url):
    ok = _script(migrated_url, "--date", "2026-12-02", "--reason", "no data", "--actor", "alice")
    assert ok.returncode == 0, ok.stderr
    assert "declared 2026-12-02" in ok.stdout

    assert _script(migrated_url, "--date", "2026-12-02", "--reason", "again", "--actor", "alice").returncode == 3
    assert _script(migrated_url, "--date", "2026-12-03", "--reason", " ", "--actor", "alice").returncode == 2
