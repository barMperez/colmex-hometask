# supported-pairs Specification

## Purpose

Defines which currency pairs the service serves and ingests, and how an operator changes that set safely: every change is recorded in the audit trail and takes effect without a redeploy or restart.

## Requirements

### Requirement: Pairs are configured in the database
The set of supported pairs SHALL be stored in the database, each with an enabled flag. Only enabled pairs SHALL be served by the read API, ingested by the refresh process and listed in readiness.

#### Scenario: Disabled pair is neither served nor ingested
- **GIVEN** GBP/ILS is configured but disabled
- **WHEN** a snapshot without a GBP entry is ingested and a client requests GBP/ILS
- **THEN** the ingestion outcome is success and the read returns 422 `UNSUPPORTED_CURRENCY_PAIR`

### Requirement: Operator enables or disables a pair
An operator SHALL be able to enable or disable a configured pair, giving a reason and an actor. The change and an audit record with the before and after state SHALL be stored atomically and take effect on the next read and the next refresh attempt without a restart. Unknown pairs SHALL be refused.

#### Scenario: Disabling a pair BoI stopped sending
- **GIVEN** BoI has stopped sending GBP, so every refresh attempt is partial
- **WHEN** an operator disables GBP/ILS with a reason
- **THEN** the next refresh attempt is a success and GBP/ILS reads return 422 `UNSUPPORTED_CURRENCY_PAIR`
- **AND** an audit record holds the actor, the reason and the enabled state before and after

#### Scenario: Re-enabling a pair
- **GIVEN** GBP/ILS is disabled
- **WHEN** an operator enables it with a reason
- **THEN** GBP/ILS is served and ingested again and an audit record is written

#### Scenario: Unknown pair refused
- **WHEN** an operator tries to enable JPY/ILS, which is not configured
- **THEN** nothing changes and no audit record is written

#### Scenario: No-op change refused
- **WHEN** an operator tries to disable a pair that is already disabled
- **THEN** nothing changes and no audit record is written
