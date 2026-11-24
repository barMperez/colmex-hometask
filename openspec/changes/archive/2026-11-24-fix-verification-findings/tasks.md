# Tasks

## 1. Ingestion: per-entry timestamps (D1, D2)

- [x] 1.1 In `adapters/boi/parser.py`, build each observation from its own entry's `lastUpdate` and publication date, drop the "Entries disagree on lastUpdate" rejection, and run the future-date check per enabled entry. Verify: in `tests/unit/test_boi_parser.py`, replace `test_inconsistent_last_update_rejected` with a test that differing timestamps are accepted and stored per entry, add a test that one future-dated GBP entry rejects the snapshot, and confirm the existing future/missing/zero-rate tests still pass.
- [x] 1.2 Change `IngestionResult` to `inserted`, `latest_publication_date` and `latest_source_last_update`. Make the `RATES_INGESTED` audit record use the ingestion's UUID as `entity_id` and list each inserted observation's pair, id, `source_last_update` and `publication_date`. Update the refresh success log line to match. Verify: `tests/integration/test_ingestion.py::test_ingest_snapshot` asserts the new audit payload, and `pytest tests/integration/test_ingestion.py tests/integration/test_refresh_loop.py` passes.
- [x] 1.3 Add fixture `tests/fixtures/boi/usd_only_correction.json` (USD at 14:05Z / 3.341; EUR and GBP unchanged at 13:21:10Z) and an integration test: after the base snapshot, ingesting it stores exactly one new observation (USD), writes one audit record naming only USD/ILS, and leaves the current EUR and GBP observations unchanged. Verify: the test passes on SQLite.

## 2. Freshness policy: unresolvable required date fails closed (D3)

- [x] 2.1 In `domain/eligibility.py`, check over-age on its own, then raise `PolicyNotEvaluable` when `required is None`, keeping the order future → max-age → unresolvable → overdue. Update the docstring. Verify: in `tests/unit/test_eligibility.py`, the existing "max-age cap overrides wrong calendar" case still yields MAX_AGE_EXCEEDED, and a new test with the same calendar and an observation dated 2026-11-25 raises `PolicyNotEvaluable`.
- [x] 2.2 Add a contract test that a read under that calendar gap returns 503 `POLICY_NOT_EVALUABLE` with no `rate` in the body. Verify: `pytest tests/contract` passes.
- [x] 2.3 Update README §4 (check order) and §6 (an implausible calendar fails closed with 503). Verify: the README wording matches the 2.1 test cases.

## 3. Refresh status persistence (D4)

- [x] 3.1 Add the `refresh_status` table to `adapters/db/tables.py`, and Alembic migration `0003_refresh_status` (down_revision `0002_seed`) creating it with `CHECK (id = 1)` and seeding row 1 (nulls, `consecutive_failures` 0). Verify: `tests/integration/test_schema_and_seed.py` asserts the seeded row, and `test_upgrade_downgrade_upgrade` still passes.
- [x] 3.2 Implement `SqlRefreshStatusRepository` with `record_success`, `record_failure` and `get` (UPDATE on row 1; a missing row reads as all-null). Verify: integration tests in `tests/integration/test_repositories.py` cover a success, a failure that keeps the earlier `last_success_at`, and a missing row.
- [x] 3.3 In `RefreshLoop`, load `last_success_at` from the row at startup, and after every attempt write the status in a separate transaction after ingestion. Catch and log `refresh.status_write_failed` without stopping the loop. Verify: `tests/integration/test_refresh_loop.py` covers a success recorded with the clock time, a BoI failure recorded as `boi_unavailable` with the incremented counter and unchanged `last_success_at`, a duplicate snapshot still counted as a success, and a failing status write that leaves the loop running and observations intact.

## 4. Readiness exposes the last successful ingestion (D5)

- [x] 4.1 Make `RateQueryService.readiness()` read the status row in the same connection, and add `last_successful_ingestion_at` to `ReadinessOut`, serialised as `lastSuccessfulIngestionAt` and visible in OpenAPI. Verify: contract tests in `tests/contract/test_exchange_rates_api.py` check that it is null before any success, and that after a success with nothing new it shows the success time while `latestRetrievedAt` keeps the older retrieval time.
- [x] 4.2 Update README §6 (the health bullet) and §7 to describe `lastSuccessfulIngestionAt` versus `latestRetrievedAt`. Verify: the documented `curl localhost:8000/health/ready` output matches the 4.1 response shape.

## 5. Integration checks

- [x] 5.1 Run the full suite on SQLite and on Postgres (`TEST_DATABASE_URL=...@localhost:5433/rates_test`). Verify: both are green.
- [x] 5.2 Compose smoke: `docker compose up --build -d`, then confirm `migrate` applies `0003` and `/health/ready` shows a non-null `lastSuccessfulIngestionAt` after the first poll. Then stop the refresh process, run it with an unreachable `BOI_URL`, and confirm `lastSuccessfulIngestionAt` does not change. Verify: the captured output is added to the README smoke-run section.
- [x] 5.3 Run `openspec validate fix-verification-findings --strict`. Verify: it reports the change as valid.

## Workflow follow-up

- Sync the delta specs and archive the change once the tasks are done.
