# Design

## Context

See proposal.md (Why) for the three findings. The behavior contracts are in this change's `specs/`. The current state that shapes this design:

- **Parser.** `parse_last_update` already yields one `SourceRate` per entry. The shared timestamp is forced only by the "entries disagree" check in `build_observations`, which applies one `published` date to every observation.
- **Idempotency is already per entry.** The unique constraint is `(base_currency, quote_currency, source_last_update)` and `insert_many` skips conflicts, so a per-entry timestamp needs no schema change.
- **Single-timestamp assumptions.** `IngestionResult`, the refresh log line and the `RATES_INGESTED` audit record all assume one `publication_date` and one `source_last_update` per snapshot.
- **Evaluator check order.** `evaluate()` runs: future-dated → (over max age **or** `required is None`) → overdue. `required_publication_date()` returns `None` when it walks back `max_age_days + 1` days without finding a business day whose deadline has passed.
- **Separate processes.** The API and refresh run as separate processes, possibly on separate hosts. `last_success_at` lives only in the refresh loop's memory and its logs.

## Goals / Non-Goals

**Goals:**
- Make the ingestion and freshness-policy code match the modified specs, with no change to the exchange-rates response schema.
- Make the last successful ingestion visible to anything that can reach the API, without the API talking to the refresh process.

**Non-Goals:**
- Making readiness depend on freshness or on refresh health. An old success time does not take the API out of rotation; that stays a monitoring and alerting concern.
- Exposing per-pair ingestion status or retry counters over HTTP. Only `lastSuccessfulIngestionAt` is added.
- Supporting more than one refresh replica. That remains out of scope (see the original change's D6).

## Decisions

### D1. Per-entry timestamps in the parser

`build_observations` builds each observation from that entry's own `SourceRate.last_update` and `publication_date_of(...)`, and drops the "Entries disagree on lastUpdate" rejection. The future-date check moves into the per-pair loop and still raises `SnapshotRejected`, so any future-dated enabled entry rejects the whole snapshot. Missing pairs, non-positive rates and bad units keep their whole-snapshot behavior.

*Alternative:* accept differing timestamps but store the snapshot's maximum `lastUpdate` on every entry. Rejected: it would invent a correction for currencies BoI didn't touch, and break idempotency for them.

### D2. The ingestion result and audit record describe several timestamps

- `IngestionResult` carries `inserted`, plus `latest_publication_date` and `latest_source_last_update` (the maxima across the snapshot's observations), which the success log uses.
- The audit record's `entity_id` becomes the ingestion's own UUID, no longer the shared timestamp. Its `after` payload lists each inserted observation with its pair, id, `source_last_update` and `publication_date`.
- There is still exactly one audit record per ingestion that stores anything, as the spec requires.

*Alternative:* one audit record per inserted pair. Rejected: it changes the existing "one record per ingestion" contract and its scenario.

### D3. Evaluator order: future → max-age → unresolvable required date → overdue

`evaluate()` checks over-age on its own first, and only then treats `required is None` as a failure, raising `PolicyNotEvaluable("No BoI business day within max age; check the non-publication calendar")`.

- The existing "cap overrides calendar" scenario keeps MAX_AGE_EXCEEDED, because that observation is over age and is caught first.
- A recent observation under the same calendar gap now fails closed.
- The read path already maps `PolicyNotEvaluable` to 503 `POLICY_NOT_EVALUABLE` with no rate in the body, and already logs it at error level, so no API code changes.

*Alternatives:* add a new NOT_ELIGIBLE reason, or document the current behavior. The user rejected both. 503 is consistent with "calendar not loaded": both mean the calendar data can't be trusted.

### D4. A single-row `refresh_status` table, written after every attempt

Migration `0003_refresh_status` creates the table and seeds the row `id = 1` with nulls and zero. Columns:
- `id`: integer primary key, with `CHECK (id = 1)`
- `last_attempt_at`: UtcDateTime, nullable
- `last_attempt_outcome`: text, nullable
- `last_success_at`: UtcDateTime, nullable
- `consecutive_failures`: integer, not null, default 0

`SqlRefreshStatusRepository` provides:
- `record_success(at)` and `record_failure(at, outcome, consecutive_failures)`, each a plain `UPDATE ... WHERE id = 1`. Seeding plus UPDATE stays portable across SQLite and Postgres with no dialect-specific upsert.
- `get()` for the read path.

`RefreshLoop.run_once` writes the status in its **own short transaction, after** the ingestion transaction has finished. This way:
- a rolled-back or rejected ingestion is still recorded as a failed attempt;
- the status write can never roll back stored observations.

If the status write itself fails (typically during a storage outage), the loop logs `refresh.status_write_failed` and carries on; the logs remain the fallback. On startup, the loop reads `last_success_at` from the row, so its logs stay continuous across restarts.

*Alternatives:*
- **Derive the value from `audit_events`.** Rejected: successful polls that store nothing write no audit record, and that is exactly the case being fixed.
- **Write a file on a shared volume.** Rejected: assumes a shared host.
- **Have the API call the refresh process.** Rejected: adds a network dependency to readiness.
- **Append-only attempt log table.** Rejected: unbounded growth (about 300 rows a day) for no requirement; the logs already hold the history.

### D5. Readiness reads the status row

`RateQueryService.readiness()` reads `latest_retrieved_at` and the status row in the same connection. `ReadinessOut` gains `last_successful_ingestion_at: UtcInstant | None`, serialised as `lastSuccessfulIngestionAt` and documented in OpenAPI.

A missing row or a null value is reported as `null`. Readiness status and the 200/503 decision stay tied only to database reachability.

## Risks / Trade-offs

- [The API starts against a database without migration `0003`] → Compose already runs `migrate` before `api` and `refresh`. The repository treats a missing row as null, but a missing table is a startup-order bug, and the readiness probe surfaces it as an error: 503 `STORAGE_UNAVAILABLE` on SQLite, which reports it as an operational error, and 500 `INTERNAL_ERROR` on Postgres, which reports it as a programming error. We deliberately don't map programming errors to 503, because that would also disguise genuine SQL bugs as outages.
- [A status write fails while ingestion succeeded] → Logged and ignored. Readiness briefly shows an older success time; the next attempt corrects it. Observations are never affected (separate transaction).
- [Two refresh processes run by mistake] → The status row is last-writer-wins, so values may interleave. Correctness of observations still holds (per-entry idempotency). The single-writer rule is unchanged.
- [A per-entry future check lets a snapshot through if a non-enabled currency is future-dated] → Intended: entries for currencies that aren't enabled are ignored, as before.
- [The new 503 on implausible calendar data] → Rare, and logged at error level. It reads as an operational fault to fix in the calendar data, not a stale rate.

## Migration Plan

1. Deploy as usual: `migrate` (`alembic upgrade head`) runs `0003`, then `api` and `refresh` start.
2. Until the first attempt after the deploy, `lastSuccessfulIngestionAt` is null.
3. **Rollback:** `alembic downgrade 0002_seed` drops `refresh_status`, then redeploy the previous image. The previous code never reads the table, so running the old image against the migrated schema is also safe.
