# Exchange Rates API: Bank of Israel representative rates

## 1. Problem

A financially critical process needs the latest Bank of Israel (BoI) **representative** exchange rates for USD, EUR and GBP against ILS. Getting it wrong is costly in both directions:

- a silently outdated rate leads to wrong financial decisions;
- an API that fails whenever BoI is slow or closed blocks the process for no reason.

This service fetches BoI rates in the background, stores every verified observation, and serves the latest one together with **when BoI published it** and an explicit **eligibility** assessment: is this rate acceptable for use *right now* under a configured, versioned policy?

> **What a representative rate is not.** BoI publishes one representative rate per business day, based on interbank trading during a short sampling window. It "has no obligatory status under law" and is **not an executable trade price**. Consumers that need a tradeable quote need a quoting service; this API is a reference-rate service.

<!-- Sections below are completed as implementation progresses. -->

## 2. Quick start

Requires Docker. Starts Postgres, runs migrations and seeds, then the API and the refresh process:

```bash
docker compose up --build
```

Rate reads need a bearer token. Compose ships a **demo-only** user (`demo` / `demo-password`):

```bash
TOKEN=$(curl -s -d username=demo -d password=demo-password   http://localhost:8000/api/v1/auth/token | python -c "import json,sys; print(json.load(sys.stdin)['access_token'])")

curl -H "Authorization: Bearer $TOKEN" "http://localhost:8000/api/v1/exchange-rates?base=USD&quote=ILS"
curl "http://localhost:8000/health/ready"      # no token needed
open http://localhost:8000/docs                # Swagger UI: "Authorize" with demo / demo-password
```

The token lasts an hour (`expires_in: 3600`). Without one, reads return **401** `AUTHENTICATION_REQUIRED`; with an expired or tampered one, 401 `INVALID_TOKEN`. Every authenticated read is written to the audit trail (§7).

Supported pairs: `USD/ILS`, `EUR/ILS`, `GBP/ILS` (`1 base = rate quote`). Codes are case-insensitive.

**Converting a shekel deposit to dollars.** Rates are served in one direction only: `1 base = rate quote`, e.g. `1 USD = 3.343 ILS`. To convert `X` ILS into USD, request `base=USD&quote=ILS` and divide:

```
10,000 ILS / 3.343 = 2,991.3251... USD   ->  2,991.33 (half-up to cents)  or  2,991.32 (rounded down)
```

Which rounding applies is the consuming system's money rule, which is why this service returns the exact decimal and never rounds. `base=ILS&quote=USD` returns 422 `UNSUPPORTED_CURRENCY_PAIR` by design.

