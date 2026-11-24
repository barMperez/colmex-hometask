# Exchange Rates API — Architecture Brainstorm

**Status:** Design exploration / pre-implementation  
**Context:** Take-home assignment for an API that retrieves the latest Bank of Israel (BoI) foreign-exchange representative rates. The API will be used by a **financially critical process**. At minimum, USD, EUR, and GBP must be supported. Implementation and technology choices are left to the engineer; source code and a runnable README must be published on GitHub.

> **Design principle:** Prefer financial correctness and explicit failure over silently using an unsuitable rate. At the same time, make the last officially published rate and its publication date visible, so a consumer can distinguish an older published rate from an erroneous one.

## 1. Scope and assumptions

### In scope

- Expose BoI **representative** FX rates, initially for **USD/ILS, EUR/ILS, GBP/ILS**.
- Identify each pair's direction explicitly with `baseCurrency` and `quoteCurrency`.
- Retrieve rates from BoI, validate them, and make a verified rate available through an API.
- Return the rate's source, applicable/publication date, and retrieval metadata.
- Apply a configurable **freshness / usability policy**; use bounded staleness and **fail closed** if no rate is eligible for the financially critical use case.
- Persist supported currency pairs, including whether each is enabled.
- Maintain rate history and an audit trail of important changes, particularly runtime policy changes.
- Document operating assumptions, failure behavior, test strategy, and trade-offs in the repository README.

### Out of scope for the initial submission

- Executing trades or processing payments.
- Rate locking, customer confirmation, quote expiration, and atomic transaction execution. These belong to an upstream trading service.
- Live, executable FX market quotes: BoI representative rates are **not** guaranteed trade execution prices.
- Inverse pairs (such as ILS/USD) or cross pairs (such as USD/EUR) in the initial MVP; these are potential extensions.
- A full management UI, microservices platform, distributed message broker, Kubernetes deployment, or other infrastructure without a clear requirement.

### Important distinction

A rate can be **authentic and correctly recorded for its publication date** while still being **too old or unsuitable for a particular financial operation**. The API must not conflate data validity, freshness, source reachability, and trade-price suitability.

## 2. Decisions made so far

| Area | Current direction | Rationale / caveat |
|---|---|---|
| Business priority | **Correctness before availability** | Financial operations should not silently proceed on an ineligible rate. |
| Failure strategy | **Bounded staleness + fail closed** | A previously verified rate can be returned only while it satisfies policy. Otherwise respond with an explicit error. |
| Days without publication | The **last officially published** rate can be returned with its actual date | Do not pretend it was published today. Whether it is eligible for a financial operation remains policy-dependent. |
| Policy | Runtime-changeable, versioned, auditable | Changing staleness thresholds changes financial risk. Do not silently use a stale/unknown policy version. |
| HTTP API | **GET** with `base` and `quote` query parameters | Simple, read-only retrieval, conventional HTTP semantics. |
| Supported pairs | **Database-backed** | Enables later control over allowed/enabled pairs without redeployment. |
| Currency scope | USD/ILS, EUR/ILS, GBP/ILS initially | Explicitly scoped to published rates against ILS. |
| Rate metadata | Source, date, retrieval time, rate identity, possibly policy version/status | Transparency, provenance, traceability. |
| Audit | Track important state and configuration changes | Reconstruction, investigation, and safe corrective action. |
| Cache vs database for rates | **Open** | Discussed but deliberately deferred. Database persistence is attractive; an in-memory cache should be justified separately. |
| Refresh architecture | **Background refresh is the current preference**, not finalized | Avoid synchronously depending on BoI for every customer request. Implementation/scheduling still open. |
| Trading workflow | **Out of scope** | The consumer owns confirmation and execution semantics. |

## 3. External source: what the API can and cannot promise

The Bank of Israel publishes **representative** exchange rates on its publication schedule, rather than a continuously changing real-time trading quote. There are non-publication days, and publication timing is not a hard guarantee. A last published rate remains a real historical observation; it is **not automatically an acceptable trading price** merely because no newer BoI rate has appeared.

**Consequence:** Avoid presenting the returned rate as a live executable market price. The downstream system must know that it is a dated BoI representative rate.

