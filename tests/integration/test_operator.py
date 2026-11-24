import json
import subprocess
import sys
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import func, select, update

from rates.adapters.db.repos import (
    SqlCalendarRepository,
    SqlPairRepository,
    SqlPolicyRepository,
    SqlRateRepository,
)
from rates.adapters.db.tables import audit_events, non_publication_days, policies
from rates.clock import FixedClock
from rates.domain.calendar import publication_deadline
from rates.domain.eligibility import evaluate
from rates.domain.errors import AlreadyInState, InvalidRequest, UnsupportedPair
from rates.domain.models import BOI_TIMEZONE, EligibilityReason, EligibilityStatus, RateObservation
from rates.services.ingestion import IngestionService
from rates.services.operator import (
    declare_non_publication_day,
    retract_non_publication_day,
    activate_policy,
    set_pair_enabled,
)
from rates.services.rate_query import RateQueryService
from tests.conftest import ROOT
from tests.fixtures import boi_fixture

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
        await SqlRateRepository(conn).store_batch([
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
    clock = FixedClock(AFTER_DEADLINE)
    await declare_non_publication_day(engine, date(2026, 12, 2), "first", "alice", clock)
    with pytest.raises(AlreadyInState):
        await declare_non_publication_day(engine, date(2026, 12, 2), "again", "alice", clock)
    assert await _count(engine, audit_events) == 1


async def test_future_date_refused_with_allowed_range(engine):
    with pytest.raises(InvalidRequest, match="2026-11-25 to 2026-12-02"):
        await declare_non_publication_day(engine, date(2026, 12, 3), "typo", "alice", FixedClock(AFTER_DEADLINE))
    assert await _count(engine, audit_events) == 0


async def test_date_older_than_window_refused(engine):
    with pytest.raises(InvalidRequest, match="outside the allowed range"):
        await declare_non_publication_day(engine, date(2026, 11, 24), "late", "alice", FixedClock(AFTER_DEADLINE))
    assert await _count(engine, audit_events) == 0


async def test_window_edges_are_inclusive(engine):
    clock = FixedClock(AFTER_DEADLINE)
    await declare_non_publication_day(engine, date(2026, 11, 25), "oldest allowed", "alice", clock)
    await declare_non_publication_day(engine, date(2026, 12, 2), "today", "alice", clock)
    assert await _count(engine, audit_events) == 2


async def test_forced_declaration_is_audited_as_forced(engine):
    await declare_non_publication_day(
        engine, date(2026, 12, 24), "planned closure", "alice", FixedClock(AFTER_DEADLINE), force=True
    )
    async with engine.connect() as conn:
        audit = (await conn.execute(select(audit_events))).mappings().one()
    assert audit["after"]["forced"] is True


async def test_unforced_declaration_is_audited_as_not_forced(engine):
    await declare_non_publication_day(engine, date(2026, 12, 2), "no data", "alice", FixedClock(AFTER_DEADLINE))
    async with engine.connect() as conn:
        audit = (await conn.execute(select(audit_events))).mappings().one()
    assert audit["after"]["forced"] is False


async def test_no_active_policy_requires_force(engine):
    async with engine.begin() as conn:
        await conn.execute(update(policies).values(active=False))
    with pytest.raises(InvalidRequest, match="No active policy"):
        await declare_non_publication_day(engine, date(2026, 12, 2), "x", "alice", FixedClock(AFTER_DEADLINE))
    await declare_non_publication_day(
        engine, date(2026, 12, 2), "x", "alice", FixedClock(AFTER_DEADLINE), force=True
    )


def _script(url: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "declare_non_publication_day.py"),
         "--database-url", url, *args],
        capture_output=True, text=True, timeout=60,
    )


def test_script_exit_codes(migrated_url):
    # The script uses the real clock: pick dates relative to today in Israel.
    today = datetime.now(BOI_TIMEZONE).date()
    ok = _script(migrated_url, "--date", today.isoformat(), "--reason", "no data", "--actor", "alice")
    assert ok.returncode == 0, ok.stderr
    assert f"declared {today.isoformat()}" in ok.stdout

    assert _script(migrated_url, "--date", today.isoformat(), "--reason", "again", "--actor", "alice").returncode == 3
    assert _script(migrated_url, "--date", today.isoformat(), "--reason", " ", "--actor", "alice").returncode == 2
    tomorrow = (today + timedelta(days=1)).isoformat()
    future = _script(migrated_url, "--date", tomorrow, "--reason", "typo", "--actor", "alice")
    assert future.returncode == 2 and "outside the allowed range" in future.stderr
    forced = _script(migrated_url, "--date", tomorrow, "--reason", "planned", "--actor", "alice", "--force")
    assert forced.returncode == 0, forced.stderr
    broken = _script("sqlite+aiosqlite:///nonexistent-dir/x.db", "--date", today.isoformat(),
                     "--reason", "r", "--actor", "a")
    assert broken.returncode == 1


