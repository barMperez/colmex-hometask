import json
from datetime import UTC, date, datetime, time
from decimal import Decimal

import pytest

from rates.adapters.boi.parser import (
    REASON_DUPLICATE,
    REASON_MISSING,
    REASON_UNATTRIBUTABLE,
    build_ingestion,
    normalise,
    parse_last_update,
    parse_payload,
    publication_date_of,
)
from rates.domain.calendar import CalendarView
from rates.domain.errors import SnapshotRejected
from rates.domain.models import BOI_TIMEZONE, CurrencyPair, Policy
from tests.fixtures import boi_fixture

PAIRS = [CurrencyPair("EUR", "ILS", "EUR"), CurrencyPair("GBP", "ILS", "GBP"), CurrencyPair("USD", "ILS", "USD")]
RETRIEVED = datetime(2026, 11, 23, 13, 25, tzinfo=UTC)
TODAY = date(2026, 11, 24)
POLICY = Policy(1, 45, 7, time(15, 15), time(12, 15), "Asia/Jerusalem")
CALENDAR = CalendarView(frozenset(), frozenset({2026}))


def load(name: str) -> dict:
    return json.loads(boi_fixture(name), parse_float=Decimal)


def batch_of(payload: dict, pairs=PAIRS, today=TODAY, stored=None, calendar=CALENDAR):
    return build_ingestion(
        parse_payload(payload), pairs, today, RETRIEVED, calendar, POLICY, stored
    )


def build(name: str, pairs=PAIRS, today=TODAY):
    """Accepted observations of a fixture; asserts nothing was rejected."""
    result = batch_of(load(name), pairs, today)
    assert result.rejected == []
    return result.accepted


def accepted_codes(result) -> set[str]:
    return {o.base_currency for o in result.accepted}


def rejected_map(result) -> dict[str, str]:
    return {r.currency: r.reason for r in result.rejected}


def test_real_snapshot_builds_observations():
    observations = {o.base_currency: o for o in build("snapshot_2026-11-23.json")}

    assert set(observations) == {"USD", "EUR", "GBP"}  # JPY / LBP ignored: not enabled
    usd = observations["USD"]
    assert usd.rate == Decimal("3.343") and usd.source_rate == Decimal("3.343")
    assert usd.source_unit == 1
    assert usd.publication_date == date(2026, 11, 23)
    assert usd.source_last_update == datetime(2026, 11, 23, 13, 21, 10, 710489, tzinfo=UTC)
    assert usd.retrieved_at == RETRIEVED
    assert observations["GBP"].rate == Decimal("4.3785")


def test_per_100_currency_is_normalised():
    observations = build("snapshot_2026-11-23.json", pairs=[CurrencyPair("JPY", "ILS", "JPY")])
    jpy = observations[0]
    assert jpy.source_rate == Decimal("2.1026")
    assert jpy.source_unit == 100
    assert jpy.rate == Decimal("0.021026")
    assert format(jpy.rate, "f") == "0.021026"


def test_normalise_exact():
    assert normalise(Decimal("3.343"), 1) == Decimal("3.343")
    assert format(normalise(Decimal("0.0004"), 10), "f") == "0.00004"


def test_last_update_parsing():
    assert parse_last_update("2026-11-23T13:21:10.7104898Z") == datetime(
        2026, 11, 23, 13, 21, 10, 710489, tzinfo=UTC
    )
    assert parse_last_update("2026-11-23T15:21:10+02:00") == datetime(2026, 11, 23, 13, 21, 10, tzinfo=UTC)
    with pytest.raises(SnapshotRejected):
        parse_last_update("2026-11-23T13:21:10")  # no timezone: ambiguous


def test_publication_date_uses_israel_date():
    assert publication_date_of(parse_last_update("2026-11-23T13:21:10.7104898Z")) == date(2026, 11, 23)
    assert publication_date_of(parse_last_update("2026-11-23T22:30:00Z")) == date(2026, 11, 24)


# --- Per-currency validation ------------------------------------------------

def test_missing_currency_rejected_alone():
    result = batch_of(load("missing_gbp.json"))
    assert accepted_codes(result) == {"USD", "EUR"}
    assert rejected_map(result) == {"GBP": REASON_MISSING}


