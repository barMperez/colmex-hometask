# Design

## Context

See proposal.md (Why). Contracts are in this change's `specs/`. Relevant current state:

- **Routing and errors.** `api/routes.py` has one rates route whose dependencies are the query parameters and `RateQueryService`. Every error becomes a problem response in `api/problems.py`, one handler per domain error plus `RequestValidationError` (400), `StarletteHTTPException` and a catch-all 500.
- **Audit storage.** `audit_events` already exists (`actor`, `event_type`, `entity_type`, `entity_id` up to 200 characters, JSON `after`) and is written through `SqlAuditRepository`.
- **The API never wrote to the database before.** `RateQueryService` uses read-only connections.
- **Clock.** The app takes an injected `Clock` (`FixedClock` in tests).
- **Verified on the installed FastAPI (0.165) with a throwaway probe app:**
  - a dependency that raises wins over query-parameter validation errors;
  - state set on `request` by that dependency is visible in the `RequestValidationError` handler;
  - `OAuth2PasswordBearer` adds the security requirement to OpenAPI.

## Goals / Non-Goals

**Goals:**
- Attribute every rate handed out to a user, together with exactly what that user was told.
- Make it impossible for a rate to leave the service without its audit record.

**Non-Goals:**
- **Production identity.** No user store, password hashing, refresh tokens, revocation, rate limiting or login auditing. One configured demo user is enough to demonstrate attribution.
- **Auditing other endpoints** (`/health/*`, docs, token issuance).
- **A separate audit table or database.** It reuses `audit_events`.

## Decisions

### D1. HS256 JWT via PyJWT, with expiry checked against the injected clock

- **Claims:** `sub` (username), `iat`, `exp`, `iss` = `aud` = `"rates-api"`, and a random `jti`.
- **Signing:** `JWT_SECRET` (`SecretStr`, at least 32 characters).
- **Verification:** `jwt.decode(..., algorithms=["HS256"], audience=..., issuer=..., options={"verify_exp": False, "require": ["exp", "iat", "sub"]})`, then `exp` is compared to `clock.now()`. Expiry therefore follows the same injected clock as everything else, and expiry tests are deterministic.
- **Algorithms:** pinned to HS256, so `alg: none` and algorithm-confusion tokens are rejected.

*Alternatives:*
- **RS256 key pair.** Rejected for the demo: a single service both issues and verifies tokens, so a shared secret is simpler and equally safe here.
- **Opaque tokens stored in the database.** Rejected: a database read per request just to authenticate, and revocation isn't needed.

### D2. A security dependency that sets the principal, and problem responses for every auth failure

`api/auth.py` provides:
- `OAuth2PasswordBearer(tokenUrl="/api/v1/auth/token", auto_error=False)`. With `auto_error=False`, FastAPI's own 401 doesn't bypass our problem format.
- A `current_user` dependency that:
  - raises `AuthenticationRequired` (no token) or `InvalidToken` (bad signature, wrong `iss`/`aud`, missing claims, expired);
  - on success, stores `request.state.principal = Principal(username)`.

New problem handlers return 401 with `WWW-Authenticate: Bearer` and log `auth.rejected reason= path=`. The token and password are never logged.

### D3. Token endpoint with a constant-time credential check

`POST /api/v1/auth/token` takes FastAPI's `OAuth2PasswordRequestForm` (which needs `python-multipart`).
- Username and password are both compared with `hmac.compare_digest`, and both comparisons always run. An unknown user and a wrong password give the same 401 `INVALID_CREDENTIALS`, so the response doesn't reveal which was wrong.
- Missing form fields go through the existing `RequestValidationError` handler and return 400 `INVALID_REQUEST`.
- Success is logged as `auth.token_issued user=`; failure as `auth.login_failed`, at WARNING.

### D4. The audit service and the two places that call it

`services/query_audit.py` provides `QueryAuditService.record(principal, raw_query, outcome)`. It writes one `RATE_QUERIED` event in its own short `engine.begin()` transaction, separate from the read:

- `actor`: the username.
- With a returned rate: `entity_type` = `"rate_observation"`, `entity_id` = `rateId`.
- Without one: `entity_type` = `"rate_query"`, `entity_id` = the event's own id.
- `after` holds the request and the response:

  ```
  {"request": {"base": <as sent or null>, "quote": <as sent or null>},
   "response": {"status": 200, "code": null, "rateId": ..., "rate": "3.343",
                "publicationDate": "2026-11-23", "eligibility": {...as returned...}}}
  ```

  The response part is `ExchangeRateOut.model_dump(mode="json", by_alias=True)`, restricted to those fields. It is literally what the caller received, not a recomputation. Raw query values are truncated to 64 characters.

Where it's called:
1. **The success path.** The route builds `ExchangeRateOut` and calls `record(...)`. Only if that commits does it return the body. If the write raises, it raises `AuditUnavailable`, whose handler returns 503 `AUDIT_UNAVAILABLE`. No rate is in that body, and the 503 itself isn't audited again; it's logged at ERROR.
2. **The error paths.** Every problem handler for the rates route — validation 400, 422, 404, both 503s and the catch-all 500 — first calls `record(...)` with its status and code, **if** `request.state.principal` is set. A failure there is logged at ERROR and the original error response is returned unchanged, because an error response carries no rate.

The route marks itself as audited by setting `request.state.audit_rate_query = True` in a route-level dependency that runs after `current_user`. The handlers check that flag, so token-endpoint and health errors are never audited as rate queries.

*Alternatives:*
- **Middleware that audits every response.** Rejected: middleware sees bytes, not the typed response, and it can't turn an already-started 200 into a 503 when the audit fails, which breaks the fail-closed rule.
- **Audit write in the same transaction as the read.** Rejected: the read uses a read-only connection, and there's nothing to keep atomic with it.

### D5. Configuration and startup check

New settings:
- `jwt_secret: SecretStr | None = None`
- `jwt_ttl_seconds = 3600` (at least 60)
- `demo_username = "demo"`
- `demo_password: SecretStr | None = None`

`create_app` raises a `RuntimeError` naming the setting when `jwt_secret` is missing or shorter than 32 characters, or when `demo_password` is missing. The API process exits at startup and Compose shows the error.

`docker-compose.yml` passes demo values to the `api` service: a 48-character demo secret and `DEMO_PASSWORD=demo-password`, both labelled demo-only. `.env.example` documents them. Tests build `Settings` with explicit values.

The refresh process doesn't need these settings. They're validated only in `create_app`, so the refresh service keeps starting without them.

## Risks / Trade-offs

- [The demo credential and secret are in the Compose file] → Labelled demo-only in Compose, `.env.example` and README §6. A real deployment would inject them from a secret store, and the startup check refuses to run without a secret.
- [Every read now writes an audit row] → One small insert per request. On SQLite, two API workers and the refresh process share a single writer; the existing 5-second `busy_timeout` absorbs it at demo load, and Postgres (the Compose default) has no such limit. The audit table grows by about one row per read. No retention policy is set: out of scope, and listed in README §8.
- [An audit outage blocks reads] → Intended (fail closed). In practice the audit write fails almost only when the database is down, and then the read fails with 503 `STORAGE_UNAVAILABLE` anyway.
- [A token is valid for an hour and can't be revoked] → Acceptable for one demo user. Changing `JWT_SECRET` invalidates every token at once.
- [Existing clients break: reads now return 401] → This is the intended breaking change. README §2 shows the token step first, and Swagger's "Authorize" button covers manual use.

## Migration Plan

1. Install the new dependencies (`PyJWT`, `python-multipart`) through `pyproject.toml`; the Compose image rebuild picks them up.
2. Set `JWT_SECRET` and `DEMO_PASSWORD` for the `api` service (Compose ships demo values). There is no database migration.
3. **Rollback:** redeploy the previous image. The `RATE_QUERIED` rows stay in `audit_events` and are ignored by the old code.
