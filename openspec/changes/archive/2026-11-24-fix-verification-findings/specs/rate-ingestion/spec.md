# Spec Delta

## MODIFIED Requirements

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

### Requirement: Snapshot-level validation
The system SHALL validate each retrieved snapshot as a whole and SHALL reject the entire snapshot, storing nothing from it, if any check fails. A valid snapshot contains every enabled pair. Each entry has a rate greater than zero, a source unit that is a positive integer, and a publication date not after the current Asia/Jerusalem date. The system SHALL NOT reject a snapshot because its entries carry different `lastUpdate` values.

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

#### Scenario: One future-dated entry rejects the whole snapshot
- **GIVEN** the current Asia/Jerusalem date is 2026-11-24
- **WHEN** a snapshot's USD and EUR entries convert to 2026-11-24 but its GBP entry converts to 2026-11-25
- **THEN** no observations from that snapshot are stored

#### Scenario: Malformed payload rejects snapshot
- **WHEN** BoI returns a non-JSON body or JSON without the expected `exchangeRates` structure
- **THEN** no observations are stored and existing observations are unchanged

#### Scenario: Currencies not enabled are ignored
- **WHEN** a valid snapshot also contains currencies that have no enabled pair
- **THEN** those entries are not stored and do not cause rejection

#### Scenario: Differing timestamps are accepted
- **WHEN** an otherwise valid snapshot has entries whose `lastUpdate` values differ
- **THEN** the snapshot is accepted and its new observations are stored

### Requirement: Observations are append-only
Stored observations SHALL NOT be updated or deleted by ingestion. A same-day correction by BoI, meaning a new source timestamp for the same publication date, SHALL be stored as a new observation. The current observation for a pair SHALL be the one with the latest publication date, and among those the latest source timestamp.

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

### Requirement: Ingestion is auditable and observable
Each ingestion that stores at least one new observation SHALL produce an audit record. Every retrieval attempt SHALL be recorded in logs with its outcome. After each attempt, the refresh process SHALL durably record the attempt time, its outcome and the time of the last successful ingestion. The readiness endpoint SHALL report that last successful ingestion time.

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
