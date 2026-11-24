# Proposal

## Why

The final self-review (WRITEUP §8) found four gaps. Each was confirmed with a probe or by reading the code:

1. **A BoI correction that keeps the same `lastUpdate` is silently ignored.** The `(pair, lastUpdate)` key makes the insert a no-op and the attempt reports `success`. Re-probed with a realistic value: BoI's latest snapshot said USD 3.341, and we kept serving 3.343 as ELIGIBLE.
2. **Postgres silently rounds past 10 decimal places** (`NUMERIC(18,10)`: `0.000012345678912` → `0.0000123457`), while SQLite stores the exact text. The two supported databases disagree, which breaks the "exact decimals end to end" contract.
3. **No audited tooling for enabling or disabling a pair, or activating a new policy version.** The documented remedy for a currency BoI stops sending ("disable the pair") needs raw, unaudited SQL. Policy changes need a migration.
4. **An operator can declare any date a non-publication day,** including a future business day, which keeps yesterday's rate eligible, and there's no way to retract a declaration.

## What Changes

- **Same-timestamp corrections are stored. BoI is the source of truth.**
  - Each observation gets a `revision`, starting at 0.
  - When BoI returns a pair with a `lastUpdate` we already hold but a different source rate or unit than its latest revision, the new value is stored as the next revision and becomes current. Earlier revisions stay as history.
  - An identical value is still a no-op.
  - Each correction is logged at WARNING (old → new) and listed in the ingestion audit record.
  - The read API is unchanged; the corrected observation has its own `rateId`.
- **Exact decimals on Postgres.** Rate columns become unconstrained `NUMERIC`, through a migration, so Postgres stores any finite decimal exactly, as SQLite does.
- **Operator commands for pairs and policy,** each writing the change and its audit record in one transaction:
  - `scripts/set_pair_enabled.py --pair USD/ILS --enable|--disable --reason --actor`
  - `scripts/activate_policy.py --grace-minutes --max-age-days --weekday-publish-time --friday-publish-time --reason --actor`, which creates the next policy version and deactivates the previous one.

  Changes take effect on the next read without a restart. That's already true for the API, which reads the pairs and policy on every request.
- **Bounded declarations, and retraction.**
  - `declare_non_publication_day` accepts only dates from today back through the active policy's `max_age_days` (Israel time).
  - Other dates are refused unless `--force` is given; a forced declaration is recorded as such in the audit record.
  - A new `scripts/retract_non_publication_day.py --date --reason --actor` removes an **operator-declared** day (never a BoI-calendar day), with an audit record holding the removed row.

There are no breaking API changes. The one behavior change is in the operator CLI: a declaration outside the allowed window now needs `--force`.

## Capabilities

### New Capabilities

- `supported-pairs`: which currency pairs the service serves, and how an operator enables or disables one with an audit trail.

### Modified Capabilities

- `rate-ingestion`:
  - exact decimals on every supported database;
  - idempotency now keyed by pair, source timestamp **and value**;
  - append-only observations gain same-timestamp revisions, and the latest revision is current.
- `rate-freshness-policy`:
  - operator declarations are bounded to the max-age window (or `--force`);
  - declarations can be retracted;
  - new policy versions can be activated with an audit record.

## Impact

- **Database:** migration `0004`.
  - `rate_observations` gains `revision` (default 0).
  - The unique key becomes `(pair, source_last_update, revision)`, and the "latest" index includes `revision`.
  - `rate` and `source_rate` become unconstrained `NUMERIC` on Postgres (unchanged TEXT on SQLite).
  - Downgrade refuses to run while any observation has `revision > 0`, rather than silently deleting corrections.
- **Code:**
  - `adapters/db/tables.py`, `types.py`, `repos.py` (revision-aware insert, `latest_for_pair` ordering);
  - `services/ingestion.py` (correction detection, audit payload);
  - `services/operator.py` (pair, policy, bounded declare, retract);
  - three new scripts plus changes to the existing one.
- **Tests:** ingestion and repository (revisions, flip-back), decimal round trip with more than 10 decimal places on both databases, operator services and script exit codes, and the migration upgrade/downgrade.
- **Docs:** README §4 (operator actions), §6 (limitations) and §7 (operator commands); WRITEUP §8 (edge cases resolved).