# --- Retraction ----------------------------------------------------------------------

async def test_retraction_undoes_a_declaration(engine):
    ts = datetime(2026, 12, 1, 13, 21, tzinfo=UTC)
    async with engine.begin() as conn:
        await SqlRateRepository(conn).store_batch([
            RateObservation(uuid4(), "USD", "ILS", Decimal("3.343"), Decimal("3.343"), 1,
                            date(2026, 12, 1), ts, ts)
        ])
    clock = FixedClock(AFTER_DEADLINE)
    await declare_non_publication_day(engine, date(2026, 12, 2), "no data", "alice", clock)
    assert (await _assess(engine, AFTER_DEADLINE)).status is EligibilityStatus.ELIGIBLE

    await retract_non_publication_day(engine, date(2026, 12, 2), "BoI did publish", "bob", clock)

    after = await _assess(engine, AFTER_DEADLINE)
    assert after.status is EligibilityStatus.NOT_ELIGIBLE
    assert after.reason is EligibilityReason.PUBLICATION_OVERDUE
    async with engine.connect() as conn:
        audit = (
            await conn.execute(
                select(audit_events).where(audit_events.c.event_type == "NON_PUBLICATION_DAY_RETRACTED")
            )
        ).mappings().one()
    assert audit["actor"] == "bob" and audit["reason"] == "BoI did publish"
    assert audit["before"]["day"] == "2026-12-02"
    assert audit["before"]["origin"] == "OPERATOR"
    assert audit["before"]["created_by"] == "alice"


@pytest.mark.parametrize("day", [date(2026, 10, 27), date(2026, 12, 9)], ids=["boi-calendar", "unknown"])
async def test_retraction_refused(engine, day):
    with pytest.raises(AlreadyInState):
        await retract_non_publication_day(engine, day, "r", "bob", FixedClock(AFTER_DEADLINE))
    assert await _count(engine, audit_events) == 0


def test_retract_script_exit_codes(migrated_url):
    today = datetime.now(BOI_TIMEZONE).date().isoformat()
    assert _script(migrated_url, "--date", today, "--reason", "no data", "--actor", "alice").returncode == 0
    retract = [sys.executable, str(ROOT / "scripts" / "retract_non_publication_day.py"), "--database-url", migrated_url]
    ok = subprocess.run([*retract, "--date", today, "--reason", "mistake", "--actor", "bob"],
                        capture_output=True, text=True, timeout=60)
    assert ok.returncode == 0, ok.stderr
    assert f"retracted {today}" in ok.stdout
    boi_day = subprocess.run([*retract, "--date", "2026-10-27", "--reason", "x", "--actor", "bob"],
                             capture_output=True, text=True, timeout=60)
    assert boi_day.returncode == 3 and "BoI calendar" in boi_day.stderr


# --- Pairs ---------------------------------------------------------------------------

async def _ingest_fixture(engine, name, at):
    snapshot = json.loads(boi_fixture(name), parse_float=Decimal)

    class Source:
        async def fetch(self):
            return snapshot

    return await IngestionService(engine, Source(), FixedClock(at)).run_once()


async def test_disabling_a_pair_boi_stopped_sending(engine):
    at = datetime(2026, 11, 23, 13, 25, tzinfo=UTC)
    assert (await _ingest_fixture(engine, "missing_gbp.json", at)).outcome == "partial"

    event = await set_pair_enabled(engine, "gbp/ils", False, "BoI stopped GBP", "alice", FixedClock(at))

    assert (await _ingest_fixture(engine, "missing_gbp.json", at)).outcome == "success"
    reads = RateQueryService(engine, FixedClock(at))
    with pytest.raises(UnsupportedPair):
        await reads.get_rate("GBP", "ILS")
    assert event.event_type == "PAIR_DISABLED"
    assert (event.before, event.after) == ({"pair": "GBP/ILS", "enabled": True}, {"pair": "GBP/ILS", "enabled": False})
    assert event.actor == "alice" and event.reason == "BoI stopped GBP"

    again = await set_pair_enabled(engine, "GBP/ILS", True, "BoI resumed GBP", "alice", FixedClock(at))
    assert again.event_type == "PAIR_ENABLED"
    async with engine.connect() as conn:
        assert "GBP" in {p.base for p in await SqlPairRepository(conn).enabled_pairs()}
    assert await _count(engine, audit_events) == 3  # partial ingestion + 2 pair changes


