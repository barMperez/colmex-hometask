# Proposal

## Why

The service sits on a customer's critical path: a deposit in ILS must be converted to USD before it reaches the trading platform. When the service says "no" (a NOT_ELIGIBLE rate or a 503), the deposit is blocked, and that can cost the customer money. Using a stale rate is just as damaging, so the standard is: never block when a usable rate exists, and when blocking is right, keep it short and make sure someone knows.

Measured against that standard, today's code has four gaps:
- **One bad currency blocks the others.** A missing or malformed GBP or EUR entry rejects the whole BoI snapshot, so USD deposits stop at the next deadline.
- **A BoI non-publication is silent.** Every poll still succeeds, so a pair can sit overdue for hours with no alert.
- **The calendar runs out on a known date.** Coverage ends on 2027-12-31, and then every read returns 503, with no advance warning.
- **Postgres doesn't restart.** In Compose, a Postgres crash takes the service down until someone intervenes.

Separately, our polling interval around the publication deadline adds avoidable minutes of NOT_ELIGIBLE after BoI publishes late.

## What Changes

- **Per-currency validation.** Each enabled currency is validated and stored on its own. A missing or invalid currency is rejected and logged on its own, while valid currencies are stored. A payload with no usable structure is still rejected as a whole.
- **Partial-ingestion outcome.** When some enabled currencies are rejected, the attempt is recorded as `partial`. It stores the valid currencies, does not move `lastSuccessfulIngestionAt`, and increments the consecutive-failure count. The ingestion audit record lists the rejected currencies and why.
- **Not-eligible pairs are observable.**
  - `/health/ready` lists every enabled pair that is not currently ELIGIBLE, with its reason.
  - The refresh process logs one ERROR line when a pair becomes not eligible, and an INFO line when it recovers.
  - Readiness status stays independent of freshness.
- **Calendar expiry warning.** `/health/ready` reports the last date the non-publication calendar covers, and a warning flag within a configurable number of days of it (default 60). The refresh process logs a WARNING once a day while the flag is set.
- **Dense polling around the deadline.** On business days, the refresh process polls at a short interval (default 60 s) from the expected publication time until 30 minutes after the deadline. Times come from the active policy.
- **Postgres restarts.** The Compose `postgres` service gets `restart: unless-stopped`.
- **Docs: conversion direction.** The README and OpenAPI document that the API serves `1 base = rate quote` (for example USD/ILS), with a worked ILS-to-USD example. Rounding stays the consumer's job. No inverse pair is added.

The only breaking change is in ingestion: a snapshot missing an enabled currency is no longer rejected outright. The `/health/ready` response only gains fields.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `rate-ingestion`:
  - Snapshot-level validation is replaced by per-currency validation.
  - Atomic persistence now refers to the accepted currencies.
  - Retrieval polling gains a dense deadline window.
  - Observability covers partial outcomes and rejected currencies.
- `rate-freshness-policy`: adds observability of not-eligible pairs and an advance warning before the calendar runs out.

## Impact

- **Code:**
  - `adapters/boi/parser.py`: per-currency accept/reject result
  - `services/ingestion.py`: partial outcome, audit payload
  - `refresh/loop.py`: partial outcome, dense polling, not-eligible transition log, calendar warning log
  - `services/rate_query.py`, `api/schemas.py`, `api/routes.py`: readiness fields
  - `settings.py`: new intervals and thresholds
  - `domain/calendar.py`: coverage end
- **Ops:** `docker-compose.yml` restart policy for Postgres.
- **API:** `/health/ready` gains `notEligiblePairs`, `calendarCoverageEndsOn` and `calendarExpiryWarning`. The exchange-rates endpoint is unchanged.
- **Database:** no schema change. The existing `refresh_status.last_attempt_outcome` column holds the new `partial` value.
- **Tests:** parser, ingestion, refresh loop (intervals, transitions, partial), readiness contract.
- **Docs:** README §3 (refresh flow), §4, §6 and §7, plus the OpenAPI description.
