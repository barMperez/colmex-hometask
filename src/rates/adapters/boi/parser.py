"""Parse, validate and normalise a BoI snapshot into rate observations.

Each enabled currency is validated on its own: a missing or invalid currency is
rejected with a reason while the others are accepted. Only a payload without a
usable ``exchangeRates`` array (or a configuration error) rejects everything.
"""

import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal, localcontext
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from rates.domain.calendar import CalendarView, latest_publication_slot, publication_slot
from rates.domain.decimals import canonical_decimal
from rates.domain.errors import PolicyNotEvaluable, SnapshotRejected
from rates.domain.models import BOI_TIMEZONE, CurrencyPair, Policy, RateObservation

log = logging.getLogger(__name__)

BOI_QUOTE_CURRENCY = "ILS"
REASON_MISSING = "missing"
REASON_DUPLICATE = "duplicate"
REASON_UNATTRIBUTABLE = (
    "lastUpdate is outside every BoI publication window and no stored rate exists "
    "to attribute the restatement to"
)

_TIMESTAMP = re.compile(
    r"^(?P<base>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})"
    r"(?:\.(?P<fraction>\d+))?"
    r"(?P<tz>Z|[+-]\d{2}:\d{2})$"
)


@dataclass(frozen=True)
class SourceRate:
    key: str
    rate: Decimal
    unit: int
    last_update: datetime


@dataclass(frozen=True)
class Snapshot:
    entries: dict[str, dict[str, Any]]
    """Raw entries keyed by currency; only entries for enabled pairs are validated."""
    duplicates: frozenset[str] = frozenset()
    """Currency keys that appeared more than once (we cannot tell which is right)."""


@dataclass(frozen=True)
class RejectedCurrency:
    currency: str
    reason: str


@dataclass(frozen=True)
class IngestionBatch:
    accepted: list[RateObservation]
    rejected: list[RejectedCurrency]


def parse_payload(payload: dict[str, Any]) -> Snapshot:
    rates = payload.get("exchangeRates")
    if not isinstance(rates, list):
        raise SnapshotRejected("Missing 'exchangeRates' array")
    entries: dict[str, dict[str, Any]] = {}
    duplicates: set[str] = set()
    for item in rates:
        if not isinstance(item, dict) or not isinstance(item.get("key"), str):
            # Cannot belong to any currency. If it was "really" an enabled one, that
            # currency shows up as missing and is rejected on its own.
            log.warning("boi.entry_skipped reason=no_string_key item=%r", item)
            continue
        key = item["key"].upper()
        if key in entries:
            duplicates.add(key)
        entries[key] = item
    return Snapshot(entries, frozenset(duplicates))


def parse_last_update(value: object) -> datetime:
    """Parse BoI's ``lastUpdate`` (7 fractional digits, 'Z'); keeps microsecond precision."""
    if not isinstance(value, str):
        raise SnapshotRejected("lastUpdate must be a string")
    match = _TIMESTAMP.match(value)
    if not match:
        raise SnapshotRejected(f"Unrecognised lastUpdate format: {value!r}")
    parsed = datetime.fromisoformat(match["base"])
    fraction = (match["fraction"] or "")[:6].ljust(6, "0")
    tz_text = match["tz"]
    if tz_text == "Z":
        tz: timezone = UTC
    else:
        sign = 1 if tz_text[0] == "+" else -1
        tz = timezone(sign * timedelta(hours=int(tz_text[1:3]), minutes=int(tz_text[4:6])))
    return parsed.replace(microsecond=int(fraction), tzinfo=tz).astimezone(UTC)


def publication_date_of(last_update: datetime, tz: ZoneInfo = BOI_TIMEZONE) -> date:
    return last_update.astimezone(tz).date()


def normalise(source_rate: Decimal, unit: int) -> Decimal:
    """Value of 1 unit of the base currency (BoI may quote per 10 or 100 units)."""
    with localcontext() as ctx:
        ctx.prec = 28
        return canonical_decimal(source_rate / Decimal(unit))


def _source_rate(key: str, entry: dict[str, Any]) -> SourceRate:
    rate = entry.get("currentExchangeRate")
    if isinstance(rate, float):
        raise TypeError("BoI payload must be parsed with parse_float=Decimal")
    if isinstance(rate, bool) or not isinstance(rate, (Decimal, int)):
        raise SnapshotRejected(f"{key}: currentExchangeRate is not a number")
    rate = Decimal(rate)
    if not rate.is_finite() or rate <= 0:
        raise SnapshotRejected(f"{key}: currentExchangeRate must be > 0, got {rate}")

    unit = entry.get("unit")
    if isinstance(unit, Decimal) and unit == unit.to_integral_value():
        unit = int(unit)
    if isinstance(unit, bool) or not isinstance(unit, int) or unit <= 0:
        raise SnapshotRejected(f"{key}: unit must be a positive integer, got {unit!r}")

    return SourceRate(key, rate, unit, parse_last_update(entry.get("lastUpdate")))


