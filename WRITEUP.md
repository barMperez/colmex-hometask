# Write-up: how this solution was built

This document is for the reviewer. The [README](README.md) explains how to run the service and how it behaves; this file explains **how I got there**: what I took the problem to be, what I learned about the Bank of Israel (BoI) API before writing code, how the architecture came out of that, and which decisions changed along the way and why.

---

## TL;DR

- **The problem in one line:** a financially critical process needs BoI's representative USD/EUR/GBP rates, and it can be hurt in **two directions**. A silently stale rate is wrong money; an API that fails whenever BoI is slow or closed blocks money movement for no reason.
- **The core idea:** separate *"is this rate authentic?"* from *"is it acceptable to use right now?"*. The service stores every verified BoI observation and returns the latest one with **when BoI published it** and an explicit **eligibility verdict**. That verdict comes from BoI's real publication calendar, not from a TTL.
- **The shape:** two processes from one codebase.
  - A **refresh process** is the only writer of rate data. It polls BoI, validates each currency, and stores observations append-only and idempotently.
  - An **API** serves reads from the database only, so a BoI outage never reaches a reader.
- **Correctness rules:** exact decimals end to end; every instant timezone-aware; **fail closed** whenever the policy can't be evaluated; a rate leaves the API only after its audit record is committed.
- **How I worked:** spec-driven. Each change has a proposal, behaviour specs with testable scenarios, a design with the alternatives considered, and a task list (see `openspec/`). There were five changes in total. Each was verified against its specs and against the live BoI API in Docker.
- **Evidence:** 246 tests on SQLite, and the same suite on Postgres (244 passed, 2 SQLite-only skips). Live runs are captured in README §7, including simulated BoI outages, a database outage and a Postgres crash.

---

## 1. Reading the assignment

The assignment asks for an API that returns the latest BoI representative rates (at least USD, EUR and GBP) for a **financially critical process**, with free choice of technology, published on GitHub with a runnable README.

The phrase I anchored on was *financially critical*. It rules out the obvious design, "fetch BoI, cache for N minutes, return it". That design gives wrong answers in exactly the situations that matter:

| Situation | Naive cache + TTL | What a financial consumer needs |
|---|---|---|
| Monday 10:00 (no BoI rate since Friday) | TTL long expired → error, or "stale" | Friday's rate **is** the latest expected one: usable |
| Tuesday 16:30, BoI hasn't published today | TTL still valid → serves Monday as if fine | Today's rate is **overdue**: say so |
| BoI API down | Error on every read | Keep serving the verified rate while it's still acceptable |
| Holiday (e.g. Knesset elections, 27/10) | Looks stale | Previous rate is correct |

So the real problem isn't fetching a number. It's **deciding, at any instant, whether the newest rate we hold is the one a careful person would use**, and being explicit when it isn't.

Later the recruiter added the concrete use case: *a customer deposits ILS to buy an asset; it must be converted to USD before reaching the trading platform, and if the service fails the customer can't act and may lose money.* That sharpened the bar to: **never block when a usable rate exists; when blocking is right, keep it short and make sure someone knows.** Section 6 shows what that changed.

## 2. Brainstorm before code

Before any code I wrote down the design space in [`docs/brainstorm.md`](docs/brainstorm.md). The most useful outcome was separating **four questions that a naive design conflates:**

