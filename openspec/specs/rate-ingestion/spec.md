# rate-ingestion Specification

## Purpose

Fetches BoI representative exchange rates, validates and normalises them, and records each published observation durably and exactly once. Read requests are then served from verified local data rather than from BoI directly.

## Requirements

### Requirement: Periodic retrieval from BoI
A refresh process, separate from the API process, SHALL periodically retrieve the current representative rates from the BoI public exchange-rates API. It SHALL poll more often on business days between 12:00 and 18:00 Asia/Jerusalem than at other times. On business days it SHALL poll at the configured dense interval from the active policy's expected publication time until the configured tail after that day's publication deadline.

#### Scenario: Polling during the publication window
- **GIVEN** the configured window interval is 5 minutes
- **WHEN** it is a business day at 14:00 Asia/Jerusalem
- **THEN** the refresh process attempts a retrieval at least every 5 minutes

#### Scenario: Polling outside the publication window
- **GIVEN** the configured off-window interval is 60 minutes
- **WHEN** it is a Saturday
- **THEN** the refresh process attempts a retrieval at most every 60 minutes

#### Scenario: API process does not write rates
- **WHEN** the API process is running without the refresh process
- **THEN** no rate observations are created or modified

#### Scenario: Dense polling around a weekday deadline
- **GIVEN** a dense interval of 60 seconds, a dense tail of 30 minutes, and policy v1 (15:15 Mon-Thu, 45 minutes grace)
- **WHEN** it is Tuesday 2026-11-24 at 15:20 or at 16:29 Asia/Jerusalem
- **THEN** the next retrieval is scheduled 60 seconds later

#### Scenario: Dense polling around a Friday deadline
- **GIVEN** the same settings and policy v1 (12:15 on Fridays)
- **WHEN** it is Friday 2026-11-27 at 12:30 Asia/Jerusalem
- **THEN** the next retrieval is scheduled 60 seconds later

#### Scenario: After the dense tail
- **GIVEN** the same settings
- **WHEN** it is Tuesday 2026-11-24 at 16:31 Asia/Jerusalem
- **THEN** the next retrieval uses the publication-window interval

#### Scenario: Policy unavailable
- **WHEN** the active policy cannot be read
- **THEN** the refresh process falls back to the publication-window and off-window intervals

### Requirement: Exact decimal handling
The system SHALL keep source rates as exact decimal values from parsing through storage, on every supported database. It MUST NOT convert them to binary floating-point at any stage, and MUST NOT round them to a fixed number of decimal places.

#### Scenario: Rate survives round trip unchanged
- **GIVEN** BoI returns USD with `currentExchangeRate` 3.343
- **WHEN** the snapshot is ingested and the stored observation is read back
- **THEN** the stored source rate equals the decimal value 3.343 exactly

#### Scenario: More than ten decimal places survive on every database
- **WHEN** an observation with normalised rate 0.000012345678912 is stored and read back on SQLite and on Postgres
- **THEN** both return exactly 0.000012345678912

### Requirement: Source unit normalisation
The system SHALL store, for each observation, the source rate and source unit as published. It SHALL also store a normalised rate equal to the source rate divided by the source unit, meaning the value of 1 base-currency unit in the quote currency.

#### Scenario: Per-unit currency
- **WHEN** BoI returns USD with rate 3.343 and unit 1
- **THEN** the normalised rate is 3.343

#### Scenario: Per-100 currency
- **WHEN** BoI returns JPY with rate 2.1026 and unit 100
- **THEN** the normalised rate is 0.021026

### Requirement: Publication date derivation
The system SHALL derive each observation's publication date by converting that entry's own source `lastUpdate` timestamp to a calendar date in Asia/Jerusalem. It SHALL also store that source timestamp itself.

#### Scenario: UTC timestamp converted to Israel date
- **WHEN** BoI returns `lastUpdate` 2026-11-23T13:21:10.7104898Z
- **THEN** the observation's publication date is 2026-11-23
- **AND** the stored source timestamp is 2026-11-23T13:21:10.7104898Z (at least millisecond precision)

#### Scenario: Late UTC evening maps to next Israel date
- **WHEN** BoI returns `lastUpdate` 2026-11-23T22:30:00Z
- **THEN** the observation's publication date is 2026-11-24

