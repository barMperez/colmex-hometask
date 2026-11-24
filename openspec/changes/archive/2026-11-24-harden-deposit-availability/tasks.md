# Tasks

## 1. Per-currency validation (D1)

- [x] 1.1 Replace `build_observations` with `build_ingestion(...) -> IngestionBatch(accepted, rejected)` in `adapters/boi/parser.py`. Per enabled currency, reject on missing / invalid rate / invalid unit / bad `lastUpdate` / future date / duplicate key, with a reason. Skip items without a string `key`, with a warning. Keep whole-payload `SnapshotRejected` only for a missing or non-list `exchangeRates`, no enabled pairs, or a non-ILS quote. Verify: `tests/unit/test_boi_parser.py` covers each per-currency rejection reason while the other currencies are accepted, the all-invalid case, duplicate keys, a keyless item, and the remaining whole-payload rejections; `pytest tests/unit/test_boi_parser.py` passes.
- [x] 1.2 Update README §3 (refresh flow) to describe per-currency validation. Verify: no README text still says one currency rejects the snapshot (`grep -n "whole" README.md` reviewed).

## 2. Partial outcome and audit (D2)

- [x] 2.1 In `services/ingestion.py`, consume `IngestionBatch`: store the accepted currencies atomically, raise `SnapshotRejected` (with the joined reasons) when nothing is accepted, add `rejected` and `outcome` to `IngestionResult`, and add `"rejected"` to the audit record's `after`. Verify: `tests/integration/test_ingestion.py` checks that a missing GBP stores USD and EUR with an audit record listing GBP as `missing`, that the all-invalid case stores nothing, and that the existing rollback and idempotency tests still pass.
- [x] 2.2 In `refresh/loop.py`, handle `partial`: one ERROR log line per rejected currency, `record_failure(outcome="partial")`, the counter increments and `last_success_at` stays put. A following full success resets the counter. Verify: `tests/integration/test_refresh_loop.py` covers partial-then-success against `refresh_status`, and checks the ERROR lines with `caplog`.

## 3. Dense polling (D3)

- [x] 3.1 Add `refresh_dense_interval_seconds` (60) and `refresh_dense_tail_minutes` (30) to `settings.py` and `.env.example`. Extend `poll_interval_seconds` with the policy and the dense branch, and make `next_interval` load the active policy (falling back when it's unavailable). Verify: `tests/unit/test_refresh_interval.py` covers Tue 15:20 and 16:29 (dense), Fri 12:30 (dense), Tue 16:31 and 14:00 (window), Saturday (off-window), a listed holiday at 15:20 (not dense), and policy `None` (falls back); `tests/unit/test_settings_and_clock.py` checks the defaults.
- [x] 3.2 Update README §3 with the dense window and its BoI request cost. Verify: the documented times match the 3.1 test cases.

## 4. Not-eligible pairs observable (D4)

- [x] 4.1 Make `RateQueryService.readiness()` evaluate every enabled pair and return the not-eligible ones (`RATE_NOT_FOUND` when there's no observation; all pairs `POLICY_NOT_EVALUABLE` on `PolicyNotEvaluable`), and add `notEligiblePairs` to `ReadinessOut` with OpenAPI descriptions. Verify: contract tests in `tests/contract/test_exchange_rates_api.py` check the overdue-USD-only case (status 200), the all-eligible empty list, the fresh install (all `RATE_NOT_FOUND`), and the 2028 case (all `POLICY_NOT_EVALUABLE`, status still 200).
- [x] 4.2 Add per-pair transition tracking to `RefreshLoop`: evaluate after each attempt, log one ERROR (`rates.pair_not_eligible ...`) on a change to not eligible or on a first assessment that is not eligible, one INFO on recovery, and nothing while the state is unchanged. Verify: `tests/integration/test_refresh_loop.py` advances a `FixedClock` across the 2026-11-24 16:00 deadline and asserts exactly one ERROR for USD/ILS, none on the next attempts, and one INFO after a new USD snapshot.
- [x] 4.3 Document `notEligiblePairs` and the transition log lines in README §6 (Health) and §7, as the signals to alert on. Verify: the documented readiness JSON matches the 4.1 response shape.

## 5. Calendar expiry warning (D5)

- [x] 5.1 Add `CalendarView.coverage_end(today)` and `calendar_warning_days` (60) in settings. Verify: `tests/unit/test_eligibility.py` (or a new `tests/unit/test_calendar.py`) covers 2026-11-24 → 2027-12-31, a gap year (2026+2028 → 2026-12-31), and an uncovered current year → None.
- [x] 5.2 Add `calendarCoverageEndsOn` and `calendarExpiryWarning` to readiness, and make the refresh loop log `rates.calendar_expiring` at most once per Israel date while the warning is on. Verify: a contract test with the clock at 2027-11-02 gets `true` and `2027-12-31`, and at 2026-11-24 gets `false`; a refresh-loop test with two attempts on the same day and one the next day asserts exactly two warning lines.
- [x] 5.3 Update the README §6 calendar bullet with the warning threshold and what to do. Verify: the text names the setting and the readiness fields.

## 6. Operations and documentation (D6, D7)

- [x] 6.1 Add `restart: unless-stopped` to the `postgres` service in `docker-compose.yml`. Verify: `docker compose config` shows the policy, and after a simulated crash (`docker exec -u postgres <container> pg_ctl stop -m immediate`; note that `docker kill` counts as a manual stop and disables the restart policy) the container comes back on its own (`RestartCount` 1) and `/api/v1/exchange-rates?base=USD&quote=ILS` returns 200 again without manual action.
- [x] 6.2 Document the conversion direction in README §2 and the endpoint's OpenAPI description, with the 10,000 ILS / 3.343 = 2,991.3251… USD example (2,991.33 half-up, 2,991.32 rounded down) and the note that rounding is the consumer's. Verify: `/openapi.json` contains the example text, and the README example's arithmetic is correct.

## 7. Integration checks

- [x] 7.1 Run the full suite on SQLite and on Postgres (`TEST_DATABASE_URL=...@localhost:5433/rates_test`). Verify: both are green.
- [x] 7.2 Compose smoke: `docker compose up --build -d`. Check that `/health/ready` shows `notEligiblePairs: []`, `calendarCoverageEndsOn: "2027-12-31"` and `calendarExpiryWarning: false`, and that the refresh logs show a dense interval when run inside the window (or the interval selection reported in the logs otherwise). Verify: the captured output is added to the README smoke-run section.
- [x] 7.3 Run `openspec validate harden-deposit-availability --strict`. Verify: it reports the change as valid.

## Workflow follow-up

- Sync the delta specs and archive the change once the tasks are done.
- Start the separate JWT and audit change once the audit-failure question is answered.