**Working assumption:** BoI is the authoritative external **source of representative rates**; the application stores the most recently **verified and ingested** source observations. It cannot claim a cached record is the newest available on BoI unless it has evidence of that fact.

**Unit normalization:** The source may quote some currencies per a denomination other than one unit (for example JPY per 100 units, if later supported). Normalize to a well-defined “1 base currency unit in quote currency units” representation, or preserve/document the source denomination; never silently treat all source quotes as per-unit.

## 4. API contract (proposed)

### Read one pair

```http
GET /api/v1/exchange-rates?base=USD&quote=ILS
```

Illustrative response (rate and timestamps are **fictional**):

```json
{
  "rateId": "boi-usd-ils-2026-11-20",
  "baseCurrency": "USD",
  "quoteCurrency": "ILS",
  "rate": "3.7250",
  "effectiveDate": "2026-11-20",
  "retrievedAt": "2026-11-20T14:30:00Z",
  "source": "BANK_OF_ISRAEL",
  "rateType": "REPRESENTATIVE",
  "status": "VALID",
  "policyVersion": 3
}
```

Contract notes:

- The `rate` is encoded as a **decimal string**, avoiding accidental binary floating-point handling in consumers.
- `baseCurrency=USD`, `quoteCurrency=ILS` means **1 USD = rate ILS** after source-unit normalization.
- Define whether `effectiveDate` means source publication date or source validity date; use a consistent name based on the actual BoI data semantics.
- `retrievedAt` is our ingestion timestamp, **not** the publication date.
- `status: VALID` (if exposed) means **eligible under our configured policy**, not “guaranteed live market price.” A less ambiguous name such as `usabilityStatus` may be preferable.
- `rateId` identifies the specific persisted source observation; do not let its format be the only guarantee of uniqueness.
- `policyVersion` identifies the policy used to assess eligibility, **if** that assessment is returned with the rate.
- Avoid claiming `isLatestPublished: true` without proof that the upstream source has not published a newer rate.

### Failure semantics

| Condition | Proposed response | Reason |
|---|---|---|
| Supported pair and policy-eligible verified rate | `200 OK` | Success, with actual source date. |
| Unsupported or disabled currency pair | `422 Unprocessable Content` with `UNSUPPORTED_CURRENCY_PAIR` | Request is not supported, not a source outage. |
| Missing/invalid required query parameters | `400 Bad Request` | Syntactically malformed request. |
| Supported pair but no policy-eligible rate | `503 Service Unavailable` with `RATE_UNAVAILABLE` or `RATE_STALE` | Do not silently supply an unsuitable rate. |
| Internal database or policy service unavailable | Typically `503` (or `500` for internal defects) | Fail safely; preserve actionable error classification and logs. |

**Open contract question:** If an older authenticated rate is no longer eligible, should a single endpoint return `503`, or should there be a separate informational endpoint that returns the last published rate explicitly marked `NOT_ELIGIBLE`? We agreed on transparency about the rate date, but have **not** finalized a dual-endpoint design. For the critical eligibility-enforcing endpoint, do not return `200` for an ineligible rate.

### Why GET rather than POST?

We considered POST because a JSON request body is convenient for future parameters and multiple currency pairs. We chose GET for the current **read-only single-pair lookup** because it has clearer semantics and standard caching behavior; additional query parameters can be introduced later. If batch or complex queries are needed, a separate POST `/resolve` operation can be considered without changing the initial contract.

## 5. Freshness, non-publication days, and fail-closed behavior

### Separate four dimensions

1. **Authenticity / validation** — did the rate come from the expected source, pass schema/content checks, and get saved intact?
2. **Source availability** — can our refresh process reach BoI now?
3. **Freshness / policy eligibility** — does the verified rate meet the configured rules for this use?
4. **Suitability for execution** — is a BoI representative rate an acceptable reference for the consuming business operation? The API itself cannot guarantee a real-time executable price.

Source unavailability alone does **not** mean the last verified rate is invalid. Conversely, successful HTTP connectivity alone does **not** prove the saved rate is eligible.

### Proposed configurable policy inputs