#### Scenario: Entries with different timestamps
- **WHEN** a snapshot has USD with `lastUpdate` 2026-11-23T14:05:00Z and EUR with `lastUpdate` 2026-11-23T13:21:10Z
- **THEN** the USD observation stores source timestamp 14:05Z and the EUR observation stores 13:21:10Z
- **AND** both have publication date 2026-11-23

### Requirement: Per-currency validation
The system SHALL validate each enabled currency's entry independently. An entry is valid when it is present, has a rate greater than zero, a source unit that is a positive integer, and a publication date not after the current Asia/Jerusalem date. The system SHALL store the valid entries and SHALL reject and log each invalid or missing entry with its reason, without affecting the others. A payload without the expected `exchangeRates` structure SHALL be rejected as a whole.

#### Scenario: Missing enabled currency is rejected alone
- **GIVEN** USD, EUR and GBP pairs are enabled
- **WHEN** a snapshot contains new USD and EUR entries but no GBP entry
- **THEN** the USD and EUR observations are stored
- **AND** GBP is rejected and the rejection is logged with the reason "missing"

#### Scenario: Non-positive rate is rejected alone
- **WHEN** a snapshot contains EUR with rate 0 and valid USD and GBP entries
- **THEN** the USD and GBP observations are stored and EUR is rejected

#### Scenario: Future-dated entry is rejected alone
- **GIVEN** the current Asia/Jerusalem date is 2026-11-24
- **WHEN** a snapshot's USD and EUR entries convert to 2026-11-24 but its GBP entry converts to 2026-11-25
- **THEN** the USD and EUR observations are stored and GBP is rejected

#### Scenario: Every enabled currency invalid
- **GIVEN** the current Asia/Jerusalem date is 2026-11-24
- **WHEN** every enabled entry in a snapshot converts to 2026-11-25
- **THEN** no observations from that snapshot are stored

#### Scenario: Malformed payload rejects snapshot
- **WHEN** BoI returns a non-JSON body or JSON without the expected `exchangeRates` structure
- **THEN** no observations are stored and existing observations are unchanged

#### Scenario: Currencies not enabled are ignored
- **WHEN** a snapshot also contains currencies that have no enabled pair, including invalid ones
- **THEN** those entries are not stored and are not reported as rejected

#### Scenario: Differing timestamps are accepted
- **WHEN** a snapshot has valid entries whose `lastUpdate` values differ
- **THEN** each entry's new observation is stored

### Requirement: Atomic and idempotent persistence
The system SHALL store all new observations of the accepted currencies from one retrieval atomically. An entry whose pair, source timestamp, source rate and source unit equal those of the latest stored revision for that pair and source timestamp SHALL NOT create a second observation.

#### Scenario: Re-ingesting the same snapshot
- **GIVEN** a snapshot has already been ingested
- **WHEN** the identical snapshot is retrieved and ingested again
- **THEN** the number of stored observations does not change

#### Scenario: Storage failure mid-write
- **WHEN** storage fails while new observations from a retrieval are being written
- **THEN** none of that retrieval's observations are stored

### Requirement: Observations are append-only
Stored observations SHALL NOT be updated or deleted by ingestion. A correction by BoI, meaning a new source timestamp for the same publication date, or the same source timestamp with a different source rate or unit, SHALL be stored as a new observation. The current observation for a pair SHALL be the one with the latest publication date, then the latest source timestamp, then the highest revision.

#### Scenario: Same-day correction
- **GIVEN** a USD observation dated 2026-11-23 with source timestamp 13:21Z and rate 3.343 exists
- **WHEN** BoI later returns USD for 2026-11-23 with source timestamp 14:05Z and rate 3.341
- **THEN** both observations are retained
- **AND** the current USD observation has rate 3.341

#### Scenario: Single-currency correction leaves other pairs untouched
- **GIVEN** USD, EUR and GBP observations dated 2026-11-23 with source timestamp 13:21Z exist
- **WHEN** BoI returns a snapshot where only USD has a new source timestamp 14:05Z and EUR and GBP still carry 13:21Z
- **THEN** exactly one new observation, for USD, is stored
- **AND** the current EUR and GBP observations are unchanged

#### Scenario: Correction with an unchanged source timestamp
- **GIVEN** a USD observation with source timestamp 2026-11-23T13:21:10.7104898Z and rate 3.343 exists as revision 0
- **WHEN** BoI returns USD with the same source timestamp and rate 3.341
- **THEN** a new observation with rate 3.341 and revision 1 is stored, and the revision 0 observation is retained
- **AND** the current USD observation has rate 3.341 and a different `rateId` than before
- **AND** the correction is logged at warning level with the old and new rate, and listed in the ingestion audit record

