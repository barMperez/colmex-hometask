# Proposal

## Why

Verifying `add-exchange-rates-api` turned up three places where the code and the specs disagree:

- **Ingestion is stricter than the spec.** It rejects a whole BoI snapshot when its currencies carry different `lastUpdate` timestamps. If BoI corrected a single currency during the day, every pair would stop updating and turn NOT_ELIGIBLE after the next deadline.
- **The evaluator mislabels one fail-closed case.** When the calendar data leaves no business day in the walk-back window, it labels even a fresh rate MAX_AGE_EXCEEDED, which hides a calendar-data fault behind a misleading reason.
- **Readiness reports the wrong time.** It reports when a rate was last *stored*, not when ingestion last *succeeded*, so a healthy refresh process looks idle whenever BoI has nothing new.

## What Changes

- **Ingestion: one timestamp per currency.** Each currency is ingested using its own `lastUpdate` and publication date. If BoI corrects only one currency, just that currency gets a new observation; the others are skipped as duplicates. The other snapshot checks (every enabled pair present, rate > 0, unit a positive integer) still apply to the whole snapshot, and the future-date check now runs per entry: any future-dated entry still rejects the whole snapshot.
- **Freshness policy: calendar gaps fail closed.** When no business day can be found within the max-age window, the system reports policy-not-evaluable, so reads return 503 `POLICY_NOT_EVALUABLE` with no rate. Future-dated and over-age rates still get their existing reasons, because those checks run first. No new value is added to the API's reason enum.
- **Readiness: report the last successful ingestion.** After every attempt, the refresh process records its outcome in the database: last attempt, last success, outcome, and consecutive failures. `/health/ready` adds `lastSuccessfulIngestionAt`, which is null until the first success. `latestRetrievedAt` keeps its current meaning. Readiness still does not depend on freshness.

There are no breaking changes. The only API change is one new readiness field. A response that used to be 200 MAX_AGE_EXCEEDED becomes 503 only when the calendar data is implausible.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `rate-ingestion`: publication date and idempotency are now per entry, and snapshot validation drops the shared-`lastUpdate` rule. A single-currency correction scenario is added. The last successful ingestion must be observable through the readiness endpoint, not only through the logs.
- `rate-freshness-policy`: an unresolvable required publication date for a rate that is not over age is now a policy-not-evaluable failure.

## Impact

- **Code:**
  - `src/rates/adapters/boi/parser.py`: per-entry timestamps and future-date check
  - `src/rates/services/ingestion.py`: result and audit record cover per-entry timestamps
  - `src/rates/domain/eligibility.py`: check order and fail-closed case
  - `src/rates/refresh/loop.py`: writes refresh status
  - `src/rates/adapters/db/tables.py` and `repos.py`: new `refresh_status` table and repository
  - `src/rates/services/rate_query.py` and `src/rates/api/schemas.py`: readiness field
- **Database:** new Alembic migration `0003` adding a single-row `refresh_status` table, on both SQLite and Postgres.
- **API:** `/health/ready` gains `lastSuccessfulIngestionAt`. The exchange-rates endpoint contract is unchanged, apart from the rare 503 above.
- **Tests:** parser, eligibility table, ingestion and refresh-loop integration, contract test for readiness, and the migration upgrade/downgrade test.
- **Docs:** README §4 (policy), §6 (limitations) and §7 (health).
