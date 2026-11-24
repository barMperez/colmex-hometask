# Spec Delta

## Purpose

Identifies who is calling the rates API, so that every rate handed out can be attributed to a user. For the demo, this is a single configured user who exchanges a username and password for a short-lived bearer token.

## ADDED Requirements

### Requirement: Token issuance for the demo user
The system SHALL expose `POST /api/v1/auth/token` accepting an OAuth2 password form (`username`, `password`). For the configured demo user it SHALL return 200 with `access_token`, `token_type` "bearer" and `expires_in` in seconds. The token SHALL identify the user and expire after the configured lifetime.

#### Scenario: Valid credentials
- **GIVEN** the demo user is `demo` and the token lifetime is 3600 seconds
- **WHEN** a client posts `username=demo` and the correct password
- **THEN** the response status is 200 with `token_type` "bearer" and `expires_in` 3600
- **AND** the token identifies the user `demo`

#### Scenario: Wrong password
- **WHEN** a client posts `username=demo` with a wrong password
- **THEN** the response status is 401 with code `INVALID_CREDENTIALS` and no token

#### Scenario: Unknown user
- **WHEN** a client posts a username other than the demo user
- **THEN** the response status is 401 with code `INVALID_CREDENTIALS`

#### Scenario: Missing form fields
- **WHEN** a client posts without a `password` field
- **THEN** the response status is 400 with code `INVALID_REQUEST`

### Requirement: Bearer token validation
A protected endpoint SHALL accept a request only with an `Authorization: Bearer` token that the service issued, is unmodified and is not expired. It SHALL respond 401 with a `WWW-Authenticate: Bearer` header: code `AUTHENTICATION_REQUIRED` when no token is sent, `INVALID_TOKEN` otherwise. Authentication SHALL be checked before request parameters.

#### Scenario: No token
- **WHEN** a client calls a protected endpoint without an `Authorization` header
- **THEN** the response status is 401 with code `AUTHENTICATION_REQUIRED` and a `WWW-Authenticate: Bearer` header

#### Scenario: Expired token
- **GIVEN** a token issued at 10:00 with a lifetime of 3600 seconds
- **WHEN** it is used at 11:00:01
- **THEN** the response status is 401 with code `INVALID_TOKEN`

#### Scenario: Tampered or foreign token
- **WHEN** a client sends a token whose signature does not match the service's secret
- **THEN** the response status is 401 with code `INVALID_TOKEN`

#### Scenario: Authentication before validation
- **WHEN** a client without a token requests `GET /api/v1/exchange-rates?base=US1`
- **THEN** the response status is 401, not 400

### Requirement: Open operational endpoints
The health endpoints and the OpenAPI document and UI SHALL remain accessible without a token.

#### Scenario: Health without token
- **WHEN** a client calls `/health/ready` or `/openapi.json` without a token
- **THEN** the responses are the same as before authentication was added

### Requirement: Signing secret is mandatory
The API SHALL refuse to start unless a token-signing secret of at least 32 characters is configured. All API workers SHALL share it, so a token from one worker is accepted by every other.

#### Scenario: Missing secret
- **WHEN** the API is started without `JWT_SECRET`
- **THEN** it exits with an error naming the missing setting

#### Scenario: Short secret
- **WHEN** the API is started with a `JWT_SECRET` shorter than 32 characters
- **THEN** it exits with an error

### Requirement: Security scheme in the API description
The OpenAPI document SHALL declare the token endpoint and a bearer security scheme, and SHALL mark the rates endpoint as requiring it.

#### Scenario: OpenAPI declares bearer security
- **WHEN** a client fetches the OpenAPI document
- **THEN** it contains an OAuth2 password-flow security scheme with token URL `/api/v1/auth/token`
- **AND** `GET /api/v1/exchange-rates` lists that scheme in its security requirements