#### Scenario: Value flips back
- **GIVEN** USD revisions 0 (3.343) and 1 (3.341) exist for the same source timestamp
- **WHEN** BoI returns USD with that source timestamp and rate 3.343 again
- **THEN** revision 2 with rate 3.343 is stored and is the current USD observation

#### Scenario: Repeating the latest revision is a no-op
- **GIVEN** the latest USD revision for a source timestamp has rate 3.341
- **WHEN** BoI returns USD with that source timestamp and rate 3.341 again
- **THEN** no observation is stored and no correction is logged

### Requirement: Failed retrieval preserves existing data
A failed retrieval SHALL leave all stored observations unchanged. Failures include timeouts, connection errors, non-2xx responses and rejected snapshots. The refresh process SHALL retry within bounded limits and continue on its schedule.

#### Scenario: BoI unavailable
- **GIVEN** a stored USD observation dated 2026-11-23
- **WHEN** every retrieval attempt times out
- **THEN** the stored observation and its timestamps are unchanged
- **AND** the refresh process attempts retrieval again at its next scheduled time

### Requirement: Ingestion is auditable and observable
Each ingestion that stores at least one new observation SHALL produce an audit record, which SHALL list any rejected enabled currencies with their reasons. Every retrieval attempt SHALL be recorded in logs with its outcome. After each attempt, the refresh process SHALL durably record the attempt time, its outcome and the time of the last successful ingestion. The readiness endpoint SHALL report that last successful ingestion time.

#### Scenario: New observations produce audit record
- **WHEN** a snapshot results in three new observations
- **THEN** one audit record identifies the ingestion, the pairs affected and the source timestamp of each new observation

#### Scenario: Duplicate snapshot produces no audit record
- **WHEN** a snapshot results in no new observations
- **THEN** no ingestion audit record is written

#### Scenario: Successful attempt with nothing new is still a success
- **GIVEN** the latest observation was retrieved at 2026-11-23T13:25:00Z
- **WHEN** an ingestion at 2026-11-24T09:00:00Z succeeds but stores no new observation
- **THEN** the readiness endpoint reports `lastSuccessfulIngestionAt` 2026-11-24T09:00:00Z
- **AND** `latestRetrievedAt` is still 2026-11-23T13:25:00Z

#### Scenario: Failed attempt does not move the last success
- **GIVEN** the last successful ingestion was at 2026-11-24T09:00:00Z
- **WHEN** a later attempt fails because BoI is unavailable
- **THEN** the readiness endpoint still reports `lastSuccessfulIngestionAt` 2026-11-24T09:00:00Z
- **AND** the recorded outcome of the latest attempt is a failure

#### Scenario: No successful ingestion yet
- **WHEN** the refresh process has never completed a successful ingestion
- **THEN** the readiness endpoint reports `lastSuccessfulIngestionAt` as null and still returns 200 while the database is reachable

#### Scenario: Audit record lists rejected currencies
- **WHEN** a snapshot has new USD and EUR entries and no GBP entry
- **THEN** the audit record for the new USD and EUR observations lists GBP as rejected with the reason "missing"

### Requirement: Refresh attempt outcomes
The refresh process SHALL classify each attempt as success when every enabled currency was accepted, partial when at least one was accepted and at least one rejected, and failure otherwise. Only a success SHALL move the last successful ingestion time. Partial and failure SHALL increment the consecutive-failure count. A partial attempt SHALL log each rejected currency and its reason at error level.

#### Scenario: Partial ingestion
- **GIVEN** the last successful ingestion was at 2026-11-24T09:00:00Z
- **WHEN** at 2026-11-24T13:25:00Z a snapshot has new USD and EUR entries and no GBP entry
- **THEN** the recorded outcome is partial, the consecutive-failure count increases and `lastSuccessfulIngestionAt` stays 2026-11-24T09:00:00Z
- **AND** the logs name GBP and its rejection reason at error level

#### Scenario: Every currency rejected
- **WHEN** every enabled entry in a snapshot is invalid
- **THEN** the recorded outcome is a failure and no observations are stored

#### Scenario: Success after partial
- **GIVEN** the previous attempt was partial
- **WHEN** the next snapshot has every enabled currency valid
- **THEN** the recorded outcome is success, the consecutive-failure count resets to 0 and `lastSuccessfulIngestionAt` moves to that attempt
