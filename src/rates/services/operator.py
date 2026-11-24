"""Operator actions: the only runtime changes to pairs, policy and the calendar.

Every action validates its input (``InvalidRequest``), then in ONE transaction reads
the current state, refuses a no-op or forbidden target (``AlreadyInState``), writes
the change and writes an audit record with ``before`` / ``after``. The API and the
refresh process re-read pairs, policy and calendar on every request / attempt, so
changes take effect without a restart.
"""

from datetime import date, time, timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from rates.adapters.db.repos import (
    SqlAuditRepository,
    SqlCalendarRepository,
    SqlPairRepository,
    SqlPolicyRepository,
)
from rates.clock import Clock
from rates.domain.errors import AlreadyDeclared, AlreadyInState, InvalidRequest
from rates.domain.models import BOI_TIMEZONE, AuditEvent

EVENT_NON_PUBLICATION_DAY_DECLARED = "NON_PUBLICATION_DAY_DECLARED"
EVENT_NON_PUBLICATION_DAY_RETRACTED = "NON_PUBLICATION_DAY_RETRACTED"
EVENT_PAIR_ENABLED = "PAIR_ENABLED"
EVENT_PAIR_DISABLED = "PAIR_DISABLED"
EVENT_POLICY_ACTIVATED = "POLICY_ACTIVATED"

ORIGIN_OPERATOR = "OPERATOR"


def _required(name: str, value: str | None) -> str:
    value = (value or "").strip()
    if not value:
        raise InvalidRequest(f"A {name} is required")
    return value


async def _audit(
    conn: AsyncConnection,
    clock: Clock,
    actor: str,
    event_type: str,
    entity_type: str,
    entity_id: str,
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
    reason: str,
) -> AuditEvent:
    event = AuditEvent(
        id=uuid4(),
        occurred_at=clock.now(),
        actor=actor,
        event_type=event_type,
        entity_type=entity_type,
        entity_id=entity_id,
        before=before,
        after=after,
        reason=reason,
    )
    await SqlAuditRepository(conn).record(event)
    return event


def _jsonable(row: dict[str, Any]) -> dict[str, Any]:
    return {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in row.items()}


# --- Non-publication days ---------------------------------------------------------


async def declare_non_publication_day(
    engine: AsyncEngine,
    day: date,
    reason: str,
    actor: str,
    clock: Clock,
    force: bool = False,
) -> AuditEvent:
    """Declare ``day`` a non-publication day (day and audit record written atomically).

    Without ``force`` the day must be between today and ``max_age_days`` days before
    today (Israel time), so a typo can't silently extend eligibility into the future.
    """
    reason = _required("reason", reason)
    actor = _required("actor", actor)
    today = clock.now().astimezone(BOI_TIMEZONE).date()
    try:
        async with engine.begin() as conn:
            if not force:
                policy = await SqlPolicyRepository(conn).active()
                if policy is None:
                    raise InvalidRequest("No active policy to bound the declaration window; use --force")
                earliest = today - timedelta(days=policy.max_age_days)
                if not earliest <= day <= today:
                    raise InvalidRequest(
                        f"{day.isoformat()} is outside the allowed range "
                        f"{earliest.isoformat()} to {today.isoformat()}; use --force to override"
                    )
            calendar = SqlCalendarRepository(conn)
            if await calendar.exists(day):
                raise AlreadyDeclared(f"{day.isoformat()} is already a non-publication day")
            await calendar.add(day, reason, ORIGIN_OPERATOR, actor, clock.now())
            return await _audit(
                conn, clock, actor, EVENT_NON_PUBLICATION_DAY_DECLARED, "non_publication_day",
                day.isoformat(), None,
                {"day": day.isoformat(), "origin": ORIGIN_OPERATOR, "reason": reason, "forced": force},
                reason,
            )
    except IntegrityError as exc:  # concurrent declaration of the same day
        raise AlreadyDeclared(f"{day.isoformat()} is already a non-publication day") from exc


