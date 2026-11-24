import json
from datetime import UTC, date, datetime, time
from decimal import Decimal
from uuid import uuid4

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
from rates.domain.models import BOI_TIMEZONE, CurrencyPair, Policy, RateObservation
from tests.fixtures import boi_fixture

PAIRS = [CurrencyPair("EUR", "ILS", "EUR"), CurrencyPair("GBP", "ILS", "GBP"), CurrencyPair("USD", "ILS", "USD")]
RETRIEVED = datetime(2026, 11, 23, 13, 25, tzinfo=UTC)
TODAY = date(2026, 11, 24)
POLICY = Policy(1, 45, 7, time(15, 15), time(12, 15), "Asia/Jerusalem")
CALENDAR = CalendarView(frozenset(), frozenset({2026}))


def load(name: str) -> dict:
    return json.loads(boi_fixture(name), parse_float=Decimal)


def batch_of(payload: dict, pairs=PAIRS, today=TODAY, current=None, calendar=CALENDAR):
    return build_ingestion(
        parse_payload(payload), pairs, today, RETRIEVED, calendar, POLICY, current
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


# --- Publication-date attribution and re-stamps -----------------------------
# Regression: publicationDate was the calendar date of lastUpdate, and any new
# lastUpdate was stored as a new publication. BoI can move lastUpdate without
# publishing, which made a stale rate ELIGIBLE after the day's deadline.

MONDAY_LU = datetime(2026, 11, 23, 13, 21, 10, 710489, tzinfo=UTC)


def monday_change() -> Decimal:
    """USD currentChange in snapshot_2026-11-23.json, as the parser canonicalises it."""
    entry = load("snapshot_2026-11-23.json")["exchangeRates"][0]
    assert entry["key"] == "USD"
    return entry["currentChange"].normalize()


MONDAY_CHANGE = monday_change()


def il(y: int, m: int, d: int, hh: int, mm: int = 0) -> str:
    """An Israel-local wall-clock time as a BoI lastUpdate string (UTC, 'Z')."""
    instant = datetime(y, m, d, hh, mm, tzinfo=BOI_TIMEZONE).astimezone(UTC)
    return instant.strftime("%Y-%m-%dT%H:%M:%SZ")


def stored_usd(
    publication_date=date(2026, 11, 23),
    rate="3.343",
    change=MONDAY_CHANGE,
    unit=1,
    last_update=MONDAY_LU,
) -> dict:
    """The current stored USD/ILS observation, as ingestion passes it to the parser."""
    obs = RateObservation(
        id=uuid4(), base_currency="USD", quote_currency="ILS",
        rate=Decimal(rate) / unit, source_rate=Decimal(rate), source_unit=unit,
        publication_date=publication_date, source_last_update=last_update,
        retrieved_at=last_update, source_change=change,
    )
    return {("USD", "ILS"): obs}


def usd_entry(last_update: str, rate="3.343", change=MONDAY_CHANGE, unit=1) -> dict:
    payload = load("snapshot_2026-11-23.json")
    entry = payload["exchangeRates"][0]
    entry.update(lastUpdate=last_update, currentExchangeRate=Decimal(rate), unit=unit)
    if change is None:
        entry.pop("currentChange")
    else:
        entry["currentChange"] = change
    return payload


def usd_result(payload: dict, today=date(2026, 11, 30), current=None, calendar=CALENDAR):
    result = batch_of(payload, today=today, current=current, calendar=calendar)
    usd = [o for o in result.accepted if o.base_currency == "USD"]
    restamped = [r for r in result.restamped if r.currency == "USD"]
    return (usd[0] if usd else None), restamped, rejected_map(result)


# currentChange parsing

def test_current_change_is_parsed_exactly():
    usd = {o.base_currency: o for o in build("snapshot_2026-11-23.json")}["USD"]
    assert isinstance(usd.source_change, Decimal)
    assert usd.source_change == MONDAY_CHANGE


@pytest.mark.parametrize(
    ("change", "expected"),
    [(0, Decimal(0)), (Decimal("-0.0895"), Decimal("-0.0895")), (Decimal("0.500"), Decimal("0.5"))],
)
def test_current_change_values(change, expected):
    usd, _, rejected = usd_result(usd_entry(il(2026, 11, 23, 15, 21), change=change))
    assert rejected == {} and usd.source_change == expected


def test_missing_current_change_is_accepted_as_unknown():
    usd, _, rejected = usd_result(usd_entry(il(2026, 11, 23, 15, 21), change=None))
    assert rejected == {} and usd.source_change is None


@pytest.mark.parametrize("change", ["0.5", True, [1], Decimal("NaN"), Decimal("Infinity")])
def test_invalid_current_change_rejected_alone(change):
    payload = load("snapshot_2026-11-23.json")
    payload["exchangeRates"][0]["currentChange"] = change  # USD
    result = batch_of(payload)
    assert accepted_codes(result) == {"EUR", "GBP"}
    assert "currentChange" in rejected_map(result)["USD"]


def test_float_current_change_is_a_programming_error():
    payload = load("snapshot_2026-11-23.json")
    payload["exchangeRates"][0]["currentChange"] = 0.5
    with pytest.raises(TypeError):
        batch_of(payload)


# Re-stamps: a new lastUpdate with the same content is not a publication

@pytest.mark.parametrize(
    "last_update",
    [
        il(2026, 11, 24, 17, 0),   # inside Tuesday's window: the case timing alone cannot catch
        il(2026, 11, 24, 15, 15),  # exactly Tuesday's expected publication time
        il(2026, 11, 24, 10, 0),   # Tuesday morning
        il(2026, 11, 25, 9, 0),    # after a missed Tuesday
        il(2026, 11, 28, 11, 0),   # Saturday
    ],
)
def test_restamp_is_not_a_publication(last_update):
    usd, restamped, rejected = usd_result(usd_entry(last_update), current=stored_usd())
    assert usd is None and rejected == {}
    assert len(restamped) == 1
    assert restamped[0].current.publication_date == date(2026, 11, 23)
    assert restamped[0].last_update == parse_last_update(last_update)


def test_unchanged_rate_publication_is_a_publication():
    # BoI publishes Tuesday's rate equal to Monday's: currentChange becomes 0.
    usd, restamped, _ = usd_result(usd_entry(il(2026, 11, 24, 15, 21), change=0), current=stored_usd())
    assert restamped == []
    assert usd.publication_date == date(2026, 11, 24)
    assert usd.source_change == 0


@pytest.mark.parametrize(
    ("rate", "change", "unit"),
    [("3.351", Decimal("0.239"), 1), ("3.343", Decimal("0.4"), 1), ("33.43", None, 10)],
    ids=["new-rate", "new-change", "new-unit"],
)
def test_any_content_change_is_a_publication(rate, change, unit):
    change = MONDAY_CHANGE if change is None else change
    usd, restamped, _ = usd_result(
        usd_entry(il(2026, 11, 24, 15, 21), rate=rate, change=change, unit=unit), current=stored_usd()
    )
    assert restamped == [] and usd.publication_date == date(2026, 11, 24)


def test_numerically_equal_change_is_still_a_restamp():
    # 0.50 and 0.5 are the same number: formatting must not fake a publication.
    current = stored_usd(change=Decimal("0.5"))
    _, restamped, _ = usd_result(usd_entry(il(2026, 11, 24, 17), change=Decimal("0.50")), current=current)
    assert len(restamped) == 1


@pytest.mark.parametrize(
    ("incoming", "stored"),
    [("missing", "known"), ("known", "missing"), ("missing", "missing")],
)
def test_unknown_change_fails_closed_as_restamp(incoming, stored):
    usd, restamped, _ = usd_result(
        usd_entry(il(2026, 11, 24, 17), change=None if incoming == "missing" else MONDAY_CHANGE),
        current=stored_usd(change=None if stored == "missing" else MONDAY_CHANGE),
    )
    assert usd is None and len(restamped) == 1


def test_unknown_change_with_new_rate_is_a_publication():
    usd, restamped, _ = usd_result(
        usd_entry(il(2026, 11, 24, 15, 21), rate="3.351", change=None), current=stored_usd(change=None)
    )
    assert restamped == [] and usd.publication_date == date(2026, 11, 24)


def test_same_last_update_is_never_a_restamp():
    # A re-fetch (or a same-timestamp correction) goes to the store, which dedupes it.
    usd, restamped, _ = usd_result(usd_entry("2026-11-23T13:21:10.7104898Z"), current=stored_usd())
    assert restamped == [] and usd.publication_date == date(2026, 11, 23)


def test_restamp_is_per_currency():
    payload = load("snapshot_2026-11-24.json")  # genuine Tuesday publication...
    usd = payload["exchangeRates"][0]           # ...except USD, which only re-stamps Monday
    assert usd["key"] == "USD"
    usd.update(currentExchangeRate=Decimal("3.343"), currentChange=MONDAY_CHANGE, lastUpdate=il(2026, 11, 24, 17))
    result = batch_of(payload, current=stored_usd())
    assert accepted_codes(result) == {"EUR", "GBP"}
    assert [r.currency for r in result.restamped] == ["USD"]
    assert {o.publication_date for o in result.accepted} == {date(2026, 11, 24)}


def test_no_stored_rate_means_no_restamp():
    usd, restamped, _ = usd_result(usd_entry(il(2026, 11, 24, 17)))
    assert restamped == [] and usd.publication_date == date(2026, 11, 24)


# Corrections (new content) outside a publication window keep the stored date

def test_morning_correction_keeps_the_stored_date():
    usd, _, rejected = usd_result(usd_entry(il(2026, 11, 24, 10), rate="3.341"), current=stored_usd())
    assert rejected == {}
    assert usd.publication_date == date(2026, 11, 23)
    assert usd.source_rate == Decimal("3.341")


def test_correction_after_a_missed_day_keeps_the_stored_date():
    usd, _, _ = usd_result(usd_entry(il(2026, 11, 25, 9), rate="3.341"), current=stored_usd())
    assert usd.publication_date == date(2026, 11, 23)  # not Tuesday, the latest slot


def test_weekend_correction_keeps_fridays_date():
    current = stored_usd(publication_date=date(2026, 11, 27))
    usd, _, _ = usd_result(usd_entry(il(2026, 11, 28, 11), rate="3.341"), current=current)
    assert usd.publication_date == date(2026, 11, 27)


def test_holiday_correction_keeps_the_stored_date():
    holiday = CalendarView(frozenset({date(2026, 11, 24)}), frozenset({2026}))
    usd, _, _ = usd_result(
        usd_entry(il(2026, 11, 24, 16), rate="3.341"), current=stored_usd(), calendar=holiday
    )
    assert usd.publication_date == date(2026, 11, 23)


def test_stored_date_is_capped_at_the_latest_slot():
    # Stored row dated Wednesday, but BoI sends a Tuesday-morning lastUpdate:
    # it cannot belong to anything later than Monday's publication.
    current = stored_usd(publication_date=date(2026, 11, 25))
    usd, _, _ = usd_result(usd_entry(il(2026, 11, 24, 10), rate="3.341"), current=current)
    assert usd.publication_date == date(2026, 11, 23)


def test_correction_outside_window_with_no_stored_rate_is_rejected():
    usd, _, rejected = usd_result(usd_entry(il(2026, 11, 24, 10), rate="3.341"))
    assert usd is None
    assert rejected == {"USD": REASON_UNATTRIBUTABLE}


@pytest.mark.parametrize(
    ("last_update", "expected"),
    [
        (il(2026, 11, 24, 15, 15), date(2026, 11, 24)),  # exactly the expected time
        (il(2026, 11, 24, 23, 59), date(2026, 11, 24)),  # same-evening correction
        (il(2026, 11, 27, 12, 15), date(2026, 11, 27)),  # Friday publishes at 12:15
        (il(2026, 7, 1, 15, 15), date(2026, 7, 1)),      # summer time (UTC+3)
    ],
)
def test_new_content_inside_a_window_is_that_days_publication(last_update, expected):
    usd, restamped, _ = usd_result(usd_entry(last_update, rate="3.341"), current=stored_usd())
    assert restamped == [] and usd.publication_date == expected


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
