# Design

## Context

This is a greenfield project with no existing code. The upstream source is the BoI public API `GET https://boi.org.il/PublicApi/GetExchangeRates`. It returns `exchangeRates[]` entries with the fields `key`, `currentExchangeRate`, `currentChange`, `unit` and `lastUpdate`. `lastUpdate` is a UTC timestamp, and it is the same on every entry in a snapshot. There is no explicit publication-date field.

BoI's publication rules, from its explanatory notes:
- Rates are published once per foreign-currency business day, "soon after 15:15", or 12:15 on Fridays and holiday eves. The exact time is not fixed.
- There are no rates on weekends, Israeli holidays, or listed "Holiday Abroad" days.
- Rates may also be skipped "due to lack of data".

BoI publishes the non-publication dates for each year as a PDF. The 2026–2027 list is the seed data.

Requirements live in `specs/`. Motivation is in `proposal.md`.

## Goals / Non-Goals

**Goals:**
- The eligibility logic is a pure function that can be tested exhaustively against the scenarios in `specs/rate-freshness-policy`.
- No binary floating-point anywhere a rate travels.
- One-command start with `docker compose up` on Postgres. The same code also runs on SQLite with no infrastructure, for quick local runs and fast tests. The database is selected only by `DATABASE_URL`.
- Exactly one writer of rate data, guaranteed by how processes are arranged rather than by distributed locking.

**Non-Goals:**
- No in-memory cache in front of the database; the dataset is a handful of rows.
- No admin HTTP surface. The only runtime mutation is the operator script.
- No automated import of the BoI holiday PDF. Each new year is added by a migration, written by hand.

## Decisions

### D1. Python 3.12 + FastAPI + Pydantic v2
FastAPI provides async request handling, generates OpenAPI from Pydantic models, and has good testing support. Pydantic v2 serialises `Decimal` as a JSON string by default in JSON mode, which is what the `exchange-rate-query` contract requires.
*Alternatives:* .NET or Spring would give stronger static typing and native decimals, but the user prefers Python, and Python's decimal story is adequate once the leaks below are sealed. Node was rejected because of weak decimal support.

### D2. Sealing the three decimal leaks
| Boundary | Default behaviour | Decision |
|---|---|---|
| BoI JSON to Python | `json` parses numbers as `float` | Parse the body with `json.loads(..., parse_float=Decimal)` (via `httpx` `response.json(parse_float=Decimal)`) |
| Python to SQLite | SQLite has no decimal type; SQLAlchemy `Numeric` goes through REAL | `DecimalText` `TypeDecorator`: stores `TEXT` on SQLite and `NUMERIC(18,10)` on Postgres, always returns `Decimal` |
| Python to API JSON | Could become a number | Response schema fields typed `Decimal`, rendered as strings; covered by a contract test |

Normalisation (`source_rate / unit`) uses a local `decimal.Context(prec=28)`; dividing by 1, 10 or 100 is exact. An end-to-end test ingests the fixture rate `3.343` and asserts that the API returns the string `"3.343"`.

### D3. Persistence: SQLAlchemy 2.0 (async) + Alembic, with dialect differences isolated
Drivers are `asyncpg` (docker compose / Postgres) and `aiosqlite` (local without Docker, and the default for tests), selected by `DATABASE_URL`. Alembic migrations must run on both dialects. Every dialect difference lives in `adapters/db/types.py` and one insert helper:
- `DecimalText`, as in D2.
- `UtcDateTime`. SQLite does not store time zones, so this type stores UTC ISO-8601 text, returns aware UTC datetimes, and raises an error on naive input.
- JSON is stored with `JSON().with_variant(JSONB(), "postgresql")`.
- Idempotent insert uses `sqlite.insert` or `postgresql.insert` with `.on_conflict_do_nothing(...)`, chosen by dialect.
- SQLite connections enable `PRAGMA journal_mode=WAL`, `foreign_keys=ON` and `busy_timeout`.

Services depend on repository interfaces (`RateRepository`, `CalendarRepository`, `PolicyRepository`, `PairRepository`, `AuditRepository`), never on SQLAlchemy.
*Alternatives:* Raw SQL per dialect would mean more code and drift between dialects. A plain ORM without repositories would leak SQLAlchemy into services and make the pure-domain tests harder.

