# Tasks

## 1. Dependencies and configuration (D5)

- [x] 1.1 Add `PyJWT` and `python-multipart` to `pyproject.toml` runtime dependencies and reinstall the venv. Verify: `pip install -e ".[dev]"` succeeds and `python -c "import jwt, multipart"` runs.
- [x] 1.2 Add `jwt_secret` (`SecretStr | None`), `jwt_ttl_seconds` (3600, ≥ 60), `demo_username` ("demo") and `demo_password` (`SecretStr | None`) to `settings.py`, and document them in `.env.example` as demo-only. Make `create_app` raise a `RuntimeError` naming the setting when the secret is missing or under 32 characters, or the demo password is missing. Verify: unit tests in `tests/unit/test_settings_and_clock.py` cover the defaults, and new app-factory tests cover the missing, short and valid secret cases; `python -m rates.refresh` still starts without these settings.
- [x] 1.3 Make the contract-test client factory pass a valid secret and demo password, and send a valid bearer token by default (with an opt-out for the auth tests). Verify: the existing contract suite passes unchanged once 2.x lands.

## 2. Authentication (D1, D2, D3)

- [x] 2.1 Implement `api/auth.py`: token creation (`sub`, `iat`, `exp`, `iss`/`aud`, `jti`, HS256) and verification with expiry against the injected clock, plus the `current_user` security dependency (`auto_error=False`) that sets `request.state.principal`. Add `AuthenticationRequired` / `InvalidToken` / `InvalidCredentials` errors and their 401 problem handlers with `WWW-Authenticate: Bearer`. Verify: unit tests cover a valid token, expiry at exactly `iat + ttl` + 1 s, a wrong secret, a wrong audience, `alg: none` and a missing `sub`.
- [x] 2.2 Add `POST /api/v1/auth/token` (`OAuth2PasswordRequestForm`, both credentials compared with `hmac.compare_digest`) and log `auth.token_issued` / `auth.login_failed` without secrets. Verify: contract tests cover a valid login (200, `token_type` bearer, `expires_in` 3600, `sub` demo), a wrong password, an unknown user (both 401 `INVALID_CREDENTIALS`) and a missing password (400 `INVALID_REQUEST`), and check with `caplog` that the password never appears in the logs.
- [x] 2.3 Protect `GET /api/v1/exchange-rates` with `current_user`. Verify: contract tests check no token → 401 `AUTHENTICATION_REQUIRED` with a `WWW-Authenticate` header and no `rate`; an expired or tampered token → 401 `INVALID_TOKEN`; no token with `base=US1` → 401 (not 400); and `/health/ready`, `/openapi.json` and `/docs` → 200 without a token.
- [x] 2.4 Make the OpenAPI description show the security scheme and document 401 on the rates endpoint. Verify: a contract test finds the OAuth2 password flow with token URL `/api/v1/auth/token`, a security requirement on the rates endpoint, and documented 401 and 503 `AUDIT_UNAVAILABLE` responses.

## 3. Rate-query audit (D4)

- [x] 3.1 Implement `services/query_audit.py` (`QueryAuditService.record`) writing `RATE_QUERIED` events with the request (raw values, truncated to 64 characters) and the response subset taken from `ExchangeRateOut`, in its own transaction. Verify: integration tests store and read back a success record (entity `rate_observation` / rateId) and a rejection record (entity `rate_query`, `rateId` null).
- [x] 3.2 Wire the success path: the route records the audit before returning, and an audit failure raises `AuditUnavailable` → 503 `AUDIT_UNAVAILABLE` (no rate, not re-audited, logged at ERROR). Verify: contract tests check that a 200 read leaves exactly one record whose `rateId`, rate, `publicationDate` and eligibility equal the response body, that `base=usd&quote=ils` is stored as sent, and that with the audit repository patched to fail the read returns 503 `AUDIT_UNAVAILABLE` without `rate`.
- [x] 3.3 Wire the error paths: the route-level audit flag, and audit calls in the 400 / 422 / 404 / 503 / 500 problem handlers when a principal is present, logging audit failures without changing the response. Verify: contract tests check one record each for `base=ILS&quote=USD` (422), `base=USD` only (400, `quote` null), a fresh install (404) and the 2028 date (503 `POLICY_NOT_EVALUABLE`); that a 401 writes no record; that a failing audit on a 422 still returns 422 and logs an ERROR; and that token-endpoint errors write no record.

## 4. Operations and documentation

- [x] 4.1 Add the demo `JWT_SECRET` (48 characters, labelled demo-only) and `DEMO_PASSWORD` to the `api` service in `docker-compose.yml`. Verify: `docker compose up --build -d` brings the API to healthy, and starting the API without `JWT_SECRET` exits with the named error.
- [x] 4.2 Update README:
  - §2: the quick start gets a token first, using `curl -d username=demo -d password=...` then `-H "Authorization: Bearer ..."`.
  - §3 and §5: the architecture diagram and D6 change from "API read-only" to "API writes audit rows only", and the auth and audit decisions are added.
  - §6: the Security bullet is replaced with the demo-only auth and its limits.
  - §7: how to query the audit trail.
  - §8: audit retention and production identity are added as extensions.

  Verify: every README command runs as written against the Compose stack.

## 5. Integration checks

- [x] 5.1 Run the full suite on SQLite and on Postgres (`TEST_DATABASE_URL=...@localhost:5433/rates_test`). Verify: both are green.
- [x] 5.2 Compose smoke:
  - Get a token.
  - Read USD/ILS with it; read without it (401); read `ILS/USD` with it (422).
  - Then `SELECT` the `RATE_QUERIED` rows from Postgres and confirm two records with actor `demo` (the 200 with its `rateId`, the 422 with `rateId` null) and none for the 401.

  Verify: the captured output is added to the README smoke-run section.
- [x] 5.3 Run `openspec validate add-demo-jwt-auth --strict`. Verify: it reports the change as valid.

## Workflow follow-up

- Sync the delta specs and archive the change once the tasks are done.