1. **Authenticity:** did this rate come from BoI and pass validation?
2. **Source availability:** can we reach BoI right now?
3. **Freshness / eligibility:** does the rate satisfy the policy *for use now*?
4. **Suitability:** is a BoI *representative* rate even the right price for this operation? (It isn't an executable trade price. That's the consumer's call, and the README says so.)

The options I weighed there, and where they landed:

| Question | Options | Decision |
|---|---|---|
| Where do reads come from? | Call BoI on demand · in-memory cache · persisted history | **Persisted, append-only history**: survives restarts, replicas agree, gives provenance and audit. No cache: three pairs, a few indexed queries. |
| When do we talk to BoI? | On the request path · background refresh | **Background refresh** in its own process: BoI latency and outages never touch reads. |
| Request shape | GET with query params · POST with body | **GET `?base=&quote=`**: read-only, cacheable semantics; batch can be a separate endpoint later. |
| Supported pairs | Hard-coded · database | **Database** (`currency_pairs.enabled`): operational control without a redeploy. |
| Staleness rule | Max age in hours · BoI calendar | Left open in the brainstorm; resolved by the API exploration (next section). |

I also deliberately listed what was **out of scope**: trade execution, rate locking and quote confirmation, inverse and cross pairs, an admin UI, and distributed infrastructure with no stated requirement. Writing that down early kept the solution narrow.

## 3. Exploring the BoI API

I studied the real source before designing the policy, because the freshness rule has to match how BoI actually behaves.

**Endpoint:** `GET https://boi.org.il/PublicApi/GetExchangeRates`, public with no key. A real entry:

```json
{"key":"USD","currentExchangeRate":3.343,"currentChange":0.511,"unit":1,"lastUpdate":"2026-11-23T13:21:10.7104898Z"}
```

What I found, and what each finding forced in the design:

| Observation | Consequence in the code |
|---|---|
| Rates are JSON **numbers** (`3.343`). A default JSON parser turns them into binary floats. | `response.json(parse_float=Decimal)`; domain models **reject floats**; storage is TEXT on SQLite and NUMERIC on Postgres; the API returns rates as **strings**. A test reads `3.343` back as exactly `Decimal("3.343")`. |
| Some currencies are quoted **per 10 or per 100 units** (`JPY unit: 100`, `LBP unit: 10`). | Store the source rate and unit as published, plus a normalised rate = 1 base unit in ILS. `2.1026 / 100 = 0.021026`, exactly. |
| There is **no publication-date field**. `lastUpdate` is a UTC timestamp with 7 fractional digits. | Publication date = `lastUpdate` converted to **Asia/Jerusalem**. `22:30Z` on the 23rd is the 24th in Israel, and a test pins that. |
| `lastUpdate` was the same on every entry in the snapshots observed. | My first version *required* that; the verification review showed it was an unjustified assumption (section 6). |
| BoI publishes once per business day, "**soon after 15:15**" (12:15 Fridays), and says "the time is not fixed". Observed publications landed at **15:21**. | A policy with publication times plus a **grace period** (45 min, an explicit assumption, versioned in the `policies` table). |
| BoI publishes a yearly **list of non-publication days**: Israeli holidays, "Holiday Abroad" days, Knesset elections. | The 2026–2027 list is seeded from BoI's PDF. If the calendar for a year we need isn't loaded, the service **fails closed** (503) instead of guessing. |
| BoI can **skip a business day** ("lack of data") with no notice. | Only a human can tell "BoI skipped" from "our ingestion is broken". So the rate goes NOT_ELIGIBLE, and there's an **audited operator script** to declare the day. |
| A representative rate "has no obligatory status under law". | The README is explicit: this is a reference-rate service, not a quote service. |

This exploration is what turned "freshness" from a TTL into a **calendar question**.

## 4. The freshness policy: the heart of the design

```
deadline(d)    = expected publish time on business day d + grace     (15:15 Mon–Thu, 12:15 Fri, in Israel time)
required(now)  = the latest business day whose deadline has passed
ELIGIBLE       = publicationDate >= required(now)
                 AND not in the future AND at most max_age_days old
```

One rule covers weekends, holidays, early Fridays, daylight-saving changes and "before today's deadline", with no special cases:

| Now (Israel) | Latest rate | Required | Verdict |
|---|---|---|---|
| Tue 10:00 | Mon | Mon | ELIGIBLE: today's deadline (16:00) hasn't passed |
| Tue 16:01 | Mon | Tue | NOT_ELIGIBLE, `PUBLICATION_OVERDUE` |
| Sun 09:00 | Fri | Fri | ELIGIBLE: the weekend needs no rule |
| 27/10 18:00 (elections) | 26/10 | 26/10 | ELIGIBLE: listed holiday |
| Pesach, 7 days later | 31/03 | 31/03 | ELIGIBLE: still within the 7-day cap |

Design properties I cared about:

- **`evaluate()` is a pure function** of (observation, now, calendar, policy) with an injected clock. All of the financial logic is a table-driven unit test, and every spec scenario is a row in it.
- **Fail closed, with a precise reason.** A missing calendar year, no active policy, or a calendar so wrong that no business day exists within the cap all return 503 `POLICY_NOT_EVALUABLE`, never a guess.
- **Versioned and auditable.** The policy is a database row with a version, and every verdict says which version produced it.

## 5. Architecture

```
            +------------- API process (N workers; writes audit rows only, never rates) ---+
 HTTP --->  |  JWT check (401) --> routes --> RateQueryService --> repositories --> DB     |
            |                         |               +--> evaluate(...)  [pure]           |
            |                         +--> QueryAuditService --> audit_events              |
            +------------------------------------------------------------------------------+
            +------------- refresh process (exactly 1, only writer of rate data) ----------+
            |  RefreshLoop --> IngestionService --> BoiClient --> boi.org.il/PublicApi     |
            |          validate each currency --> ONE transaction: observations + audit    |
            +------------------------------------------------------------------------------+
            +------------- Postgres (compose) / SQLite (local, tests) ----------------------+
            | currency_pairs | rate_observations (append-only) | policies (versioned)        |
            | non_publication_days | calendar_coverage | refresh_status | audit_events       |
            +------------------------------------------------------------------------------+
```

The main choices, and why:

- **Two processes, one codebase.** There's exactly one writer of rate data by construction, so no distributed locking is needed. The API scales horizontally, and BoI latency is never on the read path.
- **Layered code.** `domain/` (pure: models, calendar, eligibility) ← `services/` (use cases) ← `adapters/` (BoI client, SQLAlchemy repositories) ← `api/` and `refresh/`. The domain has no I/O and no hidden `now()`.
- **Append-only, idempotent storage.** The unique key is `(pair, BoI lastUpdate)` with `ON CONFLICT DO NOTHING`: duplicate polls are harmless, and same-day corrections are kept as history.
- **Database-agnostic, with the differences in one file.** `adapters/db/types.py` holds exact decimals, UTC timestamps and JSON for both dialects. Postgres runs in Compose and SQLite in local runs, and **the same test suite runs on both**. That's how the claim "switchable database" is actually tested.
- **Errors use one format.** Every non-2xx response is RFC 9457 problem details with a machine-readable `code`. 503 is reserved for real faults.
- **Operations:** one `docker compose up`, with ordering `postgres (healthy) → migrate (completed) → api + refresh`. The image runs as a non-root user, and the API has a health check.

## 6. Decisions that changed, and why

The most useful part of the process was letting evidence change decisions. Each change below has its own proposal and design in `openspec/changes/archive/`.

1. **A stale rate is 200 + `NOT_ELIGIBLE`, not 503** (during the first change). The brainstorm leaned towards 503 for an out-of-date rate. I reversed it: a stale rate is a valid domain state, not a server fault. A 503 would trigger retries and circuit breakers for nothing, and it hides useful data (what we hold, and from when). The cost is that consumers must check `eligibility.status`, so the field is marked required in OpenAPI and documented as a consumer obligation. This also turned out to fit the deposit use case: the platform gets the data and can apply its own policy.

2. **Verification found three gaps** (change `fix-verification-findings`). After the first change I verified the code against every spec scenario and found:
   - **Shared `lastUpdate` across currencies.** The parser rejected the whole snapshot when currencies had different `lastUpdate` values. The spec's own single-currency correction scenario contradicted that, so each currency now keeps its own timestamp.
   - **A misleading reason on bad calendar data.** A calendar with no business day inside the cap labelled a fresh rate `MAX_AGE_EXCEEDED`. It now fails closed with 503, after checking whether the rate really is too old.
   - **A readiness field that didn't mean what the spec said.** It reported the time of the last *stored* rate, not the last *successful poll*. Because the refresh process and the API are separate, this needed a small `refresh_status` table, not an in-memory value.

3. **Hardening after the recruiter's deposit use case** (change `harden-deposit-availability`). I listed every way the service could block a deposit and kept only the justified ones:
   - **One bad currency (say GBP) blocked USD as well,** because of whole-snapshot validation. Each pair's eligibility is judged on its own publication date, so that coupling bought nothing. Validation is now per currency, and a `partial` outcome is logged at error level and doesn't count as a healthy poll.
   - **A BoI skip was silent.** Every poll "succeeded" with nothing new. There's now a `notEligiblePairs` readiness field, plus one ERROR log line when a pair goes overdue and an INFO line when it recovers.
   - **The calendar runs out on a known date (2028-01-01).** There's now an advance warning, 60 days by default.
   - **A Postgres crash meant downtime until someone noticed.** Postgres now restarts automatically, verified by crashing it (`docker kill` turned out not to count as a crash, which was an instructive detail).
   - **Our polling lag added avoidable minutes after a late publication.** Polling is now every minute around the deadline.

4. **Authentication and audit, failing closed** (change `add-demo-jwt-auth`). Every rate handed out must be traceable to a user and to *what that user was told*.
   - **Login and tokens.** A demo user exchanges a password for an HS256 JWT.
   - **What's audited.** Every authenticated query writes a `RATE_QUERIED` record with the parameters as sent, the `rateId` and the exact eligibility verdict from the response.
   - **Fail closed on audit.** A rate is returned only after its audit record commits, otherwise 503 `AUDIT_UNAVAILABLE`.
   - **Where the audit write sits.** In the route and the error handlers, not in middleware: middleware can't turn an already-started 200 into a 503.
   - **Two framework assumptions, checked before designing.** I confirmed with a throwaway app that FastAPI resolves auth before validating parameters, and that the user is visible in the 400 handler.

5. **Closing the self-review's gaps** (change `fix-review-edge-cases`, section 8). When choosing how to handle a BoI restatement, the conservative option was to fail closed on that pair. I chose to trust BoI instead, because it's the authoritative source and blocking a deposit over a restated value would fail the availability half of the standard. History plus a WARNING and an audit entry keep it reviewable.

## 7. How I worked

- **Spec-driven, one change at a time.** Each change produced a proposal (why), delta specs (behaviour, as WHEN/THEN scenarios), a design (decisions with rejected alternatives) and tasks, each with its own check. Specs are merged into `openspec/specs/` when a change is archived. This made "done" checkable: every scenario maps to a test.
- **Tests come from the scenarios.** There are unit tests for the domain and parsing, integration tests for storage, ingestion and the refresh loop (on SQLite and Postgres), and contract tests through the real ASGI app.
- **Verification against reality, not just tests.** Every change ended with a Docker Compose run against live BoI, with the output pasted into README §7. Those runs caught things tests couldn't: a broken test fixture on Postgres, a stale editable install after a folder rename, the `docker kill` restart-policy behaviour, and PyJWT checking token timestamps against the wall clock instead of our injected clock.
- **Failure paths were exercised on purpose:**
  - BoI unreachable: reads keep the same `rateId` and stay ELIGIBLE;
  - database stopped: 503 `STORAGE_UNAVAILABLE`, and the API recovers on its own;
  - Postgres crashed: Docker restarts it;
  - API started without `JWT_SECRET`: it refuses to start with exit code 1.

## 8. Final self-review: what it found and how it was resolved

Before submitting I reviewed the code adversarially and confirmed each suspicion with a small probe against SQLite and Postgres, not just by reading. Four real gaps came out of it. None is hit by BoI's normal behaviour, but each was fixed in a fifth change, `fix-review-edge-cases`; the decisions are in its `design.md`.

| # | Edge case found | Evidence | Resolution | Proven by |
|---|---|---|---|---|
| 1 | **BoI restates a rate without changing `lastUpdate`.** The `(pair, lastUpdate)` key made the insert a silent no-op, so we kept serving the replaced value as ELIGIBLE. | Probe: base snapshot with USD 3.343, then the same snapshot with USD 3.341 under the same `lastUpdate` → `inserted=0, outcome=success`, still served 3.343. | **BoI is the source of truth:** a different value for a stored `(pair, lastUpdate)` becomes the next `revision` and is current (new `rateId`), and the old value stays as history. Compared with the *latest* revision, so a value that flips back becomes current again. Logged at WARNING and listed in the ingestion audit. | Repository tests (no-op, revision 1, flip-back to revision 2, unit-only change), ingestion test, and a contract test where the API serves 3.341 with a new `rateId` |
| 2 | **Postgres rounded beyond 10 decimal places** (`NUMERIC(18,10)`), while SQLite kept the exact text. | Probe: `0.000012345678912` → `0.0000123457` on Postgres only. | Unconstrained `NUMERIC` (migration `0004`, a lossless widening). | The decimal round-trip test with 15 decimal places and `123456789.123456789`, passing on both databases |
| 3 | **No audited tooling for pairs or policy.** The remedy for a currency BoI stops sending ("disable the pair") needed raw SQL; policy changes needed a migration. | Only one operator script existed. | `set_pair_enabled.py` and `activate_policy.py`: each validates, then writes the change and a before/after audit row in one transaction, with no restart needed. | Integration tests for every outcome and exit code, plus a live Compose run (README §7) |
| 4 | **Any date could be declared a non-publication day,** including a future one, which extends eligibility, and there was no undo. | No bounds in the operator service. | Declarations are limited to today back through the policy's max age (Israel time), otherwise `--force`, which is audited. `retract_non_publication_day.py` undoes operator declarations only, never BoI-calendar days. | Integration tests (window edges, future and old dates, force, retraction) and the live run |
| 5 | **Smaller items:** the BoI client doesn't follow redirects or honour `Retry-After`; the token endpoint has no rate limiting; the audit table grows by about one row per read with no retention policy; policy timezone and the parser's Israel timezone are separate constants. | Code reading. | Still open (low): Low: a redirect or 429 storm fails safe (stored rate kept, alerts fire); auth and audit growth are documented as demo-only. Follow redirects within the BoI domain and honour `Retry-After`; login throttling; an audit retention job; derive both timezones from the policy row. | — |

Migration `0004` also refuses to be downgraded while corrections are stored, rather than deleting history. A test covers that too.

## 9. Use of AI

I used Claude (Anthropic) as a pair-engineer throughout: exploring the design space, checking BoI's payload, schedule and calendar, drafting the specs and code task by task, running and reading the tests, and reviewing the result against the specs. The decisions in this document were discussed and chosen explicitly, often after the AI raised a counter-argument or found a gap during verification. The workflow (specs with testable scenarios, then tests, then live verification) existed so that every line can be checked against a stated behaviour, whoever typed it. I can walk through and defend any part of it.

## 10. What I'd do next

1. The remaining low-severity items in section 8, row 5: redirects and `Retry-After`, login throttling, audit retention.
2. Add CI (GitHub Actions running the suite on SQLite and on a Postgres service container), plus `ruff` and `mypy`.
3. Metrics (Prometheus): last successful ingestion, observation age per pair, eligible vs not-eligible reads, audit failures.
4. Automate loading next year's BoI calendar, or at least alert from the existing calendar-expiry warning.
5. Replace demo auth with a real identity provider (OIDC), roles for operator actions, and audit retention.