async def retract_non_publication_day(
    engine: AsyncEngine, day: date, reason: str, actor: str, clock: Clock
) -> AuditEvent:
    """Remove an operator-declared day. Official BoI calendar days are never retractable."""
    reason = _required("reason", reason)
    actor = _required("actor", actor)
    async with engine.begin() as conn:
        calendar = SqlCalendarRepository(conn)
        row = await calendar.get(day)
        if row is None:
            raise AlreadyInState(f"{day.isoformat()} is not a non-publication day")
        if row["origin"] != ORIGIN_OPERATOR:
            raise AlreadyInState(
                f"{day.isoformat()} comes from the BoI calendar ({row['origin']}) and can't be retracted"
            )
        await calendar.delete(day)
        return await _audit(
            conn, clock, actor, EVENT_NON_PUBLICATION_DAY_RETRACTED, "non_publication_day",
            day.isoformat(), _jsonable(row), None, reason,
        )


# --- Currency pairs ----------------------------------------------------------------


def parse_pair(text: str) -> tuple[str, str]:
    parts = (text or "").strip().upper().split("/")
    if len(parts) != 2 or not all(len(p) == 3 and p.isalpha() for p in parts):
        raise InvalidRequest(f"Pair must look like USD/ILS, got {text!r}")
    return parts[0], parts[1]


async def set_pair_enabled(
    engine: AsyncEngine, pair: str, enabled: bool, reason: str, actor: str, clock: Clock
) -> AuditEvent:
    base, quote = parse_pair(pair)
    reason = _required("reason", reason)
    actor = _required("actor", actor)
    name = f"{base}/{quote}"
    async with engine.begin() as conn:
        pairs = SqlPairRepository(conn)
        current = await pairs.get_enabled(base, quote)
        if current is None:
            raise InvalidRequest(f"{name} is not a configured pair (new pairs are added by migration)")
        if current == enabled:
            raise AlreadyInState(f"{name} is already {'enabled' if enabled else 'disabled'}")
        await pairs.set_enabled(base, quote, enabled)
        return await _audit(
            conn, clock, actor, EVENT_PAIR_ENABLED if enabled else EVENT_PAIR_DISABLED,
            "currency_pair", name, {"pair": name, "enabled": current}, {"pair": name, "enabled": enabled},
            reason,
        )


# --- Policy versions ---------------------------------------------------------------

POLICY_FIELDS = ("grace_minutes", "max_age_days", "weekday_publish_time", "friday_publish_time")


def parse_hhmm(text: str) -> time:
    try:
        hours, minutes = (int(part) for part in text.split(":"))
        return time(hours, minutes)
    except (ValueError, AttributeError) as exc:
        raise InvalidRequest(f"Time must be HH:MM, got {text!r}") from exc


async def activate_policy(
    engine: AsyncEngine,
    *,
    grace_minutes: int,
    max_age_days: int,
    weekday_publish_time: time,
    friday_publish_time: time,
    reason: str,
    actor: str,
    clock: Clock,
) -> AuditEvent:
    """Activate the next policy version (time zone copied from the current one)."""
    if grace_minutes < 0:
        raise InvalidRequest("grace_minutes must be >= 0")
    if max_age_days < 1:
        raise InvalidRequest("max_age_days must be >= 1")
    reason = _required("reason", reason)
    actor = _required("actor", actor)
    new_values = {
        "grace_minutes": grace_minutes,
        "max_age_days": max_age_days,
        "weekday_publish_time": weekday_publish_time,
        "friday_publish_time": friday_publish_time,
    }
    async with engine.begin() as conn:
        repo = SqlPolicyRepository(conn)
        current = await repo.active_row()
        if current is not None and all(current[f] == new_values[f] for f in POLICY_FIELDS):
            raise AlreadyInState(f"Policy version {current['version']} already has these values")
        version = await repo.max_version() + 1
        row = {
            "version": version,
            **new_values,
            "timezone": current["timezone"] if current else str(BOI_TIMEZONE),
            "created_at": clock.now(),
        }
        if current is not None:
            await repo.deactivate(int(current["version"]))  # before insert: one active at a time
        await repo.insert_active(row)
        return await _audit(
            conn, clock, actor, EVENT_POLICY_ACTIVATED, "policy", str(version),
            _jsonable(current) if current else None, _jsonable({**row, "active": True}), reason,
        )