def test_non_positive_rate_rejected_alone():
    result = batch_of(load("zero_rate.json"))
    assert accepted_codes(result) == {"USD", "GBP"}
    assert "must be > 0" in rejected_map(result)["EUR"]


def test_future_dated_entry_rejected_alone():
    payload = load("snapshot_2026-11-23.json")
    payload["exchangeRates"][1]["lastUpdate"] = "2026-11-25T10:00:00Z"  # GBP only
    result = batch_of(payload)
    assert accepted_codes(result) == {"USD", "EUR"}
    assert "after today" in rejected_map(result)["GBP"]


def test_every_currency_invalid():
    result = batch_of(load("future_last_update.json"))  # all dated 2026-11-25
    assert result.accepted == []
    assert set(rejected_map(result)) == {"USD", "EUR", "GBP"}


def test_future_publication_date_rejected_relative_to_today():
    # future_last_update.json is dated 2026-11-25: valid once that day has arrived.
    assert len(build("future_last_update.json", today=date(2026, 11, 25))) == 3


def test_bad_last_update_rejected_alone():
    payload = load("snapshot_2026-11-23.json")
    payload["exchangeRates"][0]["lastUpdate"] = "yesterday"  # USD
    result = batch_of(payload)
    assert accepted_codes(result) == {"EUR", "GBP"}
    assert "lastUpdate" in rejected_map(result)["USD"]


@pytest.mark.parametrize("unit", [0, -1, "1", True, Decimal("1.5")])
def test_invalid_unit_rejected_alone(unit):
    payload = load("snapshot_2026-11-23.json")
    payload["exchangeRates"][0]["unit"] = unit  # USD
    result = batch_of(payload)
    assert accepted_codes(result) == {"EUR", "GBP"}
    assert "unit" in rejected_map(result)["USD"]


def test_duplicate_enabled_currency_rejected_alone():
    payload = load("snapshot_2026-11-23.json")
    payload["exchangeRates"].append(dict(payload["exchangeRates"][0], currentExchangeRate=Decimal("9")))
    result = batch_of(payload)
    assert accepted_codes(result) == {"EUR", "GBP"}
    assert rejected_map(result) == {"USD": REASON_DUPLICATE}


def test_keyless_item_is_skipped(caplog):
    payload = load("snapshot_2026-11-23.json")
    payload["exchangeRates"].append({"currentExchangeRate": Decimal("3")})
    with caplog.at_level("WARNING", logger="rates.adapters.boi.parser"):
        assert len(batch_of(payload).accepted) == 3
    assert "boi.entry_skipped" in caplog.text


def test_disabled_currencies_are_ignored_even_if_invalid():
    payload = load("snapshot_2026-11-23.json")
    payload["exchangeRates"][2]["lastUpdate"] = "2026-11-25T10:00:00Z"  # JPY: not enabled
    payload["exchangeRates"][4]["currentExchangeRate"] = Decimal("0")  # LBP: not enabled
    result = batch_of(payload)
    assert len(result.accepted) == 3 and result.rejected == []


def test_differing_last_update_accepted_per_entry():
    payload = load("snapshot_2026-11-23.json")
    payload["exchangeRates"][0]["lastUpdate"] = "2026-11-23T14:05:00Z"  # USD only
    observations = {o.base_currency: o for o in batch_of(payload).accepted}

    assert observations["USD"].source_last_update == datetime(2026, 11, 23, 14, 5, tzinfo=UTC)
    assert observations["EUR"].source_last_update == datetime(
        2026, 11, 23, 13, 21, 10, 710489, tzinfo=UTC
    )
    assert {o.publication_date for o in observations.values()} == {date(2026, 11, 23)}


# --- Publication-date attribution (regression: lastUpdate is not the rate's date) ---

def il(y: int, m: int, d: int, hh: int, mm: int = 0) -> str:
    """An Israel-local wall-clock time as a BoI lastUpdate string (UTC, 'Z')."""
    instant = datetime(y, m, d, hh, mm, tzinfo=BOI_TIMEZONE).astimezone(UTC)
    return instant.strftime("%Y-%m-%dT%H:%M:%SZ")


