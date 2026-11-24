import json
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from rates.adapters.boi.parser import (
    REASON_DUPLICATE,
    REASON_MISSING,
    build_ingestion,
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


def batch_of(payload: dict, pairs=PAIRS, today=TODAY):
    return build_ingestion(parse_payload(payload), pairs, today, RETRIEVED)


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
        assert len(build_ingestion(parse_payload(payload), PAIRS, TODAY, RETRIEVED).accepted) == 3
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