- Publication calendar / known non-publication days and holidays, with explicit timezone handling.
- Expected publication window and a configurable grace period, if business rules allow it.
- Maximum acceptable staleness, preferably based on expected publication events and/or an explicit duration bound.
- Whether previously published rates can be considered eligible on non-publication days.
- Fail-closed behavior when policy cannot be evaluated or a required configuration version is unknown.

**Important:** We discussed a two-hour grace period as an **example**, **not** a BoI rule or recommendation. Likewise, a rule such as “fail after three missed publication days” is illustrative and **not agreed**. The exact thresholds and behavior on weekends/holidays remain to be specified and documented as assumptions.

### Why an old rate is risky

FX markets may move substantially while no new BoI representative rate has been published. The user raised an example of USD/ILS moving materially within 24 hours. Therefore, “last published” must not be equated with “safe at any age.” We agreed to return its **real publication date** when returning it; eligibility still needs a policy decision.

### Decision table

| BoI reachable? | Verified stored rate meets policy? | Critical read behavior |
|---|---|---|
| Yes | Yes | `200`, dated rate |
| No | Yes | `200`, dated rate; source outage can be observable separately |
| Yes | No | `503`, until an eligible rate is available |
| No | No | `503`, no silent stale fallback |

### Unresolved policy edge cases

- Is it permissible for a critical consumer to use Friday's representative rate on Saturday/Sunday? The latest direction allows **returning** it with its real date; eligibility must be explicit rather than assumed.
- How long after an expected publication event can the previous rate remain eligible?
- What happens when an official publication is unusually delayed or missing?
- How do policy changes apply to in-flight reads across multiple application instances?
- Should the API differentiate an informational “last published” read from an eligibility-enforced financial read?

## 6. Persistence, supported pairs, and cache trade-offs

### Supported currency pairs — database decision

Proposed conceptual table:

| base_currency | quote_currency | enabled | calculation_type |
|---|---|---|---|
| USD | ILS | true | DIRECT |
| EUR | ILS | true | DIRECT |
| GBP | ILS | true | DIRECT |
| ILS | USD | false | INVERSE |
| USD | EUR | false | CROSS |

- Enforce a uniqueness constraint on `(base_currency, quote_currency)`.
- Validate a pair before querying its rate.
- Audit enabling/disabling a pair.
- Start with a small database query; cache supported pairs only if necessary. A cache introduces cross-instance invalidation/version coordination.

### Rates — decision deferred

**Option A: In-memory cache only**

- Pros: fast, simple, no DB lookup.
- Cons: disappears on restart; instances can disagree; loss of history; cold-start availability depends on BoI; TTL is not a freshness policy.

**Option B: Persist verified rates and history, optionally add in-memory cache**

- Pros: restart recovery, provenance, audit/reconstruction, shared state across replicas.
- Cons: additional persistence, availability, schema, and consistency concerns; cache invalidation if memory caching is added.

**Current leaning:** Use a persistent database for verified rate history and the currently usable observation, then add in-memory caching **only when measured load/latency justifies it**. This is a design leaning, **not yet a final decision**. For three currencies with unspecified RPS, DB reads may already be sufficient.

### Why a refresh worker does not replace caching

These solve different problems:

- **Background refresh:** decides **when** to check BoI for newly published rates.
- **Cache:** decides **where** a recent result is read from efficiently.
- **Persistent storage:** provides durable verified observations and historical traceability.

A cache TTL must not itself decide whether a rate is financially eligible.

## 7. Refresh approaches and resilience

### Option A — refresh on demand

Read the saved rate; if unavailable or expired, call BoI on the request path.

- Pros: simple, avoids scheduled work.
- Cons: variable latency, synchronous coupling to the external source, possible thundering herd and failures during BoI outages.

### Option B — background refresh (preferred, not yet finalized)

A scheduled process fetches from BoI, validates and atomically persists new verified rates. Client reads are served from local durable state and checked against policy.

- Pros: predictable read latency; external failures do not immediately break reads if a policy-eligible stored rate exists; controlled upstream request volume.
- Cons: scheduling, retry strategy, safe concurrency among replicas, monitoring, and missed refresh handling.

A worker does not need to be a separate microservice: an embedded scheduler with a robust singleton mechanism or an externally managed scheduled job are options. Ensure a multi-instance deployment does **not** independently start conflicting refresh tasks.