def usd_with_last_update(last_update: str) -> dict:
    payload = load("snapshot_2026-11-23.json")
    payload["exchangeRates"][0]["lastUpdate"] = last_update  # USD only
    return payload


def usd_publication_date(last_update: str, today=TODAY, stored=None):
    result = batch_of(usd_with_last_update(last_update), today=today, stored=stored)
    usd = [o for o in result.accepted if o.base_currency == "USD"]
    return (usd[0].publication_date if usd else None), rejected_map(result)


STORED_MONDAY = {("USD", "ILS"): date(2026, 11, 23)}


def test_morning_restatement_is_not_todays_publication():
    # Monday's rate re-stamped by BoI on Tuesday 10:00: it is still Monday's rate.
    # Dating it Tuesday made it ELIGIBLE after Tuesday's deadline with no Tuesday rate.
    published, rejected = usd_publication_date(il(2026, 11, 24, 10), stored=STORED_MONDAY)
    assert published == date(2026, 11, 23)
    assert rejected == {}


def test_restatement_after_a_missed_day_keeps_the_stored_date():
    # No Tuesday publication; Monday's rate restated on Wednesday morning.
    # It must not be dated Tuesday (the latest slot) either: it keeps Monday.
    published, _ = usd_publication_date(
        il(2026, 11, 25, 9), today=date(2026, 11, 25), stored=STORED_MONDAY
    )
    assert published == date(2026, 11, 23)


def test_weekend_touch_keeps_fridays_date():
    published, _ = usd_publication_date(
        il(2026, 11, 28, 11), today=date(2026, 11, 28), stored={("USD", "ILS"): date(2026, 11, 27)}
    )
    assert published == date(2026, 11, 27)


def test_restatement_with_no_stored_rate_is_rejected():
    published, rejected = usd_publication_date(il(2026, 11, 24, 10))
    assert published is None
    assert rejected == {"USD": REASON_UNATTRIBUTABLE}


@pytest.mark.parametrize(
    ("last_update", "expected"),
    [
        (il(2026, 11, 24, 15, 15), date(2026, 11, 24)),  # exactly the expected time
        (il(2026, 11, 24, 20, 0), date(2026, 11, 24)),   # same-evening correction
        (il(2026, 11, 24, 15, 14), date(2026, 11, 23)),  # a minute early: a restatement
        (il(2026, 11, 27, 12, 15), date(2026, 11, 27)),  # Friday publishes at 12:15
    ],
)
def test_publication_window_boundaries(last_update, expected):
    published, _ = usd_publication_date(last_update, today=date(2026, 11, 27), stored=STORED_MONDAY)
    assert published == expected


def test_holiday_touch_is_a_restatement():
    holiday = CalendarView(frozenset({date(2026, 11, 24)}), frozenset({2026}))
    result = batch_of(
        usd_with_last_update(il(2026, 11, 24, 16)), stored=STORED_MONDAY, calendar=holiday
    )
    usd = next(o for o in result.accepted if o.base_currency == "USD")
    assert usd.publication_date == date(2026, 11, 23)


def test_uncovered_calendar_rejects_the_snapshot():
    with pytest.raises(SnapshotRejected, match="attribute"):
        batch_of(load("snapshot_2026-11-23.json"), calendar=CalendarView(frozenset(), frozenset()))


# --- Whole-payload rejections -----------------------------------------------

@pytest.mark.parametrize("payload", [{}, {"exchangeRates": "nope"}])
def test_structurally_malformed_payload_rejected(payload):
    with pytest.raises(SnapshotRejected):
        parse_payload(payload)


def test_no_enabled_pairs_rejected():
    with pytest.raises(SnapshotRejected, match="No enabled"):
        batch_of(load("snapshot_2026-11-23.json"), pairs=[])


def test_non_ils_quote_rejected():
    with pytest.raises(SnapshotRejected, match="ILS"):
        batch_of(load("snapshot_2026-11-23.json"), pairs=[CurrencyPair("USD", "EUR", "USD")])


def test_float_payload_is_a_programming_error():
    payload = json.loads(boi_fixture("snapshot_2026-11-23.json"))  # floats!
    with pytest.raises(TypeError):
        batch_of(payload)
