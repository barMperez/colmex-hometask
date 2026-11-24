# Tasks

## 1. Exact decimals on Postgres (D2)

- [x] 1.1 Change `DecimalText` to unconstrained `NUMERIC` on Postgres. Verify: `tests/integration/test_db_types.py::test_decimal_round_trips_exactly` gains `0.000012345678912` and `123456789.123456789` and passes on SQLite and Postgres.

## 2. Same-timestamp corrections (D1)

- [x] 2.1 Add migration `0004_revisions_and_exact_numeric`:
  - `revision INTEGER NOT NULL DEFAULT 0`;
  - the unique key becomes `(pair, source_last_update, revision)`;
  - the "latest" index includes `revision DESC`;
  - `ALTER … TYPE NUMERIC` for `rate` and `source_rate` on Postgres;
  - batch mode on SQLite;
  - a downgrade that raises when any `revision > 0` exists.

  Mirror the schema in `tables.py`. Verify: `test_schema_and_seed.py` checks the new column and unique key; `test_upgrade_downgrade_upgrade` passes; a new test shows the downgrade refusing while a correction is stored.
- [x] 2.2 Add `revision` to `RateObservation` (default 0) and the repository mappers, and order `latest_for_pair` by publication date, source timestamp, then revision. Replace `insert_many` with `store_batch` returning `StoreResult(inserted, corrections)` per D1. Verify: repository integration tests cover a first insert (revision 0), the identical value (no-op), a different rate with the same timestamp (revision 1, now current), a flip back (revision 2, current), and a different unit only (a new revision).
- [x] 2.3 In `services/ingestion.py`, add `corrections` to `IngestionResult` and to the `RATES_INGESTED` audit payload, and log `rates.same_timestamp_correction …` at WARNING. Verify: in `tests/integration/test_ingestion.py`, re-ingesting the base snapshot with USD 3.341 under the same `lastUpdate` stores revision 1, makes `latest_for_pair` return 3.341 with a new id, writes an audit record listing the correction, and logs the WARNING. Re-ingesting identical data still inserts nothing and writes no audit.
- [x] 2.4 Add a contract test: after a same-timestamp correction, `GET /api/v1/exchange-rates?base=USD&quote=ILS` returns the corrected rate with a different `rateId` than before. Verify: `pytest tests/contract` passes.

## 3. Operator actions (D3)

- [x] 3.1 Add `AlreadyInState` (generalising `AlreadyDeclared`) and the shared transaction-plus-audit helper in `services/operator.py`. Bound `declare_non_publication_day` to `[today - max_age_days, today]` (Israel time, injected clock), with a `force` flag recorded in the audit `after`. Update `scripts/declare_non_publication_day.py` with `--force`. Verify: integration tests cover in-window success, a future date refused (message names the range), a date older than the window refused, a forced declaration stored with `forced: true`, and no active policy refused without force. Existing operator tests are updated to in-window dates or `--force`, and `test_script_exit_codes` still covers exit codes 0, 2, 3 and 1.
- [x] 3.2 Add `retract_non_publication_day` and `scripts/retract_non_publication_day.py`, refusing BoI-calendar days and unknown dates. Verify: integration tests show that retracting an operator day makes it a business day again (the evaluation flips back to NOT_ELIGIBLE), the audit record holds the removed row, and a BoI-calendar day (2026-10-27) or an unknown date gives exit code 3 with no audit.
- [x] 3.3 Add `set_pair_enabled` and `scripts/set_pair_enabled.py --pair BASE/QUOTE --enable|--disable --reason --actor`. Verify: integration tests show that disabling GBP/ILS makes the next ingestion of a snapshot without GBP a `success` and GBP reads 422; re-enabling restores it; an unknown pair gives exit 2; a no-op gives exit 3; each real change writes `PAIR_ENABLED` / `PAIR_DISABLED` with before and after.
- [x] 3.4 Add `activate_policy` and `scripts/activate_policy.py`. Verify: integration tests show that activating grace 30 makes version 2 the only active policy, the next weekday deadline 15:45, and the next evaluation report `policyVersion` 2 with a `POLICY_ACTIVATED` audit holding both versions; a negative grace, a max age of 0, a malformed time or values identical to the active policy are refused with no change.

## 4. Documentation

- [x] 4.1 README:
  - §4 (the operator section): all four commands with examples and exit codes, and the declaration window and `--force`;
  - §3 and §6: same-timestamp corrections are stored as revisions, and exact decimals on both databases;
  - §7: the operator commands via `docker compose run --rm api python scripts/…`.

  Verify: each documented command runs as written against the Compose stack.
- [x] 4.2 WRITEUP §8: mark edge cases 1–4 as resolved, each with its decision and test, and update §10. Verify: §8 matches the behaviour delivered here.

## 5. Integration checks

- [x] 5.1 Run the full suite on SQLite and on Postgres (`TEST_DATABASE_URL=...@localhost:5433/rates_test`). Verify: both are green.
- [x] 5.2 Compose smoke:
  - `docker compose up --build -d` applies `0004` to the existing database (the live volume already has observations);
  - `\d rate_observations` shows `revision` and `numeric` with no precision;
  - disable then re-enable GBP/ILS through the script and see two audit rows;
  - declare a future date (refused, exit 2), then retract a test declaration.

  Verify: the captured output is added to the README smoke-run section.
- [x] 5.3 Run `openspec validate fix-review-edge-cases --strict`. Verify: it reports the change as valid.

## Workflow follow-up

- Sync the delta specs and archive the change once the tasks are done.