### Resilience behavior to design and test

- Network timeout, DNS/TLS failure, non-2xx response, malformed payload.
- Schema mismatch, missing currencies, zero/negative/impossible rates, unexpected quote denominations.
- Retries with bounded exponential backoff and jitter; avoid retry storms.
- Optional circuit breaker if failure patterns justify it; don't add it by rote.
- Atomic write of the validated refresh set (or explicitly reasoned per-pair writes) so a partially ingested publication does not masquerade as a full one.
- Idempotent ingestion and protection against out-of-order/duplicate publication observations.
- Preserve the last verified observation on failed refresh; do not reset freshness timestamps without receiving and validating new source data.
- Alert on missed expected updates, refresh failures, and policy-ineligible rates.

## 8. Data model (conceptual, not finalized)

**`supported_currency_pairs`**

- `base_currency`, `quote_currency` (composite unique key)
- `enabled`, `calculation_type` (`DIRECT` initially)
- `created_at`, `updated_at`

**`exchange_rate_observations`** (append-only or immutable after verification)

- `rate_id` (stable identifier)
- `base_currency`, `quote_currency`
- `rate` (`NUMERIC` / decimal, never binary floating point)
- `source`, `source_publication_date` (or accurately named effective date)
- `retrieved_at`, optional `verified_at`, source denomination / raw payload reference
- Uniqueness/idempotency key based on source identity and publication/version semantics

**`rate_usability_policies`** (versioned)

- `policy_id`, `version`, `active` / effective timestamp
- Publication-calendar rules and configurable staleness/grace thresholds
- Non-publication-day behavior, author, reason, timestamps

**`audit_events`** (append-only)

- `event_id`, `event_type`, `entity_type`, `entity_id`
- `actor`, `occurred_at`, `before`, `after`, `policy_version`, correlation ID, reason

**Open:** Full schema, indexes, whether policy settings and audit records live in the same DB, and whether source raw responses are retained.

## 9. Audit trail and observability

### Audit trail

Audit important mutations:

- Policy creation/activation/update; old/new values, actor, timestamp, reason, and version.
- Supported-pair activation/deactivation.
- Successful ingestion/correction of source observations and any manual operational override (if overrides are supported).

Prefer append-only audit records. Write the mutation and its audit event **in one database transaction** when feasible. Audit history helps reconstruct actions; it does **not** automatically make rollback safe. Corrective changes should create a new version rather than rewrite history.

### Operational observability

- Structured logs with correlation IDs, no secrets or unnecessary sensitive data.
- Metrics: source fetch success/failure, duration, last successful ingestion, age of each source observation, eligible/ineligible reads, DB failures.
- Health/readiness checks that distinguish process health from source reachability and data eligibility.
- Alerts for overdue publications/refreshes and repeated invalid responses.
- Normal GET requests belong primarily in access logs/metrics, **not** necessarily as individual business audit events.

## 10. Security considerations

No explicit security requirements were provided. Proposed proportional defaults:

- Validate and normalize input currency codes, restrict supported pairs, and bound query sizes.
- Restrict and authenticate administrative policy/pair mutations; authorization should be role-based if an admin capability is exposed.
- Assume internal API use unless otherwise specified; document deployment/network boundary and authentication assumptions.
- Use HTTPS/TLS for external calls and deployed API, least-privilege DB credentials, secret management, and audit logging for privileged changes.
- Consider abuse controls/rate limiting if the service is externally accessible; avoid building an elaborate permission system without a use case.
- Ensure real-time changes to policy cannot bypass versioning, auditing, or validation.

## 11. Trading consistency — consciously excluded

We discussed a trade starting with one rate, then a newer BoI rate arriving before execution. The consumer must not silently execute at a different rate than the customer accepted. In a real trading system, a **quote/confirmation service** would bind the rate (and amount, terms, expiry) to user consent and possibly require reconfirmation.

**Decision:** This workflow is **out of scope**. The Exchange Rates API returns a traceable, dated representative rate; the trading service is responsible for customer agreement and transaction semantics. The `rateId` makes that integration possible without implementing it here.

## 12. Trade-off summary