### D4. Data model
```
currency_pairs        (base_currency, quote_currency) PK, enabled, source_key
rate_observations     id uuid PK, base/quote FK, rate, source_rate, source_unit,
                      publication_date, source_last_update, retrieved_at
                      UNIQUE(base_currency, quote_currency, source_last_update)
                      INDEX(base_currency, quote_currency, publication_date DESC,
                            source_last_update DESC)
non_publication_days  date PK, reason, origin (BOI_CALENDAR|OPERATOR), created_by, created_at
calendar_coverage     year PK, source_ref
policies              version PK, grace_minutes, max_age_days, weekday_publish_time,
                      friday_publish_time, timezone, active, created_at
                      (at most one active; enforced in repository + partial unique index on PG)
audit_events          id PK, occurred_at, actor, event_type, entity_type, entity_id,
                      before JSON, after JSON, reason
```
`rateId` in the API is the `rate_observations.id` UUID. The idempotency key is `source_last_update`, not `publication_date`, so a same-day BoI correction is stored as a new row. Seeds are loaded by Alembic data migrations: the 3 pairs, policy v1 (grace 45 min, max age 7 days, 15:15/12:15, Asia/Jerusalem), and the 2026–2027 BoI non-publication dates with matching `calendar_coverage` rows.

### D5. Eligibility evaluator: a pure domain function
```
evaluate(observation, now_utc, calendar: CalendarView, policy: Policy) -> Eligibility
```
`CalendarView` is an in-memory snapshot holding the set of non-publication dates and the covered years. The evaluator works as follows:
1. Convert `now` to Asia/Jerusalem.
2. Walk back day by day from today, at most `max_age_days + 1` steps, to find the latest business day whose deadline is at or before `now`. If it meets a year not in coverage, it raises `PolicyNotEvaluable`.
3. Apply the checks in this order: future date, max age, required date.

The evaluator takes no clock argument and does no I/O, so every scenario in `rate-freshness-policy` becomes a row in one parametrised test table. If the walk reaches its bound without finding a business day, the observation is not eligible (`MAX_AGE_EXCEEDED`), which is the fail-closed outcome.

### D6. Two processes from one codebase
```
python -m rates.api       uvicorn, N workers; reads only (writes nothing)
python -m rates.refresh   exactly one instance; the only writer of rate_observations
python scripts/declare_non_publication_day.py --date --reason --actor
```
The refresh process runs an asyncio loop. Its interval is 5 minutes on business days between 12:00 and 18:00 Asia/Jerusalem and 60 minutes otherwise; both are configurable. Each attempt is wrapped in `tenacity` retries: 3 attempts, exponential backoff with jitter, and a 10 s httpx timeout. Idempotent inserts mean an accidental second refresh instance is harmless to correctness.
*Alternative:* Running the scheduler inside the API lifespan behind a flag was rejected. With multiple uvicorn workers it starts N concurrent writers, which contend on SQLite locks.

### D7. Ingestion pipeline
fetch, then parse (Decimal), then validate the whole snapshot, then normalise, then **one transaction** that inserts the observations with on-conflict-do-nothing and writes an `RATES_INGESTED` audit event if any rows were inserted. A rejected snapshot is logged at WARNING with its reason and increments a failure counter. The last success time and the last publication date are written to the logs. They are also exposed through the API's `GET /health/ready`, which reads the latest observation timestamp.

### D8. Read path and error mapping
Read path: route validation (Pydantic), then `PairRepository.is_enabled`, then load the active policy and `CalendarView`, then the latest observation, then `evaluate`. Domain exceptions map to RFC 9457 problem responses in one exception handler:
`InvalidRequest` gives 400, `UnsupportedPair` 422, `RateNotFound` 404, `PolicyNotEvaluable` 503, and `StorageUnavailable` (SQLAlchemy `OperationalError`/`DBAPIError`) 503.
Policy and calendar are loaded on every request; at this data size that costs microseconds. This is what lets an operator declaration take effect immediately with no cache to invalidate.

### D9. Time handling
A `Clock` protocol with `SystemClock` and `FixedClock` implementations is injected through FastAPI dependencies and passed into the refresh loop. Time zones come from `zoneinfo`. `tzdata` is a hard dependency because Windows has no system time-zone database. Instants are stored and emitted in UTC. Business-day logic runs only in Asia/Jerusalem.

### D10. Operator script
The script opens a session, inserts the `non_publication_days` row (origin `OPERATOR`) and the `audit_events` row in one transaction, and exits non-zero if the reason is empty or the date already exists. There is no HTTP surface, matching the agreed scope.

