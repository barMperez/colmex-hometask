# Design

## Context

See proposal.md (Why). Contracts are in this change's `specs/`. Relevant current state:

- **Storage of observations.** `rate_observations` has the unique key `(base_currency, quote_currency, source_last_update)` and the "latest" index `(pair, publication_date DESC, source_last_update DESC)`. `SqlRateRepository.insert_many` uses dialect `INSERT … ON CONFLICT DO NOTHING` per row and returns the rows it inserted. `latest_for_pair` orders by publication date, then source timestamp.
- **Decimal types.** `adapters/db/types.py: DecimalText` is TEXT on SQLite and `NUMERIC(18,10)` on Postgres. `migrations/versions/0001_schema.py` builds the columns from `DecimalText()`.
- **Operator tooling.**
  - `services/operator.py` has one action, `declare_non_publication_day` (day and audit in one transaction, `AlreadyDeclared` on a duplicate, no date bounds).
  - `scripts/declare_non_publication_day.py` wraps it, with exit codes 0 / 2 (invalid input) / 3 (already declared) / 1 (storage).
  - Pairs and policies have no actions; `policies` has a partial unique index on `active`.
- **Already dynamic.** The API reads the pairs, policy and calendar on every request, and ingestion reads the enabled pairs on every attempt, so the operator changes below take effect without restarts.

## Goals / Non-Goals

**Goals:**
- Never serve a value BoI has replaced, keeping every value BoI ever sent as history.
- Byte-for-byte identical decimals on SQLite and Postgres.
- Every runtime change to pairs, policy or the calendar goes through an audited, validated command.

**Non-Goals:**
- **An HTTP admin API or roles.** Operator actions stay CLI scripts with database access, as today.
- **Adding new pairs at runtime.** A new pair needs a `source_key` and a review, so it stays a migration.
- **Changing the policy's time zone.** It's copied from the current version, keeping it in step with the parser's Israel time zone.
- **Recovering values already rounded on Postgres.** BoI rates so far have at most about 7 decimal places, so nothing has been rounded in practice.

## Decisions

### D1. Same-timestamp corrections become revisions

`rate_observations` gains `revision INTEGER NOT NULL DEFAULT 0`.
- **Unique key:** `(base_currency, quote_currency, source_last_update, revision)`.
- **"Latest" index:** `(pair, publication_date DESC, source_last_update DESC, revision DESC)`.
- **`latest_for_pair`** orders by the same three columns.

**Ingestion.** `SqlRateRepository.insert_many` becomes `store_batch(observations) -> StoreResult(inserted, corrections)`. For each accepted observation, inside the existing ingestion transaction:
1. Read the highest stored revision for `(pair, source_last_update)`.
2. If there is none, insert revision 0.
3. If its `(source_rate, source_unit)` equals the incoming values, do nothing. `Decimal` comparison is numeric, so `3.3430` equals `3.343`.
4. Otherwise insert revision `n + 1` and record a `Correction(pair, source_last_update, previous_rate, new_rate, revision)`.

Comparing against the **latest** revision, rather than any revision, means a value that flips back (A → B → A) becomes revision 2 and is current again, which is what BoI is saying at that moment.

`IngestionResult` and the `RATES_INGESTED` audit payload gain `corrections`, and each correction is logged at WARNING: `rates.same_timestamp_correction pair= source_last_update= from= to= revision=`. The insert keeps `ON CONFLICT DO NOTHING` on the new key, as a guard if two writers ever race on the same revision.

*Alternatives:*
- **Fail closed on the pair** (mark it NOT_ELIGIBLE until a newer `lastUpdate`). Rejected by the user's decision: BoI is the source of truth, and the deposit flow should not be blocked when BoI has simply restated a value.
- **Update the row in place.** Rejected: it breaks append-only history, and existing audit records would point at a rate that no longer exists.
- **Compare against any stored revision.** Rejected: an A → B → A flip would be ignored and B served.

### D2. Unconstrained NUMERIC on Postgres

