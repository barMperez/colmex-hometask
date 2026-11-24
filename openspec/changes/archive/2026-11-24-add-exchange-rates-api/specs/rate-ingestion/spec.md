# Spec Delta

## Purpose

Fetches BoI representative exchange rates, validates and normalises them, and records each published observation durably and exactly once. Read requests are then served from verified local data rather than from BoI directly.

## ADDED Requirements

### Requirement: Periodic retrieval from BoI
A refresh process, separate from the API process, SHALL periodically retrieve the current representative rates from the BoI public exchange-rates API. It SHALL poll more often on business days between 12:00 and 18:00 Asia/Jerusalem than at other times.

#### Scenario: Polling during the publication window
- **GIVEN** the configured window interval is 5 minutes
- **WHEN** it is a business day at 15:20 Asia/Jerusalem
- **THEN** the refresh process attempts a retrieval at least every 5 minutes

#### Scenario: Polling outside the publication window
- **GIVEN** the configured off-window interval is 60 minutes
- **WHEN** it is a Saturday
- **THEN** the refresh process attempts a retrieval at most every 60 minutes

#### Scenario: API process does not write rates
- **WHEN** the API process is running without the refresh process
- **THEN** no rate observations are created or modified

### Requirement: Exact decimal handling
The system SHALL keep source rates as exact decimal values from parsing through storage. It MUST NOT convert them to binary floating-point at any stage.

#### Scenario: Rate survives round trip unchanged
- **GIVEN** BoI returns USD with `currentExchangeRate` 3.343
- **WHEN** the snapshot is ingested and the stored observation is read back
- **THEN** the stored source rate equals the decimal value 3.343 exactly

### Requirement: Source unit normalisation
The system SHALL store, for each observation, the source rate and source unit as published. It SHALL also store a normalised rate equal to the source rate divided by the source unit, meaning the value of 1 base-currency unit in the quote currency.

#### Scenario: Per-unit currency
- **WHEN** BoI returns USD with rate 3.343 and unit 1
- **THEN** the normalised rate is 3.343

#### Scenario: Per-100 currency
- **WHEN** BoI returns JPY with rate 2.1026 and unit 100
- **THEN** the normalised rate is 0.021026

### Requirement: Publication date derivation
The system SHALL derive an observation's publication date by converting the source `lastUpdate` timestamp to a calendar date in Asia/Jerusalem. It SHALL also store the source timestamp itself.

#### Scenario: UTC timestamp converted to Israel date
- **WHEN** BoI returns `lastUpdate` 2026-11-23T13:21:10.7104898Z
- **THEN** the observation's publication date is 2026-11-23
- **AND** the stored source timestamp is 2026-11-23T13:21:10.7104898Z (at least millisecond precision)

#### Scenario: Late UTC evening maps to next Israel date
- **WHEN** BoI returns `lastUpdate` 2026-11-23T22:30:00Z
- **THEN** the observation's publication date is 2026-11-24

### Requirement: Snapshot-level validation
The system SHALL validate each retrieved snapshot as a whole and SHALL reject the entire snapshot, storing nothing from it, if any check fails. A valid snapshot contains every enabled pair. Each entry has a rate greater than zero, a source unit that is a positive integer, and a publication date not after the current Asia/Jerusalem date.

#### Scenario: Missing enabled currency rejects snapshot
- **GIVEN** USD, EUR and GBP pairs are enabled
- **WHEN** a snapshot contains USD and EUR but not GBP
- **THEN** no observations from that snapshot are stored
- **AND** the rejection is logged with the reason

#### Scenario: Non-positive rate rejects snapshot
- **WHEN** a snapshot contains EUR with rate 0
- **THEN** no observations from that snapshot are stored

#### Scenario: Future publication date rejects snapshot
- **WHEN** the current Asia/Jerusalem date is 2026-11-24 and a snapshot's `lastUpdate` converts to 2026-11-25
- **THEN** no observations from that snapshot are stored

#### Scenario: Malformed payload rejects snapshot
- **WHEN** BoI returns a non-JSON body or JSON without the expected `exchangeRates` structure
- **THEN** no observations are stored and existing observations are unchanged

#### Scenario: Currencies not enabled are ignored
- **WHEN** a valid snapshot also contains currencies that have no enabled pair
- **THEN** those entries are not stored and do not cause rejection

### Requirement: Atomic and idempotent persistence
The system SHALL store all new observations from a valid snapshot atomically. A pair and source timestamp that have already been stored SHALL NOT create a second observation.

#### Scenario: Re-ingesting the same snapshot
- **GIVEN** a snapshot has already been ingested
- **WHEN** the identical snapshot is retrieved and ingested again
- **THEN** the number of stored observations does not change

#### Scenario: Storage failure mid-write
- **WHEN** storage fails while new observations from a valid snapshot are being written
- **THEN** none of that snapshot's observations are stored

### Requirement: Observations are append-only
Stored observations SHALL NOT be updated or deleted by ingestion. A same-day correction by BoI, meaning a new source timestamp for the same publication date, SHALL be stored as a new observation. The current observation for a pair SHALL be the one with the latest publication date, and among those the latest source timestamp.

#### Scenario: Same-day correction
- **GIVEN** a USD observation dated 2026-11-23 with source timestamp 13:21Z and rate 3.343 exists
- **WHEN** BoI later returns USD for 2026-11-23 with source timestamp 14:05Z and rate 3.341
- **THEN** both observations are retained
- **AND** the current USD observation has rate 3.341

### Requirement: Failed retrieval preserves existing data
A failed retrieval SHALL leave all stored observations unchanged. Failures include timeouts, connection errors, non-2xx responses and rejected snapshots. The refresh process SHALL retry within bounded limits and continue on its schedule.

#### Scenario: BoI unavailable
- **GIVEN** a stored USD observation dated 2026-11-23
- **WHEN** every retrieval attempt times out
- **THEN** the stored observation and its timestamps are unchanged
- **AND** the refresh process attempts retrieval again at its next scheduled time

### Requirement: Ingestion is auditable and observable
Each ingestion that stores at least one new observation SHALL produce an audit record. Every retrieval attempt SHALL be recorded in logs with its outcome, and the time of the last successful ingestion SHALL be observable.

#### Scenario: New observations produce audit record
- **WHEN** a snapshot results in three new observations
- **THEN** one audit record identifies the ingestion, the pairs affected and the source timestamp

#### Scenario: Duplicate snapshot produces no audit record
- **WHEN** a snapshot results in no new observations
- **THEN** no ingestion audit record is written