### D11. Package layout
```
src/rates/
  domain/      models.py eligibility.py calendar.py errors.py
  services/    rate_query.py ingestion.py
  adapters/boi/   client.py parser.py
  adapters/db/    tables.py types.py repos.py session.py
  api/         app.py routes.py schemas.py problems.py __main__.py
  refresh/     __main__.py loop.py
  clock.py settings.py
migrations/    alembic env + schema + seed revisions
scripts/       declare_non_publication_day.py
tests/         unit/ integration/ contract/   (fixtures: recorded BoI payloads)
Dockerfile  docker-compose.yml  pyproject.toml  README.md  .env.example
```
The integration tests run against SQLite by default. The same suite runs against Postgres when `TEST_DATABASE_URL` is set, for example against the compose `postgres` service. This keeps the "switchable DB" claim tested.

### D12. Docker Compose with Postgres
```
docker compose up
  postgres  (postgres:16, healthcheck pg_isready, named volume)
     |  healthy
     v
  migrate   (same image; `alembic upgrade head`; exits 0)
     |  completed_successfully
     +------------------+
     v                  v
  api                 refresh
  (uvicorn, :8000)    (python -m rates.refresh, replicas: 1)
```
- One image for all app services; only the command differs.
- `depends_on` with `condition: service_healthy` / `service_completed_successfully` orders startup with no wait-for scripts.
- Configuration comes from environment variables (`DATABASE_URL`, policy-independent tuning such as poll intervals and the BoI URL), with `.env.example` documenting them. Policy values live in the database, not in env.
- `refresh` is pinned to one replica; `api` may be scaled (`--scale api=N` behind a proxy is out of scope, but uvicorn `--workers` is configurable).
- The operator script runs as `docker compose run --rm api python scripts/declare_non_publication_day.py ...`.
*Alternative:* SQLite in a volume shared by two containers was rejected because file locking across containers is fragile, and the brief asks for a runnable, credible setup.

### D13. README
The README is the reviewer's entry point and contains, in order:
1. The problem in one paragraph, and what a "representative rate" is and is not (not an executable trade price).
2. Quick start: `docker compose up`, sample `curl` calls, and the response for ELIGIBLE, NOT_ELIGIBLE and an error.
3. Architecture: a component diagram, the two processes, and the read and refresh flows.
4. The freshness policy: the rule, worked examples (weekend, intraday, holiday, unexpected miss), and the operator script.
5. Decisions and trade-offs: a table summarising D1–D12 and the deliberate change from "503 for stale" to "200 + NOT_ELIGIBLE".
6. Operating assumptions and limitations: grace and max-age values, the yearly calendar migration, holiday-eve timing, ingestion lag, the consumer's obligation to check `eligibility.status`, and no stated RPS/SLO.
7. Running without Docker (SQLite), running tests (SQLite and Postgres), and simulating BoI failures.
8. Out of scope and possible extensions.
9. Where AI assistance was used.

## Risks / Trade-offs

- [Consumer ignores `eligibility` on a 200] → `eligibility` is required in the OpenAPI schema, the README states the consumer's obligation, and the field names make it obvious. The risk remains and is accepted as a deliberate contract choice.
- [The calendar runs out on 2028-01-01] → The API returns 503 `POLICY_NOT_EVALUABLE`. That is intended fail-closed behaviour. The README gives the yearly step of adding a migration with the new BoI list.
- [BoI skips a business day for lack of data] → The API returns NOT_ELIGIBLE until an operator runs the script. This is intentionally strict.
- [Holiday-eve 12:15 publications aren't modelled] → On those days the previous rate stays eligible until the 15:15 + grace deadline. This errs on the side of keeping the previous rate eligible a little longer; the refresh process will already have ingested the new rate. Documented.
- [Ingestion lag between BoI publication and our poll] → Bounded by the 5-minute window interval and documented.
- [BoI changes the payload shape] → Snapshot validation rejects it as a whole, the last good data is preserved, and the failure is logged. Reads become NOT_ELIGIBLE after the deadline, which is the visible signal.
- [SQLite single-writer limits] → Only one writer exists by design and WAL allows concurrent readers. Postgres is the compose default.
- [Dialect drift between SQLite and Postgres] → All dialect differences live in `types.py`, and the same integration suite runs on both databases.
- [The 45-minute grace and 7-day cap are invented values] → Documented as assumptions, held in the versioned policy row, and changed only through a new migration.

## Migration Plan

Greenfield deployment. `docker compose up` starts Postgres, runs the `migrate` service (schema and seeds), then starts `api` and `refresh`. Rollback is `alembic downgrade`, or `docker compose down -v` to discard the database, since there are no external consumers yet.