`DecimalText` uses `NUMERIC` with no precision or scale on Postgres (still `asdecimal=True`). Postgres then stores any finite decimal exactly, as SQLite's TEXT already does.

Migration `0004` runs `ALTER COLUMN rate / source_rate TYPE NUMERIC`. That's lossless, because widening never rounds. Because `0001` builds its columns from `DecimalText()`, a fresh database gets `NUMERIC` from `0001` directly, and `0004`'s alter is then a no-op there. Both paths end in the same schema, which the migration test checks.

*Alternative:* keep `NUMERIC(18,10)` and reject entries with more than 10 decimal places. Rejected: it refuses valid data and adds a validation rule that only one database needs.

### D3. Operator actions share one pattern

`services/operator.py` gains `set_pair_enabled`, `activate_policy` and `retract_non_publication_day`, and `declare_non_publication_day` gains the window. Each one:
1. validates its input (raising `InvalidRequest`, exit code 2);
2. opens **one** transaction that reads the current state, refuses a no-op or a conflict (raising `AlreadyInState`, exit code 3), writes the change, and writes an `AuditEvent` with `before` and `after`;
3. leaves storage errors as exit code 1.

The scripts are thin `argparse` wrappers with the same exit codes as the existing one.

| Action | Event type | `before` / `after` |
|---|---|---|
| Enable / disable pair | `PAIR_ENABLED` / `PAIR_DISABLED` | `{pair, enabled}` |
| Activate policy | `POLICY_ACTIVATED` | the previous and new policy rows |
| Declare day | `NON_PUBLICATION_DAY_DECLARED` | `after.forced: true/false` |
| Retract day | `NON_PUBLICATION_DAY_RETRACTED` | the removed row (day, reason, origin, created_by, created_at) |

**Policy activation:**
- The new version is `max(version) + 1`, taking the time zone from the current active row.
- The previous row is deactivated **before** the new one is inserted active, in the same transaction, so the partial unique index on `active` never sees two active rows.
- Validation: grace ≥ 0, max age ≥ 1, `HH:MM` times.

**The declaration window:**
- Allowed: `today - max_age_days ≤ date ≤ today`, where "today" is Israel time from the injected clock.
- `max_age_days` comes from the active policy; with no active policy, a declaration needs `--force`.
- The refusal message names the allowed range.

**Retraction** refuses `origin = 'BOI_CALENDAR'` days, which only change through migrations.

*Alternative:* an authenticated admin HTTP API. Rejected for this release: it brings roles and a UI surface. The CLI plus database access keeps the trust boundary where it already is (README §6).

## Risks / Trade-offs

- [Corrections are served without human review] → This is the user's decision (BoI is authoritative). Each correction is logged at WARNING and audited with the old and new values, so it's visible and reversible in analysis. A consumer that referenced the old `rateId` can still look it up in the history.
- [A noisy source could flip values repeatedly] → Each flip is a revision and a warning, which alerting will surface. No damping logic is added without evidence that BoI does this.
- [Downgrading `0004` with corrections stored would violate the old unique key] → Downgrade raises with a clear message when any `revision > 0` exists, instead of deleting history.
- [`--force` can still extend eligibility by mistake] → It must be typed explicitly, is recorded as `forced` in the audit, and can be undone with retract.
- [Policy activation is a sharp tool] → Every field is validated, a no-op activation (identical values) is refused, and the audit record holds both versions.

## Migration Plan

1. `alembic upgrade head` applies `0004`:
   - adds `revision` with default 0, so existing rows become revision 0;
   - swaps the unique constraint and the "latest" index;
   - alters the decimal columns on Postgres.

   On SQLite it runs through Alembic batch mode.
2. Deploy the new image. The refresh process starts writing revisions, and the API reads them through the new ordering.
3. **Rollback:** downgrade `0004` only when no corrections are stored. Otherwise keep the schema; the previous image ignores the `revision` column, but would then serve an arbitrary revision for a corrected timestamp, so roll forward instead.
