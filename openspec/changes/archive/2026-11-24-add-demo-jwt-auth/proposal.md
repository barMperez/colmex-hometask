# Proposal

## Why

Rates feed customer deposits (ILS converted to USD before reaching the trading platform), so every rate the service hands out must be traceable to the caller who received it, together with what that caller was told. Today the read API is anonymous and reads leave no trace. For the demo, we add a minimal identity (one demo user with a JWT) and an audit record for every authenticated rate query.

## What Changes

- **Token endpoint.** `POST /api/v1/auth/token` (OAuth2 password form: `username`, `password`) returns `{access_token, token_type: "bearer", expires_in}` for the single demo user configured in the environment.
  - The token is an HS256 JWT, valid for 1 hour by default, so it outlives a deposit session.
  - Wrong credentials return 401 `INVALID_CREDENTIALS`.
- **BREAKING: bearer token required.** `GET /api/v1/exchange-rates` requires `Authorization: Bearer <token>`.
  - A missing token returns 401 `AUTHENTICATION_REQUIRED`.
  - A malformed, tampered or expired token returns 401 `INVALID_TOKEN`.
  - The 401 is returned before parameter validation. `/health/*` and the OpenAPI docs stay open.
  - Swagger UI gets an "Authorize" button.
- **Audit of every authenticated rate query** (event `RATE_QUERIED`). Each record holds:
  - the user;
  - `base` and `quote` exactly as sent;
  - the outcome: HTTP status and error code;
  - for a returned rate: `rateId`, the rate, `publicationDate` and the full eligibility verdict (status, reason, `requiredPublicationDate`, `policyVersion`, `evaluatedAt`).

  Rejected calls (400, 422, 404, 503) are audited too, with a null `rateId`. Unauthenticated calls (401) are logged only, since there's no user to attribute them to.
- **Fail closed on audit.** A rate is returned only after its audit record is committed. If that write fails, the response is 503 `AUDIT_UNAVAILABLE` with no rate in the body.
- **Configuration.** `JWT_SECRET` (required, at least 32 characters; the API refuses to start without it), `JWT_TTL_SECONDS` (3600), `DEMO_USERNAME` and `DEMO_PASSWORD`. Compose and `.env.example` ship clearly-labelled demo values.
- **Dependencies:** `PyJWT`, plus `python-multipart` for the OAuth2 form.

## Capabilities

### New Capabilities

- `api-authentication`: issuing tokens for the demo user and validating bearer tokens on protected endpoints, including the 401 contract and the OpenAPI security scheme.
- `rate-query-audit`: the audit trail of rate queries. What is recorded, for which calls, and the fail-closed rule for returning a rate.

### Modified Capabilities

- `exchange-rate-query`:
  - The read endpoint now requires authentication.
  - A new 503 `AUDIT_UNAVAILABLE` error is added.
  - The OpenAPI description declares the bearer security scheme.

## Impact

- **API:** consumers must obtain and send a token. `/health/*`, `/docs` and `/openapi.json` are unchanged.
- **Code:**
  - new `rates/api/auth.py` (token issuing and verification, FastAPI security dependency);
  - new `rates/services/query_audit.py`;
  - changes to `api/routes.py`, `api/problems.py`, `api/app.py` and `settings.py`.
- **Database:** no schema change. Rows go into the existing `audit_events` table. The API process now writes to the database (audit rows only; rate data is still written only by the refresh process).
- **Ops:** `docker-compose.yml` and `.env.example` gain the JWT and demo-user settings.
- **Docs:** README §2 (quick start with a token), §5 (decision D6 now "API writes audit rows only"), §6 (demo-only security) and §7.
- **Out of scope:** real user management, password hashing, refresh tokens, token revocation, rate limiting and auditing logins. The demo credential is plain configuration and is documented as demo-only.
