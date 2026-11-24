"""Parse, validate and normalise a BoI snapshot into rate observations.

The snapshot is validated as a whole: any failure rejects everything.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal, localcontext
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from rates.domain.decimals import canonical_decimal
from rates.domain.errors import SnapshotRejected
from rates.domain.models import BOI_TIMEZONE, CurrencyPair, RateObservation

BOI_QUOTE_CURRENCY = "ILS"

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


def parse_payload(payload: dict[str, Any]) -> Snapshot:
    rates = payload.get("exchangeRates")
    if not isinstance(rates, list):
        raise SnapshotRejected("Missing 'exchangeRates' array")
    entries: dict[str, dict[str, Any]] = {}
    for item in rates:
        if not isinstance(item, dict) or not isinstance(item.get("key"), str):
            raise SnapshotRejected("Malformed 'exchangeRates' entry")
        key = item["key"].upper()
        if key in entries:
            raise SnapshotRejected(f"Duplicate entry for {key}")
        entries[key] = item
    return Snapshot(entries)


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


def build_observations(
    snapshot: Snapshot,
    enabled_pairs: Sequence[CurrencyPair],
    today_local: date,
    retrieved_at: datetime,
) -> list[RateObservation]:
    """Validate the snapshot for the enabled pairs and build observations.

    Raises ``SnapshotRejected`` if any enabled pair is missing or invalid, if the
    entries disagree on ``lastUpdate``, or if the publication date is in the future.
    """
    if not enabled_pairs:
        raise SnapshotRejected("No enabled currency pairs configured")

    sources: list[tuple[CurrencyPair, SourceRate]] = []
    for pair in enabled_pairs:
        if pair.quote != BOI_QUOTE_CURRENCY:
            raise SnapshotRejected(f"BoI only quotes against ILS; cannot serve {pair.base}/{pair.quote}")
        entry = snapshot.entries.get(pair.source_key.upper())
        if entry is None:
            raise SnapshotRejected(f"Snapshot is missing enabled currency {pair.source_key}")
        sources.append((pair, _source_rate(pair.source_key, entry)))

    timestamps = {source.last_update for _, source in sources}
    if len(timestamps) != 1:
        raise SnapshotRejected("Entries disagree on lastUpdate")
    last_update = timestamps.pop()
    published = publication_date_of(last_update)
    if published > today_local:
        raise SnapshotRejected(
            f"Publication date {published} is after today ({today_local}) in Asia/Jerusalem"
        )

    return [
        RateObservation(
            id=uuid4(),
            base_currency=pair.base,
            quote_currency=pair.quote,
            rate=normalise(source.rate, source.unit),
            source_rate=source.rate,
            source_unit=source.unit,
            publication_date=published,
            source_last_update=last_update,
            retrieved_at=retrieved_at,
        )
        for pair, source in sources
    ]
