# Spec Delta

## ADDED Requirements

### Requirement: Authenticated access
`GET /api/v1/exchange-rates` SHALL require a valid bearer token as defined by the api-authentication capability. Every other requirement of this capability describes the behavior for a request that carries a valid token.

#### Scenario: Read without a token
- **WHEN** a client requests `GET /api/v1/exchange-rates?base=USD&quote=ILS` without a token
- **THEN** the response status is 401 with code `AUTHENTICATION_REQUIRED` and the body contains no `rate`

#### Scenario: Read with a valid token
- **GIVEN** USD/ILS is enabled, a current observation exists and the client holds a valid token
- **WHEN** the client requests `GET /api/v1/exchange-rates?base=USD&quote=ILS` with `Authorization: Bearer <token>`
- **THEN** the response status is 200

### Requirement: Audit unavailable
The system SHALL respond 503 with code `AUDIT_UNAVAILABLE`, and no rate, when the audit record for a rate it would return cannot be stored.

#### Scenario: Audit write fails
- **GIVEN** a rate is available and the audit record cannot be written
- **WHEN** an authenticated client reads USD/ILS
- **THEN** the response status is 503 with code `AUDIT_UNAVAILABLE` and media type `application/problem+json`

## MODIFIED Requirements

### Requirement: Machine-readable API description
The system SHALL publish an OpenAPI description of the endpoint. In it, `eligibility` and `eligibility.status` SHALL be marked as required in the 200 response schema. The endpoint SHALL be marked as requiring the bearer security scheme, and its 401 and 503 `AUDIT_UNAVAILABLE` responses SHALL be documented.

#### Scenario: OpenAPI marks eligibility required
- **WHEN** a client fetches the OpenAPI document
- **THEN** the 200 response schema for `GET /api/v1/exchange-rates` lists `eligibility` as required

#### Scenario: OpenAPI documents authentication errors
- **WHEN** a client fetches the OpenAPI document
- **THEN** `GET /api/v1/exchange-rates` documents a 401 response and a security requirement
