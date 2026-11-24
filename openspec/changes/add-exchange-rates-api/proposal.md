# Proposal

## Why

A financially critical process needs the latest Bank of Israel (BoI) representative exchange rates for at least USD, EUR and GBP against ILS. Getting it wrong is costly in both directions. A rate that is silently out of date leads to wrong financial decisions. An API that fails whenever BoI is slow or closed blocks the process for no reason. The service must say plainly which rate it has, when BoI published it, and whether that rate is acceptable for use right now.

## What Changes

- New HTTP API `GET /api/v1/exchange-rates?base=XXX&quote=YYY`. It returns the latest ingested BoI representative rate for a supported pair, including its publication date, source metadata and an explicit **eligibility** assessment (`ELIGIBLE` / `NOT_ELIGIBLE` + reason).
- An out-of-date but authentic rate is returned as `200` with `NOT_ELIGIBLE`, not as an error. Error responses (`application/problem+json`) cover real faults only: malformed input, unsupported pair, no rate ever ingested, policy not evaluable, storage unavailable.
- A freshness policy based on BoI's publication calendar. A rate is eligible when it is at least as recent as the latest **expected** BoI publication, which is computed from business days, the official BoI non-publication list, the expected publication time and a grace period.
- Fail-closed behaviour. When the policy cannot be evaluated (no calendar data for the current year, or missing policy configuration), the API refuses to assess eligibility instead of guessing.
- A separate refresh process polls the BoI public API. It validates each snapshot as a whole, normalises source units, and stores rate observations append-only and idempotently. It is the only writer of rate data.
- An operator script declares an unexpected non-publication day. It writes the day and an audit record in one transaction. This is the only runtime policy mutation in this change.
- Persistence goes through SQLAlchemy behind a database-agnostic layer. Postgres is the database for the `docker compose` setup. SQLite is supported for zero-setup local runs and fast tests. Switching between them is a configuration change.
- A `docker compose` setup runs Postgres, migrations, the API and the refresh process with one command.
- A README explains the problem, the architecture, key decisions and trade-offs, operating assumptions, and how to run and test the project.

Out of scope: trading, rate locking and quote confirmation; inverse and cross pairs; an admin HTTP API or UI for policy changes; in-memory caching; multi-region or distributed deployment.

## Capabilities

### New Capabilities
- `rate-freshness-policy`: when a stored rate observation counts as eligible. Covers the business-day calendar, expected publication times and grace period, the maximum-age cap, fail-closed evaluation, and operator-declared non-publication days.
- `rate-ingestion`: how BoI rates are fetched, validated, normalised and durably recorded by the refresh process, including snapshot-level validation, idempotency and correction handling.
- `exchange-rate-query`: the HTTP read contract. Covers request validation, supported-pair checks, response shape including eligibility and provenance, decimal encoding, and the mapping of failure conditions to status codes.

### Modified Capabilities
<!-- None: no existing specs. -->

## Impact

- New Python 3.12+ codebase: FastAPI, Pydantic v2, SQLAlchemy 2.0 (async) with Alembic, httpx, tenacity, zoneinfo with `tzdata`, and pytest with respx.
- Postgres (via docker compose) or SQLite database, with migrations that seed the supported pairs (USD/EUR/GBP to ILS), policy version 1, and the BoI 2026–2027 non-publication calendar.
- Outbound dependency on `https://boi.org.il/PublicApi/GetExchangeRates`.
- Two runtime processes: the API (scalable workers, read-only on rates) and the refresh process (single instance, only writer).
- New Dockerfile and `docker-compose.yml` (services: `postgres`, `migrate`, `api`, `refresh`).
- The README must state operating assumptions: grace period, the maximum-age cap, consumer obligation to check `eligibility.status`, and the need to load each new year's BoI calendar.
