# Tasks

## 1. Project scaffolding

- [x] 1.1 Create `pyproject.toml` (Python 3.12+) with runtime deps (fastapi, uvicorn, pydantic, pydantic-settings, sqlalchemy[asyncio], alembic, aiosqlite, asyncpg, httpx, tenacity, tzdata) and dev deps (pytest, pytest-asyncio, respx). Verify: `pip install -e .[dev]` succeeds and `python -c "import zoneinfo; zoneinfo.ZoneInfo('Asia/Jerusalem')"` runs on Windows.
- [x] 1.2 Create the `src/rates/` package layout from design D11, with empty modules, plus `tests/unit`, `tests/integration` and `tests/contract`. Verify: `pytest` runs and collects 0 tests without errors.
- [x] 1.3 Add `settings.py` (pydantic-settings: `DATABASE_URL`, BoI URL, HTTP timeout, window and off-window poll intervals) and `.env.example`. Verify: a unit test loads settings from env overrides.
- [x] 1.4 Add `clock.py` with the `Clock` protocol, `SystemClock` and `FixedClock`. Verify: a unit test shows `FixedClock` returns an aware UTC instant and `SystemClock` returns an aware datetime.
- [x] 1.5 Start `README.md` with the problem statement and the representative-rate caveat (D13 §1), and an empty outline of the remaining sections. Verify: the file renders and the outline matches D13.

## 2. Domain: calendar and eligibility evaluator

- [x] 2.1 Implement domain models (`RateObservation`, `Policy`, `Eligibility`, eligibility status and reason enums) and domain errors (`PolicyNotEvaluable`, `UnsupportedPair`, `RateNotFound`, `StorageUnavailable`). Verify: the module imports cleanly and models reject float rates (a `Decimal` is required).
- [x] 2.2 Implement `CalendarView` (non-publication dates and covered years) with `is_business_day(date)`, raising `PolicyNotEvaluable` for uncovered years. Verify: unit tests for weekend, listed holiday (2026-10-27), ordinary weekday, and an uncovered year.
- [x] 2.3 Implement the deadline computation (15:15 Mon–Thu, 12:15 Fri, + grace, Asia/Jerusalem). Verify: unit tests for the weekday and Friday deadlines and the DST-transition scenario from `rate-freshness-policy`.
- [x] 2.4 Implement the required-publication-date walk-back, bounded by `max_age_days + 1`. Verify: unit tests for the before-deadline, after-deadline, weekend, holiday and unloaded-year scenarios.
- [x] 2.5 Implement the pure `evaluate(observation, now, calendar, policy)` with check order future date → max age → required date, setting policy version and `evaluatedAt`. Verify: one parametrised table test covers every scenario in `specs/rate-freshness-policy/spec.md` (except the operator scenarios, which are covered in group 4) and passes.
- [x] 2.6 Write README §4 (freshness policy rule and worked examples). Verify: each README example matches a row of the 2.5 test table.

## 3. Persistence layer

- [x] 3.1 Implement `adapters/db/types.py`: `DecimalText` (TEXT on SQLite, NUMERIC(18,10) on PG), `UtcDateTime` (rejects naive datetimes), and the JSON/JSONB variant. Verify: unit tests show `Decimal("3.343")` round-trips exactly and a naive datetime raises an error.
- [x] 3.2 Implement `tables.py` with the D4 schema (unique key and index on `rate_observations`). Implement `session.py`: async engine from `DATABASE_URL`, and on SQLite enable WAL, foreign keys and busy timeout. Verify: an integration test creates the schema on SQLite and checks the PRAGMAs.
- [x] 3.3 Set up Alembic with a schema revision that works on both dialects. Verify: `alembic upgrade head` then `alembic downgrade base` succeeds on SQLite.
- [x] 3.4 Add seed data migrations: the 3 enabled pairs (USD, EUR, GBP → ILS), policy v1 (45 min grace, 7-day cap, 15:15/12:15, Asia/Jerusalem), the 2026 and 2027 BoI non-publication dates (origin `BOI_CALENDAR`), and `calendar_coverage` rows for 2026 and 2027. Verify: an integration test after upgrade counts 3 pairs, 1 active policy, 16 + 13 non-publication days and 2 coverage years.
- [x] 3.5 Implement repository interfaces and SQLAlchemy implementations (pairs, rates with `latest_for_pair` ordering and idempotent `insert_many`, calendar view loader, active policy, audit). Insert-or-ignore is chosen by dialect. Verify: integration tests for latest-observation ordering (including same-day correction), duplicate insert ignored, and an empty active policy returning none.
- [x] 3.6 Make the integration suite run on Postgres when `TEST_DATABASE_URL` is set. Verify: the suite passes on SQLite, and on Postgres against `docker compose up postgres`.

## 4. Operator script

- [x] 4.1 Implement `scripts/declare_non_publication_day.py` (`--date --reason --actor`): it inserts the day (origin `OPERATOR`) and the audit event in one transaction, and exits non-zero on an empty reason or an existing date. Verify: integration tests for success (day plus audit row), missing reason (nothing written), and duplicate date.
- [x] 4.2 Add an integration test for the operator scenario in `rate-freshness-policy`: an observation dated 2026-12-01 is NOT_ELIGIBLE after the 2026-12-02 deadline, and ELIGIBLE after the declaration, with no restart. Verify: the test passes.
- [x] 4.3 Document the operator script in README §4 (when to use it, the command, the audit effect). Verify: the documented command runs as written against the local database.