def attribute_publication_date(
    last_update: datetime,
    calendar: CalendarView,
    policy: Policy,
    stored: date | None,
) -> date | None:
    """The publication date a BoI ``lastUpdate`` stands for (None: cannot tell).

    BoI's API has no date field, and ``lastUpdate`` moves whenever BoI touches an
    entry, including when it restates an earlier rate. Using its calendar date would
    turn a morning restatement of yesterday's rate into "today's publication" and
    make a stale rate ELIGIBLE after today's deadline. So:

    - inside a publication window (business day D, at or after D's expected time):
      D's publication or a same-day correction -> D;
    - anywhere else: a restatement of the rate already current, so it keeps the
      ``stored`` publication date of the pair's current observation (capped at the
      latest publication slot before ``last_update``);
    - a restatement with nothing stored -> None (rejected: fail closed).

    Raises ``PolicyNotEvaluable`` if the calendar does not cover the days needed.
    """
    slot = publication_slot(last_update, calendar, policy)
    if slot is not None:
        return slot
    if stored is None:
        return None
    bound = latest_publication_slot(last_update, calendar, policy)
    return stored if bound is None else min(stored, bound)


def build_ingestion(
    snapshot: Snapshot,
    enabled_pairs: Sequence[CurrencyPair],
    today_local: date,
    retrieved_at: datetime,
    calendar: CalendarView,
    policy: Policy,
    stored_publication_dates: Mapping[tuple[str, str], date] | None = None,
) -> IngestionBatch:
    """Validate each enabled currency independently and build its observation.

    Each entry keeps its own ``lastUpdate``; its publication date comes from
    ``attribute_publication_date`` (``stored_publication_dates`` maps
    ``(base, quote)`` to the publication date of the pair's current observation).
    A missing, duplicated or invalid entry (bad rate, unit or ``lastUpdate``, a
    ``lastUpdate`` after ``today_local``, or an unattributable restatement) is
    rejected with a reason; the others are accepted.

    Raises ``SnapshotRejected`` only for configuration errors: no enabled pairs, a
    pair that BoI cannot serve (quote other than ILS), or a calendar that does not
    cover the days needed to attribute publication dates.
    """
    if not enabled_pairs:
        raise SnapshotRejected("No enabled currency pairs configured")
    for pair in enabled_pairs:
        if pair.quote != BOI_QUOTE_CURRENCY:
            raise SnapshotRejected(f"BoI only quotes against ILS; cannot serve {pair.base}/{pair.quote}")

    accepted: list[RateObservation] = []
    rejected: list[RejectedCurrency] = []
    for pair in enabled_pairs:
        key = pair.source_key.upper()
        entry = snapshot.entries.get(key)
        if entry is None:
            rejected.append(RejectedCurrency(key, REASON_MISSING))
            continue
        if key in snapshot.duplicates:
            rejected.append(RejectedCurrency(key, REASON_DUPLICATE))
            continue
        try:
            source = _source_rate(key, entry)
        except SnapshotRejected as exc:
            rejected.append(RejectedCurrency(key, _strip_key(key, str(exc))))
            continue
        local_date = publication_date_of(source.last_update)
        if local_date > today_local:
            rejected.append(
                RejectedCurrency(
                    key,
                    f"publication date {local_date} is after today ({today_local}) in Asia/Jerusalem",
                )
            )
            continue
        try:
            published = attribute_publication_date(
                source.last_update,
                calendar,
                policy,
                (stored_publication_dates or {}).get((pair.base, pair.quote)),
            )
        except PolicyNotEvaluable as exc:
            raise SnapshotRejected(f"Cannot attribute publication dates: {exc}") from exc
        if published is None:
            rejected.append(RejectedCurrency(key, REASON_UNATTRIBUTABLE))
            continue
        accepted.append(
            RateObservation(
                id=uuid4(),
                base_currency=pair.base,
                quote_currency=pair.quote,
                rate=normalise(source.rate, source.unit),
                source_rate=source.rate,
                source_unit=source.unit,
                publication_date=published,
                source_last_update=source.last_update,
                retrieved_at=retrieved_at,
            )
        )
    return IngestionBatch(accepted, rejected)


def _strip_key(key: str, message: str) -> str:
    prefix = f"{key}: "
    return message[len(prefix):] if message.startswith(prefix) else message
