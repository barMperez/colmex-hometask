import json
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from rates.adapters.boi.parser import (
    build_observations,
    normalise,
    parse_last_update,
    parse_payload,
    publication_date_of,
)
from rates.domain.errors import SnapshotRejected
from rates.domain.models import CurrencyPair
from tests.fixtures import boi_fixture

PAIRS = [CurrencyPair("EUR", "ILS", "EUR"), CurrencyPair("GBP", "ILS", "GBP"), CurrencyPair("USD", "ILS", "USD")]
RETRIEVED = datetime(2026, 11, 23, 13, 25, tzinfo=UTC)
TODAY = date(2026, 11, 24)


def load(name: str) -> dict:
    return json.loads(boi_fixture(name), parse_float=Decimal)


def build(name: str, pairs=PAIRS, today=TODAY):
    return build_observations(parse_payload(load(name)), pairs, today, RETRIEVED)


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


@pytest.mark.parametrize(
    "fixture", ["missing_gbp.json", "zero_rate.json", "future_last_update.json"]
)
def test_invalid_snapshots_rejected(fixture):
    with pytest.raises(SnapshotRejected):
        build(fixture)


def test_future_publication_date_rejected_relative_to_today():
    # future_last_update.json is dated 2026-11-25: valid once that day has arrived.
    assert len(build("future_last_update.json", today=date(2026, 11, 25))) == 3


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"exchangeRates": "nope"},
        {"exchangeRates": [{"currentExchangeRate": 3}]},
    ],
)
def test_structurally_malformed_payload_rejected(payload):
    with pytest.raises(SnapshotRejected):
        build_observations(parse_payload(payload), PAIRS, TODAY, RETRIEVED)


def test_differing_last_update_accepted_per_entry():
    payload = load("snapshot_2026-11-23.json")
    payload["exchangeRates"][0]["lastUpdate"] = "2026-11-23T14:05:00Z"  # USD only
    observations = {
        o.base_currency: o
        for o in build_observations(parse_payload(payload), PAIRS, TODAY, RETRIEVED)
    }

    assert observations["USD"].source_last_update == datetime(2026, 11, 23, 14, 5, tzinfo=UTC)
    assert observations["EUR"].source_last_update == datetime(
        2026, 11, 23, 13, 21, 10, 710489, tzinfo=UTC
    )
    assert {o.publication_date for o in observations.values()} == {date(2026, 11, 23)}


def test_single_future_dated_entry_rejects_snapshot():
    payload = load("snapshot_2026-11-23.json")
    payload["exchangeRates"][1]["lastUpdate"] = "2026-11-25T10:00:00Z"  # GBP only
    with pytest.raises(SnapshotRejected, match="GBP"):
        build_observations(parse_payload(payload), PAIRS, TODAY, RETRIEVED)


def test_future_dated_entry_of_disabled_currency_is_ignored():
    payload = load("snapshot_2026-11-23.json")
    payload["exchangeRates"][2]["lastUpdate"] = "2026-11-25T10:00:00Z"  # JPY: not enabled
    assert len(build_observations(parse_payload(payload), PAIRS, TODAY, RETRIEVED)) == 3


@pytest.mark.parametrize("unit", [0, -1, "1", True, Decimal("1.5")])
def test_invalid_unit_rejected(unit):
    payload = load("snapshot_2026-11-23.json")
    payload["exchangeRates"][0]["unit"] = unit
    with pytest.raises(SnapshotRejected):
        build_observations(parse_payload(payload), PAIRS, TODAY, RETRIEVED)


def test_float_payload_is_a_programming_error():
    payload = json.loads(boi_fixture("snapshot_2026-11-23.json"))  # floats!
    with pytest.raises(TypeError):
        build_observations(parse_payload(payload), PAIRS, TODAY, RETRIEVED)