**Eligible rate** (Tue 24/11 10:00 Israel time; Monday's rate is the latest expected):

```json
{
  "rateId": "a5ac2529-fbea-4026-b85a-986d3648d3c5",
  "baseCurrency": "USD",
  "quoteCurrency": "ILS",
  "rate": "3.343",
  "publicationDate": "2026-11-23",
  "eligibility": {
    "status": "ELIGIBLE",
    "reason": null,
    "requiredPublicationDate": "2026-11-23",
    "evaluatedAt": "2026-11-24T08:00:00Z",
    "policyVersion": 1
  },
  "source": {
    "name": "BANK_OF_ISRAEL",
    "type": "REPRESENTATIVE",
    "lastUpdate": "2026-11-23T13:21:10.710489Z",
    "rate": "3.343",
    "unit": 1
  },
  "retrievedAt": "2026-11-23T13:25:00Z"
}
```

**Not eligible**: same request at 16:01, after Tuesday's deadline, with no new BoI rate. Still **200**, because the data is valid; it is just not acceptable for use now:

```json
{
  "rate": "3.343",
  "publicationDate": "2026-11-23",
  "eligibility": {
    "status": "NOT_ELIGIBLE",
    "reason": "PUBLICATION_OVERDUE",
    "requiredPublicationDate": "2026-11-24",
    "evaluatedAt": "2026-11-24T14:01:00Z",
    "policyVersion": 1
  },
  "...": "other fields as above"
}
```

> **Consumer obligation:** check `eligibility.status == "ELIGIBLE"` before using `rate` in a financial operation. A 200 alone does not mean "usable". `eligibility` is always present and is marked required in the OpenAPI schema.

**Errors** are RFC 9457 problem details (`application/problem+json`) and are reserved for real faults:

```json
{
  "type": "/problems/unsupported-currency-pair",
  "title": "Unprocessable Entity",
  "status": 422,
  "detail": "Currency pair ILS/USD is not supported or not enabled",
  "code": "UNSUPPORTED_CURRENCY_PAIR"
}
```

| Situation | Status | `code` |
|---|---|---|
| Rate exists, eligible | 200 | n/a (`eligibility.status = ELIGIBLE`) |
| Rate exists, not eligible | 200 | n/a (`NOT_ELIGIBLE` + `reason`) |
| Missing / malformed `base` or `quote` | 400 | `INVALID_REQUEST` |
| Pair not configured or disabled | 422 | `UNSUPPORTED_CURRENCY_PAIR` |
| Pair enabled but nothing ingested yet | 404 | `RATE_NOT_FOUND` |
| Policy cannot be evaluated (calendar not loaded, no active policy) | 503 | `POLICY_NOT_EVALUABLE` (no rate in body) |
| Database unreachable | 503 | `STORAGE_UNAVAILABLE` |

`NOT_ELIGIBLE` reasons: `PUBLICATION_OVERDUE`, `MAX_AGE_EXCEEDED`, `INVALID_PUBLICATION_DATE` (future-dated).

## 3. Architecture

```
            +------------- API process (N workers; writes audit rows only, never rates) ---+
 HTTP --->  |  JWT check (401) --> routes --> RateQueryService --> repositories --> DB     |
            |                         |               |                                    |
            |                         |               +--> evaluate(...)  [pure]           |
            |                         +--> QueryAuditService --> audit_events (RATE_QUERIED) |
            +------------------------------------------------------------------------------+

            +---------------------- refresh process (exactly 1, only writer) --------------+
            |  RefreshLoop --> IngestionService --> BoiClient --> boi.org.il/PublicApi     |
            |                        |  validate each currency, normalise units            |
            |                        +--> ONE transaction: insert observations + audit     |
            +------------------------------------------------------------------------------+

            +---------------------- database (Postgres in compose / SQLite locally) -------+
            | currency_pairs | rate_observations (append-only) | policies (versioned)        |
            | non_publication_days | calendar_coverage | audit_events (append-only)          |
            +------------------------------------------------------------------------------+
                      ^
                      +-- scripts/declare_non_publication_day.py (day + audit, one transaction)
```

**Read flow** (`GET /api/v1/exchange-rates`): verify the bearer token (401, not audited), validate codes (400), then check the pair is enabled (422), then load the active policy and calendar (503 if missing), then load the latest observation (404 if none), then `evaluate(...)`, write the `RATE_QUERIED` audit record and only then return 200 (503 `AUDIT_UNAVAILABLE` if the record can't be stored). Rejected authenticated reads are audited too. **BoI is never called on the read path**, so BoI outages don't affect reads; they only stop new data arriving.

**Refresh flow**: every **1 min** on business days from the expected publication time until 30 min after the deadline (15:15–16:30 Mon–Thu, 12:15–13:30 Fri, taken from the active policy), every 5 min otherwise between 12:00 and 18:00, and hourly outside that window, at weekends and on holidays. The dense window costs about 60 extra BoI requests per business day (75 polls instead of 15), and it means a late BoI publication reaches us within a minute instead of up to five. Each run fetches with a timeout and bounded retries (exponential backoff and jitter). It parses numbers directly into `Decimal` and validates **each enabled currency on its own**: present, rate > 0, unit is a positive integer, a parseable `lastUpdate`, and no publication date in the future. A missing or invalid currency is rejected with its reason and logged at error level, while the valid currencies are still stored, so a GBP problem never stops USD. Such an attempt is recorded as `partial`, which doesn't count as a success. Only a payload without an `exchangeRates` array is rejected as a whole. Each currency keeps its own `lastUpdate`, so a BoI correction to one currency stores just that currency. The accepted currencies are inserted in **one transaction** with `ON CONFLICT DO NOTHING` on `(pair, source lastUpdate)`, and one audit event is written only if anything new was stored; it lists any rejected currencies. A failed fetch changes nothing.

**Code layout**

```
src/rates/
  domain/        models, calendar + deadlines, eligibility (pure), decimals, errors
  services/      rate_query (read path), ingestion, operator, ports (repository interfaces)
  adapters/boi/  client (httpx + tenacity, parse_float=Decimal), parser (validation, normalisation)
  adapters/db/   tables, types (all SQLite/Postgres differences), repos, session
  api/           app factory, routes, schemas, problem details
  refresh/       loop + entry point
migrations/      Alembic: 0001 schema, 0002 seed (pairs, policy v1, BoI 2026-27 calendar)
scripts/         declare_non_publication_day.py
tests/           unit/ (domain, parser, client), integration/ (DB, ingestion, loop), contract/ (HTTP)
```

## 4. Freshness policy

BoI publishes once per foreign-currency **business day**, "soon after 15:15" Israel time (12:15 on Fridays), with no rates on weekends, Israeli holidays and listed "Holiday Abroad" days. BoI publishes its non-publication list per year. The 2026 and 2027 lists are seeded into the database.

**The rule.** At any instant *now* (evaluated in `Asia/Jerusalem`):

```
deadline(d)   = expected publish time on d (15:15 Mon-Thu, 12:15 Fri) + grace
required(now) = latest business day d with deadline(d) <= now
ELIGIBLE      = publicationDate >= required(now)
                AND publicationDate is not in the future
                AND publicationDate is at most max_age_days old
```

Checks run in this order: future-dated (`INVALID_PUBLICATION_DATE`) → older than the cap (`MAX_AGE_EXCEEDED`) → no `required(now)` within the cap (503, see below) → older than `required(now)` (`PUBLICATION_OVERDUE`).

Policy v1: grace **45 min**, max age **7 days**. These are assumptions, not BoI rules (see §6).

**Worked examples** (each one is a row in [`tests/unit/test_eligibility.py`](tests/unit/test_eligibility.py)):

| Now (Israel time) | Latest rate dated | Required | Result |
|---|---|---|---|
| Tue 24/11 10:00 | Mon 23/11 | Mon 23/11 | ELIGIBLE (today's deadline 16:00 not reached) |
| Tue 24/11 16:01 | Mon 23/11 | Tue 24/11 | NOT_ELIGIBLE, `PUBLICATION_OVERDUE` |
| Sat 28/11 12:00 | Fri 27/11 | Fri 27/11 | ELIGIBLE (weekend needs no special rule) |
| Fri 27/11 13:01 | Thu 26/11 | Fri 27/11 | NOT_ELIGIBLE (Friday deadline is 13:00) |
| Tue 27/10 18:00 | Mon 26/10 | Mon 26/10 | ELIGIBLE (27/10 is a listed holiday: Knesset elections) |
| Wed 02/12 16:30, BoI unexpectedly silent | Tue 01/12 | Wed 02/12 | NOT_ELIGIBLE until an operator declares 02/12 (below) |
| Tue 07/04 10:00 (Pesach) | Tue 31/03 | Tue 31/03 | ELIGIBLE (7 days old, within the cap) |
| Any time in 2028 with no 2028 calendar loaded | n/a | n/a | **503 `POLICY_NOT_EVALUABLE`**: fails closed |
| Thu 26/11 18:00, calendar wrongly lists 17/11–26/11 | Mon 16/11 | none | NOT_ELIGIBLE, `MAX_AGE_EXCEEDED` (cap checked first) |
| Thu 26/11 18:00, same wrong calendar | Wed 25/11 | none | **503 `POLICY_NOT_EVALUABLE`**: calendar is implausible |

**Fail closed.** The API refuses to assess eligibility (503) rather than guess when:
- the calendar for a year the evaluation needs is not loaded;
- no active policy exists;
- the calendar leaves no business day within the max age, which means the calendar data is wrong.

### Operator action: declaring an unexpected non-publication day

BoI may skip a business day that is not in its published list ("lack of data"). The service cannot distinguish that from a BoI outage or a bug in our ingestion. So it **fails closed**: after the deadline, reads report `NOT_ELIGIBLE` / `PUBLICATION_OVERDUE` until a person confirms the day was genuinely skipped:

```bash
# local (uses DATABASE_URL, default ./rates.db)
python scripts/declare_non_publication_day.py --date 2026-12-02 --reason "BoI did not publish: no data" --actor alice

# docker compose
docker compose run --rm api python scripts/declare_non_publication_day.py --date 2026-12-02 --reason "BoI did not publish: no data" --actor alice
```

The day row (`origin = OPERATOR`) and an `audit_events` row (actor, reason, time) are written in **one transaction**. The next read re-evaluates against the updated calendar, so the previous rate becomes `ELIGIBLE` again with no restart. The script exits `0` declared, `2` invalid input (e.g. empty reason), `3` already declared, `1` storage error.

This script is the only runtime policy change in this release. Changing grace or max age means adding a new `policies` row (new version) through a migration.

## 5. Decisions and trade-offs

The full design record, including the alternatives considered, is in [`openspec/changes/add-exchange-rates-api/design.md`](openspec/changes/add-exchange-rates-api/design.md). The behaviour contracts, with testable scenarios, are in its `specs/` folder.

| # | Decision | Why | Trade-off / cost |
|---|---|---|---|
| 1 | **Correctness before availability.** Fail closed when eligibility can't be determined | A silently wrong rate in a financial process is worse than an explicit refusal | More 503s in edge cases (e.g. calendar not loaded) |
| 2 | **Out-of-date rate = `200` + `NOT_ELIGIBLE`**, not `503` | A stale rate is a valid domain state, not a server fault. 503 would trigger retries/circuit breakers for nothing, and hides useful data (what we have, and its date) | Consumers must check `eligibility.status`; a lazy client that only checks HTTP status could misuse the rate. Mitigated by a required field, docs and naming. *This reverses an earlier "503 when stale" idea.* |
| 3 | **Eligibility = "at least as recent as the latest *expected* BoI publication"**, driven by BoI's calendar, instead of a max age in hours | Handles weekends, holidays and intraday timing with one rule. An hours-based TTL is wrong on Mondays and after holidays | Needs the official holiday list loaded each year (fails closed if missing) |
| 4 | **Unexpected missed publication: fail closed until an operator declares it** (audited script) | Only a human can tell "BoI skipped a day" from "our ingestion is broken" | Requires a person to act; until then reads are `NOT_ELIGIBLE` |
| 5 | **Pure `evaluate()` function** with an injected clock | All financial logic is unit-testable as a table; no I/O, no hidden `now()` | n/a |
| 6 | **Two processes from one codebase**: API (N workers, writes audit rows only) + refresh (exactly 1 writer of rate data) | One writer by construction, no distributed locking; BoI latency never sits on the read path | Two things to run (one `docker compose up`) |
| 7 | **Append-only observations, idempotent on `(pair, BoI lastUpdate)`** | Same-day BoI corrections are kept as history; duplicate or concurrent refreshes are harmless | Storage grows slowly (3 rows per publication) |
| 8 | **Per-currency validation** (replaced whole-snapshot validation) | Each pair's eligibility is judged on its own publication date, so a bad GBP entry has no reason to block USD deposits | A currency BoI stops sending leaves every attempt `partial`: `lastSuccessfulIngestionAt` freezes until the pair is disabled. That's intentional, so the problem gets alerted on |
| 9 | **Exact decimals end to end**: `parse_float=Decimal`, `TEXT`/`NUMERIC` storage, JSON strings | Binary floats can't represent rates exactly | Consumers parse a string, not a JSON number |
| 10 | **SQLAlchemy Core + Alembic; Postgres in compose, SQLite locally/tests** | One-command realistic setup, plus zero-setup dev; all dialect quirks in `adapters/db/types.py`; the same test suite runs on both | Hand-written migrations must work on both dialects |
| 11 | **Policy and calendar read on every request**, no cache | A few dozen rows; operator changes apply immediately; nothing to invalidate across workers | A few extra cheap queries per request |
| 12 | **GET with query params**, single pair | Read-only, cacheable semantics | Batch lookups would need a new endpoint |
| 13 | **Python 3.12 + FastAPI + Pydantic v2** | Async, OpenAPI from types, `Decimal` serialises as string | Weaker static guarantees than e.g. .NET; covered by tests |
| 14 | **Demo JWT auth** (one configured user, HS256, expiry checked against the injected clock) | Every rate needs an owner, and a demo needs no identity provider; one clock keeps expiry testable | Not production identity (see §6, §8) |
| 15 | **Fail closed on audit**: a rate is returned only after its `RATE_QUERIED` record commits | A rate handed out with no trace of who received it, and what they were told, breaks the audit trail | One insert per read; an audit outage blocks reads (in practice only when the DB is down anyway) |

## 6. Assumptions and limitations

- **Grace 45 minutes and max age 7 days are assumptions, not BoI rules.** BoI says rates are published "soon after 15:15" but "the time is not fixed". Observed publications are a few minutes after 15:15. Both values live in the versioned `policies` table (v1).
- **The BoI calendar must be loaded every year.** Seeded for 2026–2027 from BoI's official PDF. From 2028-01-01 the API returns `503 POLICY_NOT_EVALUABLE` (by design) until a migration adds the 2028 list and a `calendar_coverage` row. That cliff is announced in advance: `/health/ready` reports `calendarCoverageEndsOn` (2027-12-31 today) and sets `calendarExpiryWarning: true` within `CALENDAR_WARNING_DAYS` (default **60**) of it, and the refresh process logs `rates.calendar_expiring ends_on=... days_left=...` once a day while the warning is on. When it fires, add a migration with the next year's BoI non-publication list and its `calendar_coverage` row. Walking back from early January also needs the previous year. A calendar that leaves no business day within the 7-day cap is also treated as bad data: reads of a recent rate return 503 `POLICY_NOT_EVALUABLE` (logged at error level) until the calendar is corrected.
- **Holiday eves with an early (12:15) publication are not modelled.** The calendar only lists days with *no* publication. On a Mon–Thu holiday eve the previous rate stays eligible until 16:00 instead of 13:00. The refresh process still ingests the new rate as soon as it appears.
- **Ingestion lag.** Between BoI publishing and our next poll (≤ 1 min in the dense window around the deadline, ≤ 5 min elsewhere in the publication window) the API serves the previous rate, which is still eligible until the deadline.
- **BoI may change its payload shape.** Validation rejects anything unexpected (per currency, or the whole payload if `exchangeRates` itself is missing), the last good data is kept, and the failure is logged. Reads turn `NOT_ELIGIBLE` after the next deadline, which is the visible signal.
- **SQLite allows one writer.** That matches the design (only the refresh process writes) and WAL keeps readers concurrent. Postgres is the compose default.
- **`publicationDate` is derived** from BoI's `lastUpdate` timestamp converted to Israel time. The API has no explicit date field.
- **Representative rates are not trade prices** (see §1).
- **No stated load or SLO requirements** were given. Reads are a few indexed queries over a tiny dataset; the API scales horizontally (`API_WORKERS`, more replicas). The refresh process must stay single-instance (correctness doesn't depend on it, efficiency does).
- **Security (demo only).** One configured user exchanges a username and password for an HS256 JWT (1 h, shared `JWT_SECRET`; the API refuses to start without a secret of at least 32 characters). There is no user store, no password hashing, no refresh token, no revocation (rotating `JWT_SECRET` invalidates every token) and no rate limiting. The Compose secret and password are demo values and must come from a secret store anywhere else. The operator action is a script with DB access, not an HTTP endpoint. Compose database credentials are for local development only.
- **Health:** `/health/live` checks the process. `/health/ready` checks the database (200, or 503 `STORAGE_UNAVAILABLE`) and reports what an operator needs to keep deposits flowing:
  - `latestRetrievedAt`: when the newest stored rate was fetched.
  - `lastSuccessfulIngestionAt`: when the refresh process last completed a successful poll, including polls that found nothing new. Null until the first success. A `partial` poll (some currency rejected) does not count.
  - `notEligiblePairs`: every enabled pair a consumer can't use right now, with its reason (`PUBLICATION_OVERDUE`, `MAX_AGE_EXCEEDED`, `INVALID_PUBLICATION_DATE`, `RATE_NOT_FOUND` or `POLICY_NOT_EVALUABLE`) and the publication date of its current rate.

  None of these change the readiness status: an old rate or a stalled refresh must not take the API out of rotation. They are the signals to alert on. The refresh process records each attempt in a one-row `refresh_status` table, so the API can report this without talking to the refresh process.

  Example, Tue 24/11 16:30, with BoI's Tuesday USD rate not yet received:

  ```json
  {
    "status": "ready",
    "database": "ok",
    "latestRetrievedAt": "2026-11-24T13:25:00Z",
    "lastSuccessfulIngestionAt": "2026-11-24T14:29:41Z",
    "notEligiblePairs": [
      {"pair": "USD/ILS", "reason": "PUBLICATION_OVERDUE", "publicationDate": "2026-11-23"}
    ],
    "calendarCoverageEndsOn": "2027-12-31",
    "calendarExpiryWarning": false
  }
  ```

  **What to alert on** (any of these means deposits are, or soon will be, blocked):

  | Signal | Where | Meaning |
  |---|---|---|
  | `notEligiblePairs` not empty | `/health/ready` | A pair is unusable right now (e.g. BoI skipped a day; see §4 operator action) |
  | `rates.pair_not_eligible pair=... reason=...` (ERROR) | refresh logs | Logged once when a pair becomes unusable; `rates.pair_eligible` (INFO) when it recovers. Fires even when no one is calling the API |
  | `refresh.currency_rejected currency=... reason=...` (ERROR) | refresh logs | BoI sent a missing or invalid entry; the other currencies were still stored |
  | `lastSuccessfulIngestionAt` older than ~15 min in the dense window | `/health/ready` | Refresh is failing or every attempt is `partial` |
  | `calendarExpiryWarning: true` / `rates.calendar_expiring` (WARNING, daily) | `/health/ready`, refresh logs | The BoI calendar runs out soon: every read will return 503 after `calendarCoverageEndsOn` |

## 7. Running without Docker, tests, failure simulation

**Local run (SQLite, no Docker)**, Python 3.12+:

```bash
python -m venv .venv
.venv/Scripts/activate            # Windows; on Linux/macOS: source .venv/bin/activate
pip install -e ".[dev]"
alembic upgrade head              # creates ./rates.db with schema + seeds
python -m rates.refresh           # terminal 1: the single writer
python -m rates.api               # terminal 2: http://127.0.0.1:8000
```

Configuration is via environment variables or a `.env` file. See [`.env.example`](.env.example).

**Tests**

```bash
pytest                                                                   # SQLite (default)
docker compose up -d postgres
docker compose exec postgres createdb -U rates rates_test                       # once per volume
TEST_DATABASE_URL=postgresql+asyncpg://rates:rates@localhost:5433/rates_test pytest   # same suite on Postgres
```

On PowerShell: `$env:TEST_DATABASE_URL="postgresql+asyncpg://rates:rates@localhost:5433/rates_test"; pytest`. The suite drops and recreates the `public` schema before every test, so use a dedicated database: pointing it at `rates` would wipe the running stack's data.

- `tests/unit`: eligibility table (every spec scenario), calendar and DST, BoI parser and client (retries, malformed bodies), response serialisation.
- `tests/integration`: decimal and timestamp round trips, migrations and seeds, repositories, ingestion atomicity and idempotency, refresh loop resilience, operator script.
- `tests/contract`: the HTTP contract: status codes, problem details, decimal strings, "reads never call BoI", OpenAPI required fields.

**Simulating BoI failure**: point the refresh process at an unreachable host:

```bash
BOI_URL=http://127.0.0.1:9/unreachable python -m rates.refresh
# logs: refresh.attempt outcome=boi_unavailable consecutive_failures=1 ...
```

With compose: `docker compose stop refresh`, then `docker compose run --rm -e BOI_URL=http://127.0.0.1:9/x refresh`. Existing data is untouched. Reads keep returning the stored rate, which becomes `NOT_ELIGIBLE` once the next deadline passes. `/health/ready` keeps reporting the earlier `lastSuccessfulIngestionAt`, so an alert on its age catches the outage before any deadline does.

**Operator script**: see §4.

**Audit trail of rate queries.** Each authenticated read leaves one `RATE_QUERIED` row in `audit_events`: the user (`actor`), `base`/`quote` exactly as sent, the HTTP status and error code, and, for a returned rate, its `rateId`, rate, `publicationDate` and the full eligibility verdict the caller received. 401s are not audited (there is no user); they're logged as `auth.rejected`. With compose:

```bash
docker compose exec postgres psql -U rates -d rates -c "
  SELECT occurred_at, actor,
         after->'request'->>'base' AS base, after->'request'->>'quote' AS quote,
         after->'response'->>'status' AS status, after->'response'->>'code' AS code,
         after->'response'->>'rateId' AS rate_id,
         after->'response'->'eligibility'->>'status' AS eligibility
  FROM audit_events WHERE event_type = 'RATE_QUERIED' ORDER BY occurred_at DESC LIMIT 10"
```

**End-to-end smoke run** (`docker compose up --build`, Tue 2026-11-24 13:45 Israel time, live BoI):

```text
refresh-1 | refresh.attempt outcome=success inserted=3 latest_publication_date=2026-11-23 latest_source_last_update=2026-11-23T13:21:10.710489+00:00 duration_ms=194

$ curl "localhost:8000/api/v1/exchange-rates?base=USD&quote=ILS"
{"rateId":"c2726261-6e4f-4692-8c33-b039a7f8b13b","baseCurrency":"USD","quoteCurrency":"ILS","rate":"3.343","publicationDate":"2026-11-23","eligibility":{"status":"ELIGIBLE","reason":null,"requiredPublicationDate":"2026-11-23","evaluatedAt":"2026-11-24T11:46:06.203451Z","policyVersion":1},"source":{"name":"BANK_OF_ISRAEL","type":"REPRESENTATIVE","lastUpdate":"2026-11-23T13:21:10.710489Z","rate":"3.343","unit":1},"retrievedAt":"2026-11-24T11:45:58.922261Z"}

$ curl "localhost:8000/api/v1/exchange-rates?base=ILS&quote=USD"     # 422 UNSUPPORTED_CURRENCY_PAIR
$ curl "localhost:8000/api/v1/exchange-rates?base=US1&quote=ILS"     # 400 INVALID_REQUEST
```

With the refresh process pointed at an unreachable BoI URL, the log shows `outcome=boi_unavailable` and the read above returns the same `rateId`, still `ELIGIBLE`.

**Readiness across a quiet poll and an outage** (same day, 14:53 Israel time, after a redeploy that applied migration `0003`). BoI had nothing new, so the poll stored nothing but still counts as a success:

```text
refresh-1 | refresh.attempt outcome=success inserted=0 latest_publication_date=2026-11-23 latest_source_last_update=2026-11-23T13:21:10.710489+00:00 duration_ms=189 last_success_at=2026-11-24T12:53:12.084627+00:00

$ curl localhost:8000/health/ready
{"status":"ready","database":"ok","latestRetrievedAt":"2026-11-24T11:45:58.922261Z","lastSuccessfulIngestionAt":"2026-11-24T12:53:12.084627Z"}

# refresh restarted with BOI_URL=http://127.0.0.1:9/x
refresh   | refresh.started url=http://127.0.0.1:9/x last_success_at=2026-11-24T12:53:12.084627+00:00
refresh   | refresh.attempt outcome=boi_unavailable consecutive_failures=1 last_success_at=2026-11-24T12:53:12.084627+00:00 ...

$ curl localhost:8000/health/ready     # unchanged: a failure never moves the last success
{"status":"ready","database":"ok","latestRetrievedAt":"2026-11-24T11:45:58.922261Z","lastSuccessfulIngestionAt":"2026-11-24T12:53:12.084627Z"}
```

**Deposit-availability hardening** (redeploy at Tue 24/11 15:56 Israel time, inside the dense window 15:15–16:30; BoI had published today's rates at 15:21):

```text
refresh-1 | 13:56:01 refresh.attempt outcome=success inserted=0 latest_publication_date=2026-11-24 ...
refresh-1 | 13:57:01 refresh.attempt outcome=success inserted=0 latest_publication_date=2026-11-24 ...
refresh-1 | 13:58:01 refresh.attempt outcome=success inserted=0 latest_publication_date=2026-11-24 ...   # 60 s apart (UTC times)

$ curl localhost:8000/health/ready
{"status":"ready","database":"ok","latestRetrievedAt":"2026-11-24T13:23:34.169839Z","lastSuccessfulIngestionAt":"2026-11-24T13:56:01.881867Z","notEligiblePairs":[],"calendarCoverageEndsOn":"2027-12-31","calendarExpiryWarning":false}

$ curl "localhost:8000/api/v1/exchange-rates?base=USD&quote=ILS"   # rate 3.345, publicationDate 2026-11-24, ELIGIBLE

# simulated Postgres crash (pg_ctl stop -m immediate inside the container):
# Docker restarts it (RestartCount 1) and the read above returns 200 again with no manual action
```

**Authentication and audit** (Tue 24/11 16:36 Israel time, demo user):

```text
$ TOKEN=$(curl -s -d username=demo -d password=demo-password localhost:8000/api/v1/auth/token | python -c "...['access_token']")

$ curl -H "Authorization: Bearer $TOKEN" "localhost:8000/api/v1/exchange-rates?base=USD&quote=ILS"   # 200
{"rateId":"fa99b236-de6e-42d6-b723-e0af9677497c","baseCurrency":"USD","quoteCurrency":"ILS","rate":"3.345","publicationDate":"2026-11-24","eligibility":{"status":"ELIGIBLE",...}, ...}

$ curl "localhost:8000/api/v1/exchange-rates?base=USD&quote=ILS"                                     # 401, no token
{"type":"/problems/authentication-required","title":"Unauthorized","status":401,"detail":"A bearer token is required","code":"AUTHENTICATION_REQUIRED"}

$ curl -H "Authorization: Bearer $TOKEN" "localhost:8000/api/v1/exchange-rates?base=ILS&quote=USD"   # 422
{"type":"/problems/unsupported-currency-pair",...,"status":422,...,"code":"UNSUPPORTED_CURRENCY_PAIR"}

$ docker compose exec postgres psql ... RATE_QUERIED rows since the first call:
 actor | base | quote | status |           code            |               rate_id                | eligibility
-------+------+-------+--------+---------------------------+--------------------------------------+-------------
 demo  | USD  | ILS   | 200    |                           | fa99b236-de6e-42d6-b723-e0af9677497c | ELIGIBLE
 demo  | ILS  | USD   | 422    | UNSUPPORTED_CURRENCY_PAIR |                                      |

api-1 | auth.rejected code=AUTHENTICATION_REQUIRED path=/api/v1/exchange-rates ...   # the 401: logged, not audited
```

## 8. Out of scope and possible extensions

Out of scope by design: trade execution, rate locking and quote confirmation (the consumer's quoting service owns those; `rateId` lets it reference the exact observation), inverse and cross pairs, an admin HTTP API or UI, caching, and multi-region deployment.

Natural extensions:
- Inverse and cross pairs (`calculation_type` on `currency_pairs`), computed from direct observations.
- An authenticated admin API for policy versions and declarations (reusing the same audited service).
- Automated import or alerting for the next year's BoI calendar.
- Metrics (Prometheus): last successful ingestion, observation age, eligible vs. not-eligible reads.
- `GET /api/v1/exchange-rates/history` over the append-only observations.
- Production identity: an identity provider (OIDC) or a user store with hashed passwords, roles, short tokens with refresh, revocation, and audited logins.
- Audit retention and export: `RATE_QUERIED` rows grow by about one per read, with no retention policy yet.

## 9. AI assistance

AI assistance (Claude) was used for:
- design exploration: the freshness policy, the API semantics and their trade-offs, captured as an OpenSpec change (proposal, specs, design, tasks) under `openspec/`;
- checking BoI's actual API payload, publication schedule and holiday calendar;
- drafting the implementation and tests task by task against those specs.

Every decision above was discussed and chosen explicitly. The code is covered by tests derived from the spec scenarios.