## 5. BoI ingestion and refresh process

- [x] 5.1 Record BoI payload fixtures: a real snapshot, one missing GBP, a zero rate, a future `lastUpdate`, malformed JSON, and a same-day correction. Verify: the fixture files exist under `tests/fixtures/boi/`.
- [x] 5.2 Implement `adapters/boi/client.py` (httpx, 10 s timeout, `response.json(parse_float=Decimal)`, tenacity 3 attempts with exponential backoff and jitter). Verify: respx tests for success, timeout retried then failed, and non-2xx; parsed rates are `Decimal`, never `float`.
- [x] 5.3 Implement `parser.py` and snapshot validation (all enabled pairs present, rate > 0, unit a positive integer, consistent `lastUpdate`, publication date not after today in Israel, unknown currencies ignored), plus unit normalisation and publication-date derivation. Verify: unit tests for every validation and normalisation scenario in `specs/rate-ingestion/spec.md`, including JPY 2.1026/100 → 0.021026 and 22:30Z → next Israel date.
- [x] 5.4 Implement `services/ingestion.py`: validate the snapshot, then in one transaction insert observations and write a `RATES_INGESTED` audit event only if rows were inserted. Verify: integration tests for the idempotent re-ingest, no audit on duplicate, rollback on mid-write failure, and same-day correction kept.
- [x] 5.5 Implement `refresh/loop.py` and `__main__.py`: an asyncio loop with window and off-window intervals based on the business calendar and the injected clock, logging each attempt's outcome and the last success time, and continuing after failures. Verify: unit tests with `FixedClock` for interval selection, and an integration test showing a failed fetch leaves stored observations unchanged.
- [x] 5.6 Add the decimal round-trip test: ingest the fixture `3.343` and read it back from the DB as exactly `Decimal("3.343")`. Verify: the test passes on SQLite and Postgres.

## 6. HTTP API

- [x] 6.1 Implement `api/schemas.py`: the response model with required `eligibility`, `Decimal` fields serialised as strings, and UTC `Z` timestamps. Verify: a unit test shows serialised `rate` is the JSON string `"3.343"` and timestamps end with `Z`.
- [x] 6.2 Implement `services/rate_query.py` (pair check → load policy and calendar → latest observation → evaluate) and `api/routes.py` (`GET /api/v1/exchange-rates`, case-insensitive 3-letter codes, clock injected via dependency). Verify: contract tests for an ELIGIBLE 200, a NOT_ELIGIBLE 200 with reason and required date, lowercase codes, and a stable `rateId`.
- [x] 6.3 Implement `api/problems.py`: an RFC 9457 handler mapping 400 `INVALID_REQUEST`, 422 `UNSUPPORTED_CURRENCY_PAIR`, 404 `RATE_NOT_FOUND`, 503 `POLICY_NOT_EVALUABLE` (no rate in the body) and 503 `STORAGE_UNAVAILABLE`. Verify: a contract test for every error scenario in `specs/exchange-rate-query/spec.md`, checking the `application/problem+json` content type.
- [x] 6.4 Add `GET /health/live` and `GET /health/ready` (DB reachable, plus the latest observation time). Verify: tests for 200 when the DB is up and 503 when the DB is unavailable.
- [x] 6.5 Add a contract test showing reads never call BoI (respx asserts no outbound calls during reads) and that the OpenAPI document marks `eligibility` as required. Verify: the tests pass.
- [x] 6.6 Write README §2 (quick start curl examples with ELIGIBLE, NOT_ELIGIBLE and error responses) and §3 (architecture diagram, the two processes, read and refresh flows). Verify: the example responses match the contract test fixtures.

## 7. Docker Compose with Postgres

- [x] 7.1 Write a `Dockerfile` (python:3.12-slim, non-root user, installs the package; one image for api, refresh and migrate). Verify: `docker build .` succeeds.
- [x] 7.2 Write `docker-compose.yml` with services `postgres` (healthcheck, named volume), `migrate` (`alembic upgrade head`), `api` (:8000) and `refresh` (1 replica), using `depends_on` conditions per D12. Verify: `docker compose up` brings the API to healthy and `curl localhost:8000/health/ready` returns 200.
- [x] 7.3 Write README §7: running without Docker (SQLite), running tests on SQLite and Postgres, simulating BoI failure (point the BoI URL at an unreachable host), and the `docker compose run` operator command. Verify: each documented command runs as written.

## 8. End-to-end checks and README completion

- [x] 8.1 End-to-end smoke test on compose: after `docker compose up`, the refresh process ingests real BoI data and `GET /api/v1/exchange-rates?base=USD&quote=ILS` returns 200 with an `eligibility` block. Verify: a manual run, with the output captured in the README.
- [x] 8.2 Complete README §5 (decisions and trade-offs table D1–D13, including the 503 → 200 + NOT_ELIGIBLE change), §6 (assumptions and limitations), §8 (out of scope and extensions) and §9 (AI-assistance note). Verify: every design decision and risk in `design.md` appears in the README.
- [x] 8.3 Run the full test suite on SQLite and on Postgres, and run `openspec validate add-exchange-rates-api --strict`. Verify: all green.

## Workflow follow-up

- Archive the change once the implementation is reviewed and accepted.
