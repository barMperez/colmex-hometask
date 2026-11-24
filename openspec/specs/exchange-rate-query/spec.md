# exchange-rate-query Specification

## Purpose

The HTTP contract consumers use to read the current BoI representative rate for a currency pair, together with its provenance and an explicit eligibility assessment. Failure responses are reserved for real faults.

## Requirements

### Requirement: Read a single pair
The system SHALL expose `GET /api/v1/exchange-rates` with required query parameters `base` and `quote`. Each is an ISO 4217 alphabetic code of three letters, matched case-insensitively. The response SHALL describe the current observation for that pair.

#### Scenario: Successful read
- **GIVEN** USD/ILS is enabled and a current observation exists
- **WHEN** a client requests `GET /api/v1/exchange-rates?base=USD&quote=ILS`
- **THEN** the response status is 200
- **AND** the body contains `baseCurrency` "USD" and `quoteCurrency` "ILS"

#### Scenario: Lowercase codes accepted
- **WHEN** a client requests `GET /api/v1/exchange-rates?base=usd&quote=ils`
- **THEN** the response is the same as for `base=USD&quote=ILS`

### Requirement: Response content
A 200 response SHALL include the following fields:
- `baseCurrency` and `quoteCurrency`
- `rate`: the normalised value of 1 base unit in the quote currency
- `publicationDate`
- `eligibility`: with `status`, `reason` when not eligible, `requiredPublicationDate`, `evaluatedAt` and `policyVersion`
- `source`: with name, rate type, source timestamp, source rate and source unit
- `retrievedAt`
- `rateId`: a stable identifier of the observation

#### Scenario: Eligible response
- **GIVEN** the current USD/ILS observation is assessed ELIGIBLE under policy version 1
- **WHEN** a client reads USD/ILS
- **THEN** `eligibility.status` is "ELIGIBLE" and `eligibility.policyVersion` is 1
- **AND** `source.name` is "BANK_OF_ISRAEL" and `source.type` is "REPRESENTATIVE"

#### Scenario: Same observation yields same rateId
- **WHEN** a client reads USD/ILS twice and no new observation was ingested in between
- **THEN** both responses carry the same `rateId`

### Requirement: Not-eligible rates are returned, not errored
When a current observation exists but is assessed NOT_ELIGIBLE, the system SHALL respond with status 200. The body SHALL contain the observation with `eligibility.status` "NOT_ELIGIBLE", the reason, and the required publication date. `eligibility` SHALL always be present in every 200 response.

#### Scenario: Overdue rate
- **GIVEN** the latest USD/ILS observation is dated 2026-11-23 and the required publication date is 2026-11-24
- **WHEN** a client reads USD/ILS
- **THEN** the response status is 200
- **AND** `eligibility.status` is "NOT_ELIGIBLE", `eligibility.reason` is "PUBLICATION_OVERDUE" and `eligibility.requiredPublicationDate` is "2026-11-24"
- **AND** `publicationDate` is "2026-11-23"

### Requirement: Decimal values encoded as strings
The system SHALL encode `rate` and the source rate as JSON strings containing the exact decimal value. It MUST NOT encode them as JSON numbers.

#### Scenario: Rate is a string
- **GIVEN** the stored normalised USD/ILS rate is exactly 3.343
- **WHEN** a client reads USD/ILS
- **THEN** the JSON value of `rate` is the string "3.343"

### Requirement: Timestamps and dates format
The system SHALL encode instants as ISO 8601 in UTC with a `Z` suffix and calendar dates as ISO 8601 `YYYY-MM-DD`.

#### Scenario: Formats
- **WHEN** a client reads any pair successfully
- **THEN** `retrievedAt` and `eligibility.evaluatedAt` end with "Z"
- **AND** `publicationDate` matches `YYYY-MM-DD`

### Requirement: Error responses use problem details
Every non-2xx response SHALL use media type `application/problem+json`. It SHALL include `type`, `title`, `status`, `detail` and a machine-readable `code`.

#### Scenario: Error body shape
- **WHEN** any request results in an error status
- **THEN** the response content type is `application/problem+json` and the body contains `status` and `code`

### Requirement: Invalid request parameters
The system SHALL respond 400 with code `INVALID_REQUEST` when `base` or `quote` is missing or is not three alphabetic characters.

#### Scenario: Missing quote
- **WHEN** a client requests `GET /api/v1/exchange-rates?base=USD`
- **THEN** the response status is 400 with code `INVALID_REQUEST`

#### Scenario: Malformed code
- **WHEN** a client requests `GET /api/v1/exchange-rates?base=US1&quote=ILS`
- **THEN** the response status is 400 with code `INVALID_REQUEST`

### Requirement: Unsupported or disabled pair
The system SHALL respond 422 with code `UNSUPPORTED_CURRENCY_PAIR` when the requested pair is well-formed but not configured or not enabled.

#### Scenario: Inverse pair not supported
- **WHEN** a client requests `base=ILS&quote=USD`
- **THEN** the response status is 422 with code `UNSUPPORTED_CURRENCY_PAIR`

#### Scenario: Disabled pair
- **GIVEN** the GBP/ILS pair is configured but disabled
- **WHEN** a client requests `base=GBP&quote=ILS`
- **THEN** the response status is 422 with code `UNSUPPORTED_CURRENCY_PAIR`

### Requirement: No observation yet
The system SHALL respond 404 with code `RATE_NOT_FOUND` when the pair is enabled but no observation has ever been stored for it.

#### Scenario: Fresh installation before first ingestion
- **GIVEN** EUR/ILS is enabled and no observations exist
- **WHEN** a client reads EUR/ILS
- **THEN** the response status is 404 with code `RATE_NOT_FOUND`

### Requirement: Policy not evaluable
The system SHALL respond 503 with code `POLICY_NOT_EVALUABLE` when the freshness policy cannot be evaluated. In that case it MUST NOT return a rate.

#### Scenario: Calendar not loaded for current year
- **GIVEN** the evaluation instant falls in a year with no loaded non-publication calendar
- **WHEN** a client reads USD/ILS
- **THEN** the response status is 503 with code `POLICY_NOT_EVALUABLE`
- **AND** the body contains no `rate`

### Requirement: Storage unavailable
The system SHALL respond 503 with code `STORAGE_UNAVAILABLE` when it cannot read the data it needs from storage. It MUST NOT fall back to unverified data or to a direct BoI call.

#### Scenario: Database unreachable
- **WHEN** the database cannot be reached during a read
- **THEN** the response status is 503 with code `STORAGE_UNAVAILABLE`

### Requirement: Reads do not depend on BoI availability
Serving a read SHALL NOT require contacting BoI. Its result SHALL depend only on stored observations, configuration and the current time.

#### Scenario: BoI down, eligible rate stored
- **GIVEN** BoI is unreachable and the current USD/ILS observation is assessed ELIGIBLE
- **WHEN** a client reads USD/ILS
- **THEN** the response status is 200 with `eligibility.status` "ELIGIBLE"

### Requirement: Machine-readable API description
The system SHALL publish an OpenAPI description of the endpoint. In it, `eligibility` and `eligibility.status` SHALL be marked as required in the 200 response schema.

#### Scenario: OpenAPI marks eligibility required
- **WHEN** a client fetches the OpenAPI document
- **THEN** the 200 response schema for `GET /api/v1/exchange-rates` lists `eligibility` as required
