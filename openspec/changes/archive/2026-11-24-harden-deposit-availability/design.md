# Design

## Context

See proposal.md (Why) for the deposit-flow standard; the behavior contracts are in this change's `specs/`. The relevant parts of today's code:

- `adapters/boi/parser.py`: `parse_payload` rejects a non-list `exchangeRates`, entries without a string `key`, and duplicate keys, all for the whole snapshot. `build_observations` raises `SnapshotRejected` on the first missing or invalid enabled currency, so one bad entry rejects all of them.
- `services/ingestion.py`: fetch → parse → build → one transaction (`insert_many` + audit) → `IngestionResult(inserted, latest_*)`.
- `refresh/loop.py`:
  - `run_once` classifies the attempt by exception type and writes `refresh_status` in its own transaction.
  - `poll_interval_seconds(now, calendar, settings)` picks between the 12:00–18:00 window interval and the off-window interval.
  - `next_interval` loads the calendar before each sleep.
- `domain/calendar.py`: `CalendarView(non_publication_days, covered_years)`. `domain/eligibility.evaluate` is pure.
- `services/rate_query.readiness()` reads `latest_retrieved_at` and `refresh_status`.
- `docker-compose.yml`: `restart: unless-stopped` comes only through the `x-app` anchor; the `postgres` service has none.

## Goals / Non-Goals

**Goals:**
- Remove every *unjustified* way the service blocks a USD conversion, and make the *justified* ones visible to an operator within one polling interval.
- Keep the exchange-rates endpoint contract unchanged.

**Non-Goals:**
- **Database high availability.** Replication and failover are out of scope for this service; a single Postgres instance is a documented limitation.
- **An inverse `ILS/USD` pair.** That would make this service own the rounding of money. It stays documented, not served.
- **Changing the policy values** (45-minute grace, 7-day cap). They remain business assumptions in policy v1.
- **Metrics endpoints or an alerting stack.** Signals are emitted as readiness fields and log lines that existing tooling can alert on.
- **Readiness status depending on freshness.** It still means "can serve" (database reachable), so a stale rate never takes the API out of rotation.

## Decisions

### D1. The parser returns accepted and rejected entries instead of raising

`build_observations` becomes `build_ingestion(...) -> IngestionBatch(accepted: list[RateObservation], rejected: list[RejectedCurrency(code, reason)])`. Within the per-pair loop:
- A missing entry is rejected with reason `missing`.
- An invalid rate, unit or `lastUpdate`, or a future date, is rejected with the existing message as its reason.
- A duplicate key for an enabled currency is rejected with reason `duplicate`, because we can't know which of the two is right.

What still raises `SnapshotRejected` for the whole payload:
- `exchangeRates` is missing or not a list;
- there are no enabled pairs;
- an enabled pair quotes against something other than ILS. That's a configuration error, not data.

Items without a string `key` are skipped with a warning, because they can't belong to any currency. If one was "really" USD, USD shows up as missing and is rejected on its own, which is safe.

A float rate stays a `TypeError`: that's a programming error, not a data problem.

*Alternative:* keep raising, and catch and retry per currency in the service. Rejected: control flow by exception for an expected data condition, and the reasons are harder to collect.

### D2. Outcome classification lives in the ingestion service

`IngestionResult` gains `rejected: list[RejectedCurrency]` and an `outcome` property:
- `success`: no rejections;
- `partial`: at least one accepted and at least one rejected.

When nothing is accepted, the service raises `SnapshotRejected` with the joined reasons, so the loop's existing `snapshot_rejected` failure path applies unchanged.

In `RefreshLoop.run_once`:
- A `partial` result logs one ERROR line per rejected currency and goes down the failure path for status: `record_failure(outcome="partial")`, the counter increments and `last_success_at` doesn't move.
- `success` behaves as today.

The audit record's `after` gains `"rejected": [{"currency", "reason"}]`, but only when there is something to audit: no new observations means no audit record, as before.

*Alternative:* count `partial` as a success. Rejected: a currency silently missing for days would look healthy in readiness. The per-pair not-eligible signal (D4) would only catch it after a deadline.

### D3. Dense polling window derived from the active policy

`poll_interval_seconds(now, calendar, policy, settings)` gains a first branch. If `now` falls on a business day, inside `[publish_time(day), deadline(day) + dense_tail]`, the interval is `refresh_dense_interval_seconds`:
- `publish_time(day)` is the policy's weekday or Friday time;
- `deadline(day)` comes from the existing `publication_deadline`.

