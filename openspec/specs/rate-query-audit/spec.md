# rate-query-audit Specification

## Purpose

Makes every rate handed out by the API traceable: who asked, exactly what they asked for, which stored observation they received, and what eligibility verdict they were told, so that an auditor can reconstruct any conversion decision afterwards.

## Requirements

### Requirement: Every authenticated rate query is audited
The system SHALL write one `RATE_QUERIED` audit record for every authenticated request to `GET /api/v1/exchange-rates`, whatever its outcome. Requests rejected for authentication (401) SHALL NOT produce an audit record and SHALL be logged instead.

#### Scenario: Successful read is audited
- **GIVEN** user `demo` holds a valid token
- **WHEN** `demo` requests `base=usd&quote=ils` and receives 200
- **THEN** exactly one `RATE_QUERIED` audit record exists for that request with actor `demo`

#### Scenario: Rejected read is audited
- **WHEN** `demo` requests `base=ILS&quote=USD` and receives 422
- **THEN** one `RATE_QUERIED` audit record exists with actor `demo`, status 422, code `UNSUPPORTED_CURRENCY_PAIR` and a null `rateId`

#### Scenario: Unauthenticated request is not audited
- **WHEN** a request without a token receives 401
- **THEN** no audit record is written and the rejection is logged

### Requirement: Audit record content
Each `RATE_QUERIED` record SHALL contain the actor, the time, the request's `base` and `quote` exactly as sent (null when absent), the HTTP status and error code (if any), and the `rateId` (null when no rate was returned). When a rate is returned, it SHALL also contain the rate, its `publicationDate` and the full eligibility verdict returned to the caller.

#### Scenario: Returned rate and verdict recorded
- **GIVEN** the current USD/ILS observation has rate 3.343, publication date 2026-11-23 and is assessed NOT_ELIGIBLE with reason PUBLICATION_OVERDUE under policy version 1
- **WHEN** `demo` requests `base=usd&quote=ils` and receives 200
- **THEN** the audit record contains `base` "usd", `quote` "ils", status 200, the response's `rateId`, rate "3.343", `publicationDate` 2026-11-23
- **AND** eligibility status NOT_ELIGIBLE, reason PUBLICATION_OVERDUE, the required publication date, policy version 1 and the `evaluatedAt` returned to the caller

#### Scenario: Missing parameter recorded as absent
- **WHEN** `demo` requests `GET /api/v1/exchange-rates?base=USD` and receives 400
- **THEN** the audit record contains `base` "USD", `quote` null, status 400 and code `INVALID_REQUEST`

#### Scenario: Audit links to the stored observation
- **WHEN** an audit record has a non-null `rateId`
- **THEN** a stored rate observation with that id exists

### Requirement: No rate without an audit record
The system SHALL return a rate only after its audit record has been durably stored. If storing it fails, the system SHALL respond 503 with code `AUDIT_UNAVAILABLE` and SHALL NOT include a rate. For a request that returns no rate, a failed audit write SHALL be logged at error level and SHALL NOT change the response.

#### Scenario: Audit store fails on a successful read
- **GIVEN** the audit record cannot be written
- **WHEN** `demo` requests `base=USD&quote=ILS` and a rate is available
- **THEN** the response status is 503 with code `AUDIT_UNAVAILABLE` and the body contains no `rate`

#### Scenario: Audit store fails on a rejected read
- **GIVEN** the audit record cannot be written
- **WHEN** `demo` requests `base=ILS&quote=USD`
- **THEN** the response status is 422 with code `UNSUPPORTED_CURRENCY_PAIR`
- **AND** the failed audit write is logged at error level
