# Remediation Pass — Session Handoff

**Date:** 2026-07-29
**State:** all 16 re-assessment findings fixed and pushed to `main`; **nothing applied to any AWS
account**

Companion to `docs/WIRING_PASS_HANDOFF.md`. Read that one first for the six gates and the
programme's design decisions; this one covers what a second assessment found in that work, and what
is still open.

---

## What triggered this pass

The same assessment prompt was run again against the repo. It produced 16 findings — and the
uncomfortable part is the attribution: **ten of them were defects in the wiring pass itself**, not
fresh discoveries in older code.

That is worth stating plainly rather than smoothing over. The first pass fixed "the code exists but
nothing calls it". This one found "something calls it, but it filters on a field that does not
exist, or grants on a tag that is never assigned, or greps for a pattern that can never match."

The common mechanism: **the same author wrote the code, the test, and the gate**, so a
misunderstanding of the failure mode was reproduced identically in all three, and all three went
green. Three concrete instances:

- The twin routes filtered on `twin.scope_unit_id` while `Twin`, `TwinEdge` and the DynamoDB item
  carried no such field. `getattr(..., None)` made it always evaluate `matches(None)`. Tests used
  `demo`, a single-partition tenant where that is `True`. G3 asserted the literal string
  `scope_predicate` appeared in the function body.
- The `scope_unit` LF-Tag was created, referenced only in a `depends_on`, and counted as satisfying
  its requirement because the id appeared in a `.tf` file.
- `make banned-names` used BRE alternation under `grep -E`, so `\|` was a literal pipe and the
  pattern could only match the literal text `def helper|def util|…`.

---

## The worst problem was not in the sixteen

`.gitignore` had blanket `*secret*` and `*credential*` rules, intended for credential *data*. They
also matched *source*, and excluded **14 files** from the repository:

- `connector_runtime/credential_client.py` — the shared base class `connector_runtime/CLAUDE.md`
  cites
- `connector_runtime/connection_credential_resolver.py` — the canonical credential path resolver
- `connector_runtime/credential_rotation/credential_expiry_notifier_handler.py` — a Lambda handler
- `serving_store/credential_delivery.py`, `scripts/migrate_credentials_to_connection_paths.py`,
  three adapter clients, three test modules
- `infrastructure/modules/secrets/{main,outputs,variables}.tf` — a module **all three environments
  reference**

At least eight tracked modules import the Python ones. Proven rather than inferred:

```
$ git clone . /tmp/check && cd /tmp/check
$ python -c "import connector_runtime.writeback_handler"
ModuleNotFoundError: No module named 'connector_runtime.connection_credential_resolver'
```

**CI had therefore failed on every run for months** — including before this work started — while
every check passed on developer machines, where the files sit untracked on disk. `ruff` also respects
`.gitignore`, so those 14 files had never been linted, type-checked, or bandit-scanned; fixing that
surfaced real lint errors. The shell's `grep` here is a function that respects it too, which is why a
repo-wide rename silently skipped them.

The fix keeps the data patterns and re-includes source. **Directory negations must come first** — a
file cannot be re-included while its parent directory is excluded, which is what hid
`credential_rotation/` and `modules/secrets/` whole.

**A fresh clone now runs the full suite green.** That had never been true before.

---

## Two new gates

Both were built before their fix, shown red on the real defect, and have committed negative tests
including a **positive control** — without one, a gate that always failed would pass every negative
test and be equally useless.

| Gate | Command | Catches |
|---|---|---|
| **G7** security columns | `make security-columns` | A column a scope filter reads that no record declares or writer sets |
| Naming | `make banned-names` | Prohibited generic identifiers — now including suffixes (`SageCredentialManager`), module filenames, and package directories |

The naming gate replaced a `grep` that could not fail. It excuses `SecretsManager` only inside that
exact compound, so `SecretsManagerCredentialClient` passes while a bare `CredentialManager` fails.
There is deliberately **no file-based allowlist** — that is how a naming rule dies quietly.

`make wiring-gates` now runs four gates; the naming gate is its own CI job.

---

## Design decisions worth not re-deriving