`next_interval` loads the active policy next to the calendar. If the policy is unavailable, the dense branch is skipped and today's intervals apply.

New settings:
- `refresh_dense_interval_seconds = 60`
- `refresh_dense_tail_minutes = 30`

The dense window always sits inside the 12:00–18:00 window, so it only *shortens* intervals and never adds polls at night. That's about 60 extra requests to BoI on a weekday (75 polls instead of 15): polite enough for a public API at one request a minute.

*Alternative:* fixed clock times in settings. Rejected: they would drift from the policy row, which is the versioned source of the publication times.

### D4. Not-eligible pairs: computed on read in the API, tracked as transitions in the refresh process

**Readiness.** `RateQueryService.readiness()` loads, in its existing connection:
- the enabled pairs;
- the active policy;
- the calendar view;
- the latest observation per pair (3 pairs, about 6 cheap queries).

It then runs the pure `evaluate()` for each pair. Each not-eligible pair becomes `{pair, reason, publicationDate}`:
- no observation: `RATE_NOT_FOUND`, with a null date;
- `PolicyNotEvaluable` (no policy, or the calendar doesn't cover the dates needed): every pair is listed as `POLICY_NOT_EVALUABLE`.

Storage errors still give 503 as today. The readiness *status* is unaffected.

**Logs.** `RefreshLoop` keeps an in-memory `dict[pair, bool | None]` (None means not yet assessed). After each attempt, in the same read-only connection as `next_interval`, it evaluates every enabled pair:
- On a change to not eligible (or not eligible at the first assessment), it logs one ERROR line: `rates.pair_not_eligible pair= reason= publication_date= required=`.
- On recovery, it logs one INFO line: `rates.pair_eligible`.

A restart re-announces an ongoing problem once, which is the right behavior for an alert.

*Alternatives:*
- **Persist the per-pair state.** Rejected: readiness already recomputes it, and the logs only need edges.
- **Compute only in the API.** Rejected: an alert should fire even when no one is calling the API.

### D5. Calendar coverage end is a pure function of the calendar view

`CalendarView.coverage_end(today) -> date | None`: walk the years upward from `today.year` while they are in `covered_years`, and return 31 December of the last one, or None when `today.year` itself isn't covered. The warning is `end is None or (end - today).days <= settings.calendar_warning_days` (default 60).

- Readiness adds `calendarCoverageEndsOn` and `calendarExpiryWarning`.
- The refresh loop stores `last_calendar_warning_on: date` and logs `rates.calendar_expiring ends_on= days_left=` at most once per Israel date.

### D6. Postgres restart policy

Add `restart: unless-stopped` to the `postgres` service.

The API and refresh services already reconnect on their own: the engine pool retries per request, and the loop catches storage errors. So a database restart means a short window of 503 / `storage_error`, with no manual recovery.

### D7. Conversion direction is documented, not computed

README §2 and the endpoint's OpenAPI description will say this:
- The API serves `1 base = rate quote`. For a deposit of `X` ILS, USD = `X / rate` (USD/ILS).
- Example: 10,000 ILS at 3.343 = 2,991.3251… USD. That's 2,991.33 rounded half-up to cents, or 2,991.32 rounded down. Which one applies is the consumer's money rule, which is exactly why this service doesn't round.
- Requesting `ILS/USD` returns 422 by design.

## Risks / Trade-offs

- [A currency stays permanently absent from BoI, for example if BoI retires GBP] → Every attempt is `partial`, so `lastSuccessfulIngestionAt` freezes and alerts fire on it. That's intended. The operator's remedy is to disable the pair, after which attempts become `success` again.
- [Readiness now runs eligibility (about 6 queries) on every probe] → The data is tiny, and probes come every 5 seconds per container. The cost is negligible next to the read path, which does the same work per request.
- [More BoI traffic in the dense window] → Bounded to the 15:15–16:30 (12:15–13:30 Friday) window on business days, at one request a minute.
- [The ERROR on a not-eligible transition fires on every refresh restart while a pair is overdue] → Accepted: a restart during an incident should re-raise it.
- [Partial ingestion stores USD while GBP lags behind] → Each pair's eligibility is judged on its own publication date, so a stale GBP is reported as GBP's problem only, which is the whole point.

## Migration Plan

1. There is no database migration. Deploy the new image and update the Compose file.
2. **Rollback:** redeploy the previous image. The rows written by both versions are compatible. `refresh_status.last_attempt_outcome` may hold `partial`, which the previous code only ever reads back as text.