@pytest.mark.parametrize(
    ("pair", "enabled", "error"),
    [("JPY/ILS", True, InvalidRequest), ("USD-ILS", True, InvalidRequest), ("USD/ILS", True, AlreadyInState)],
    ids=["unknown", "malformed", "no-op"],
)
async def test_pair_change_refused(engine, pair, enabled, error):
    with pytest.raises(error):
        await set_pair_enabled(engine, pair, enabled, "r", "alice", FixedClock(AFTER_DEADLINE))
    assert await _count(engine, audit_events) == 0


def test_pair_script_exit_codes(migrated_url):
    script = [sys.executable, str(ROOT / "scripts" / "set_pair_enabled.py"), "--database-url", migrated_url,
              "--reason", "r", "--actor", "alice"]

    def run(*args):
        return subprocess.run([*script, *args], capture_output=True, text=True, timeout=60)

    ok = run("--pair", "GBP/ILS", "--disable")
    assert ok.returncode == 0, ok.stderr
    assert "GBP/ILS disabled" in ok.stdout
    assert run("--pair", "GBP/ILS", "--disable").returncode == 3
    assert run("--pair", "JPY/ILS", "--enable").returncode == 2
    assert run("--pair", "GBP/ILS", "--enable").returncode == 0


# --- Policy versions -----------------------------------------------------------------

V1 = {"grace_minutes": 45, "max_age_days": 7, "weekday_publish_time": time(15, 15), "friday_publish_time": time(12, 15)}


async def test_activating_a_stricter_grace_period(engine):
    ts = datetime(2026, 11, 23, 13, 21, tzinfo=UTC)
    async with engine.begin() as conn:
        await SqlRateRepository(conn).store_batch([
            RateObservation(uuid4(), "USD", "ILS", Decimal("3.343"), Decimal("3.343"), 1,
                            date(2026, 11, 23), ts, ts)
        ])
    at_1550 = datetime(2026, 11, 24, 15, 50, tzinfo=BOI_TIMEZONE).astimezone(UTC)
    assert (await _assess(engine, at_1550)).status is EligibilityStatus.ELIGIBLE  # v1 deadline 16:00

    event = await activate_policy(
        engine, **(V1 | {"grace_minutes": 30}), reason="tighter", actor="alice", clock=FixedClock(at_1550)
    )

    after = await _assess(engine, at_1550)  # v2 deadline 15:45
    assert after.policy_version == 2
    assert after.status is EligibilityStatus.NOT_ELIGIBLE
    assert after.reason is EligibilityReason.PUBLICATION_OVERDUE
    async with engine.connect() as conn:
        active = (await conn.execute(select(policies.c.version).where(policies.c.active.is_(True)))).scalars().all()
        policy = await SqlPolicyRepository(conn).active()
    assert active == [2]
    assert publication_deadline(date(2026, 11, 24), policy) == datetime(
        2026, 11, 24, 15, 45, tzinfo=BOI_TIMEZONE
    ).astimezone(UTC)
    assert event.event_type == "POLICY_ACTIVATED" and event.entity_id == "2"
    assert event.before["version"] == 1 and event.before["grace_minutes"] == 45
    assert event.after["version"] == 2 and event.after["grace_minutes"] == 30
    assert event.after["timezone"] == "Asia/Jerusalem"


@pytest.mark.parametrize(
    ("overrides", "error"),
    [
        ({"grace_minutes": -1}, InvalidRequest),
        ({"max_age_days": 0}, InvalidRequest),
        ({}, AlreadyInState),  # identical to v1
    ],
    ids=["negative-grace", "zero-max-age", "identical"],
)
async def test_invalid_policy_refused(engine, overrides, error):
    with pytest.raises(error):
        await activate_policy(engine, **(V1 | overrides), reason="r", actor="alice", clock=FixedClock(AFTER_DEADLINE))
    async with engine.connect() as conn:
        assert (await SqlPolicyRepository(conn).active()).version == 1
    assert await _count(engine, audit_events) == 0


def test_policy_script_exit_codes(migrated_url):
    script = [sys.executable, str(ROOT / "scripts" / "activate_policy.py"), "--database-url", migrated_url,
              "--max-age-days", "7", "--friday-publish-time", "12:15", "--reason", "r", "--actor", "alice"]

    def run(*args):
        return subprocess.run([*script, *args], capture_output=True, text=True, timeout=60)

    assert run("--grace-minutes", "45", "--weekday-publish-time", "15:15").returncode == 3  # same as v1
    assert run("--grace-minutes", "30", "--weekday-publish-time", "25:99").returncode == 2  # malformed
    ok = run("--grace-minutes", "30", "--weekday-publish-time", "15:15")
    assert ok.returncode == 0, ok.stderr
    assert "activated policy version 2" in ok.stdout
