# Spec Delta

## ADDED Requirements

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

## MODIFIED Requirements

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

### Requirement: Atomic and idempotent persistence
The system SHALL store all new observations of the accepted currencies from one retrieval atomically. A pair and source timestamp that have already been stored SHALL NOT create a second observation.

#### Scenario: Re-ingesting the same snapshot
- **GIVEN** a snapshot has already been ingested
- **WHEN** the identical snapshot is retrieved and ingested again
- **THEN** the number of stored observations does not change

#### Scenario: Storage failure mid-write
- **WHEN** storage fails while new observations from a retrieval are being written
- **THEN** none of that retrieval's observations are stored

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

## REMOVED Requirements

### Requirement: Snapshot-level validation
**Reason**: Rejecting the whole snapshot when one currency is missing or invalid blocks every other currency, even though each pair's eligibility is judged on that pair's own publication date. For the deposit flow, a GBP problem must not stop USD.
**Migration**: Replaced by "Per-currency validation". Malformed payloads are still rejected as a whole. A missing or invalid enabled currency is now rejected on its own and reported in the logs, in the refresh outcome (`partial`) and in the ingestion audit record.