| Choice | Benefit | Cost / risk | Current position |
|---|---|---|---|
| Correctness over unconditional availability | Avoids silent financial mistakes | More explicit `503` responses | **Chosen** |
| Bounded staleness instead of always using latest stored rate | Controls accepted age | Policy complexity / possible false rejections | **Chosen**, exact thresholds open |
| Return actual publication date | Honest provenance, consumer transparency | Consumer still needs eligibility semantics | **Chosen** |
| Dynamic policy with audit/versioning | Respond to risk and production changes | Governance, consistency, failure handling | **Chosen** |
| GET instead of POST | Standard retrieval and HTTP semantics | Less convenient for complex batch inputs | **Chosen** for single pair |
| DB-backed pair configuration | Operational control | Additional dependency; optional cache coordination | **Chosen** |
| Persisted history over memory-only state | Recovery, traceability | Storage and DB operations | **Leaning**, to finalize |
| Background refresh vs on-demand external fetch | Stable read latency, decoupling | Scheduler reliability and concurrency | **Preferred**, not finalized |
| ILS direct pairs vs general cross-rate engine | Small, reliable scope | Less functionality | **Chosen** for MVP |
| Exclude quote/trade execution | Focus on actual assignment | Downstream must handle confirmation | **Chosen** |

## 13. Open decisions before implementation

1. **Exact freshness policy:** What publication calendar and timezone are used? Are older rates usable on non-publication days? What are the grace period and absolute cutoff? How will exceptional publication delays be handled?
2. **API semantics for old observations:** One strict, eligibility-enforcing endpoint, or an additional informational endpoint for “last published but not currently usable”?
3. **Runtime policy management:** What is the smallest implementable change/activation workflow that still provides versioning, access control, and audit? How is policy propagated across instances?
4. **Rate persistence and caching:** Finalize the storage architecture and whether cache is needed at all; keep rate cache and supported-pair cache decisions distinct.
5. **Refresh scheduling:** Embedded scheduler, external scheduled job, or dedicated worker? How is singleton execution enforced in multi-instance deployment?
6. **Failure contracts:** Exact machine-readable error schema, readiness semantics, and operational alerts.
7. **Technology stack and deployment:** Framework, DB migration approach, local startup, containerization, CI checks.
8. **Quantified nonfunctional targets:** No RPS, latency SLO, or uptime SLO was supplied; choose modest, explicit assumptions rather than inventing requirements.

## 14. Suggested tests

- USD/EUR/GBP direct pair reads with correct direction and decimal precision.
- Invalid/missing codes, unsupported pair, disabled pair.
- Non-publication day with last verified rate and correct original publication date.
- Boundary tests around publication window, grace period, and maximum permitted staleness.
- BoI outage with eligible rate → successful dated response; BoI outage with ineligible/no rate → explicit `503`.
- Malformed source data, missing one currency, negative/zero values, source unit normalization.
- Duplicate or out-of-order refreshes; idempotent and atomic updates.
- Restart recovery; optionally multiple instances with consistent configuration and no duplicate refresh.
- Policy update with version change and audit record; failure to load policy must not silently relax validation.
- DB unavailability, timeout/retry limits, and no accidental fallback to unverifiable data.
- If caching is introduced, verify invalidation and that cache TTL never bypasses the freshness policy.

## 15. README / delivery checklist

- One-paragraph problem definition and explicit business assumptions.
- Running instructions (`docker compose up` or equivalent), configuration example, migrations and seeding.
- OpenAPI or equivalent endpoint documentation and example success/error responses.
- Source reference and data normalization rules.
- Architecture diagram and decision/trade-off record.
- Freshness policy definition **including its assumptions and non-BoI-specific thresholds**.
- How to run tests, simulate BoI failures, and observe service health.
- Operational limitations, known risks, and what was deliberately left out of scope.
- A concise explanation of where AI-assisted development was used, while ensuring the developer understands and can defend every design and code decision.

---

**Bottom line:** Build a narrow, well-tested representative-rate API with explicit pair direction, verified historical observations, configurable eligibility rules, honest publication metadata, auditable policy changes, and safe failure modes. Prioritize a clear correctness contract over speculative scale or infrastructure complexity.