**An LF-Tag cannot filter rows.** Tags apply to tables and columns, so tagging a curated table with
a scope unit enforces nothing when the table holds many units' rows — which is every curated table
here. Row-level Athena isolation uses `aws_lakeformation_data_cells_filter` with a row-filter
expression per (table, scope unit). The assessment's first proposal was wrong about this and the
correction is recorded in the module itself.

**RLS must exempt the loader, by role.** `ENABLE` + `FORCE ROW LEVEL SECURITY` with only a
`FOR SELECT` policy would have refused the loader's *own next upsert*: under RLS PostgreSQL denies
any command with no policy, and `FORCE` removes the table-owner exemption. Fixed with
`FOR ALL … TO <loader_role>` rather than `BYPASSRLS`, which is superuser-adjacent and not otherwise
needed. `FORCE` is kept deliberately — without it the owner reads unfiltered.

**The NULL divergence was fixed in the writer, not the filter.** Attribution stamps `__tenant__` for
a single-partition tenant, so a NULL `scope_unit_id` reaching the serving store is a data defect. The
Python predicate's `IS NULL` branch covers pre-attribution rows in the *lake*; the SQL policy
excludes NULL. Reconciled and asserted so they cannot silently diverge.

**Alarm thresholds are derived, not chosen.** See `docs/SCALE_AND_DLQ_THRESHOLDS.md`. The key insight
is that `PipelineFreshnessSeconds` is the only alarm measuring the *commitment* rather than the
failure handling — a run that **succeeds** in five hours breaches a 2–4 hour SLA and emits no DLQ
message at all. It was 24h and non-paging.

**Route tables stay with the dispatcher.** When `control_plane_handler.py` was split, both route
tables sat mid-file between handler groups. Someone asking "what routes exist" should find one
answer in one place.

**`load_active_model` belongs to governance, not intelligence.** The published-model lifecycle —
publish, version, roll back — is a governance concern, and both route groups read through it.

---

## Still open

`docs/KNOWN_GAPS_AND_ROADMAP.md` items 20–24 are the authoritative list. In priority order:

1. **Item 20 is load-bearing.** `RunCoordinator.enqueue_dlq_entry` accepts a `failed_stage` argument
   but hardcodes `_DLQ_NAME = "datalake-extraction-failure-dlq-dev"`, and its only production caller is
   `orchestration/step_functions/extraction_workflow.py`. **Five of six pipeline stages enqueue to no
   DLQ**, and the nine per-stage queues have no producer or consumer — so the alarms sized in this
   pass cannot fire. The fix is small: map `failed_stage` to the queue, and route the other stages
   through `observability/stage_execution.py` so a new handler gets it structurally.
2. **Item 23, the concurrency wall,** waits on one product answer: is the 2–4 hours measured from
   each run's own start, or is it an absolute daily deadline? From run start, per-tenant staggering
   solves it free; absolute, a concurrency limit increase becomes required. The cron jitter is
   deliberately unimplemented until that is settled.
3. **Item 24** — the DLQ processor pages per message; one tenant's ~1,200 entities failing is 1,200
   pages. Needs a digest, and the `(Stage, TenantCode)` metric plus its "one tenant dominates" alarm
   must land together because the alarm↔emitter reconciliation is bidirectional.
4. **No entity has a quality policy attached**, so the DL-DQ gate returns "ungated" everywhere. That
   is config data and needs a decision on which entity and what thresholds.
5. **F4 has no behavioural proof.** The outstanding test is load → apply RLS → load again → reader
   sees only its own unit, against a real PostgreSQL. The 18 tests that exist are SQL-shape
   assertions and say so in their docstring.
6. **F2 enforces nothing until populated.** `scope_unit_row_filters` and `scope_unit_grants` default
   to `{}` on purpose — a grant to a guessed principal ARN is worse than no grant.

---

## Deployment context

The platform deploys to a **brand-new AWS account within days**, which voids three of the four
decisions `docs/WIRING_PASS_HANDOFF.md` was waiting on: the Lake Formation revocation hazard (no
existing grant), the GSI backfill concern (no data), and the scope-attribution backfill strategy (no
pre-attribution history). That handoff records the reasoning rather than deleting it.

Still required: **`terraform apply`, naming the environment** — nothing has been applied at any
point — and the two enforce-mode flips (IAM tenant boundary, WAF), each needing an observation window
with `CrossTenantAccessAttempts` at zero.
