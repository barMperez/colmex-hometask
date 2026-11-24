# Spec Delta

## MODIFIED Requirements

### Requirement: Exact decimal handling
The system SHALL keep source rates as exact decimal values from parsing through storage, on every supported database. It MUST NOT convert them to binary floating-point at any stage, and MUST NOT round them to a fixed number of decimal places.

#### Scenario: Rate survives round trip unchanged
- **GIVEN** BoI returns USD with `currentExchangeRate` 3.343
- **WHEN** the snapshot is ingested and the stored observation is read back
- **THEN** the stored source rate equals the decimal value 3.343 exactly

#### Scenario: More than ten decimal places survive on every database
- **WHEN** an observation with normalised rate 0.000012345678912 is stored and read back on SQLite and on Postgres
- **THEN** both return exactly 0.000012345678912

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
