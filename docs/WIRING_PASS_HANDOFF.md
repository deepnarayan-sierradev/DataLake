# Wiring Pass — Session Handoff

**Date:** 2026-07-28 (second pass of the SOW requirements programme)
**State:** all local gates green; **nothing applied to any AWS account**

This is the companion to `docs/PLATFORM_EVOLUTION_PROGRESS.md` for the wiring pass. Read it if you
are picking up this work, deciding what to deploy, or wondering why a particular decision was made.

---

## What triggered this pass

An audit of the requirements programme asked one question the test suite could not answer: *can a
deployed Lambda actually reach this code?* The answer was mostly no.

- The scope predicate (DL-12's core) was **never constructed at runtime** — `build_scope_claims()`
  had no production caller, every consumer defaulted `scope_predicate` to `None`, and
  `_apply_scope_predicate` returned early on `None`. Every query ran tenant-wide.
- No writer emitted the `scope_unit_id` column the predicate filters on.
- The ten SOW connectors were imported **only by their tests**, so `resolve_builder()` raised
  `KeyError` at runtime for all of them.
- Entity resolution still merged across scope units — the exact defect DL-12 was created to fix.
- DL-11 (config propagation), DL-06 (workflow), DL-10 (portability) had **no entry point at all**.
- `tests/test_tenant_isolation.py` parameterised over every `ConsumptionSurface` but only exercised
  the predicate *object*, so it reported isolation working while four surfaces applied no filter.

**Root cause:** the definition of done was "module + unit tests + gates green". A unit test imports
the module under test directly — which is precisely the import the handlers were missing.

---

## The six gates (why this cannot recur silently)

Each was built **before** any fix and shown red, so the mechanism is demonstrably able to detect the
defect class rather than merely asserting it.

| Gate | Where | Red baseline | Catches |
|---|---|---|---|
| G1 reachability | `make reachability` | 68 unreachable modules | A production module with no production importer |
| G2 registry | `connector_runtime/tests/test_handler_connector_reachability.py` | 22 failures | A source the extraction handler cannot resolve |
| G3 call sites | `tests/test_scope_call_sites.py` | 9 failures | A consumption surface applying no predicate |
| G4 fail-open | `make fail-open` | 6 violations | A security parameter defaulting to `None` |
| G5 traceability | `make traceability` | 58 declared-only, 23 missing | A requirement uncited, unreachable, or falsely waived |
| G6 absence alarms | `platform_metric_alarms.tf` | n/a (new) | A control publishing no metric because it never runs |

`make wiring-gates` runs G1/G4/G5; all six run in CI (`wiring-gates` job + pytest + Terraform).

**`requirements/WAIVERS.md` is load-bearing.** Anything unreachable or uncited must be waived there
with a reason and the plan item that will fix it. **G1 and G5 fail on a stale waiver**, so the file
cannot drift once code catches up — it flagged 25+ stale waivers across this pass, which is what
kept it honest rather than aspirational.

---

## What landed

**Scope isolation, end to end.** Attribution stamps `scope_unit_id` at the curated write and **fails
the stage** above the declared unattributed threshold; the partition profile is *required* (defaulting
to `single` would have been the fail-open this exists to prevent). Claims come from
`custom:scope_units` on the verified JWT in one place. Every security parameter is now required, so an
unscoped call will not compile. The twin API filters and returns **404, not 403**, for a foreign
unit's twin — confirming existence is the disclosure DL-SCOPE-13 forbids.

**Entity resolution partitioned by scope unit.** The scope unit participates in the blocking key, and
the guarantee holds even when no blocking strategy is declared (a brute-force single block would
otherwise span every unit). `RESOLUTION_SCOPE_VIOLATIONS` counts any comparison that reaches the
pairwise stage across units — defence in depth against a blocking defect.

**DL-11 wired.** The pipeline trigger pins every `latest` pointer at the run boundary; the ER stage
asserts the observed version matches and records the effective version, with
`fail_on_mismatch=True` because ER is the capability where a mid-run change produces *wrong* output
rather than merely inconsistent provenance.

**Four Lambdas that had no deployment**: webhook receiver, write-back, workflow runner, portability —
each with **its own** least-privilege role. The write-back role reads only the `-writeback` secret
suffix; the portability role holds the only bulk `s3:DeleteObject` in the platform.

**L14 auto-resume**, which the code previously said was "intentionally left undone". See the design
note below.

Plus: governance/triage/serving-onboarding routes, batch quality gate + exception store on the live
path, RLS policies **and their supporting indexes** applied by the loader, opaque continuation
tokens, two tenant-keyed GSIs, NetSuite keyset pagination, per-tenant usage metering, result cache,
durable fleet-wide circuit breaker.

**Traceability: 66 → 96 wired.** 2,740 tests, 95.4% coverage, mypy 380 files, bandit 0, pip-audit
clean, `terraform validate` green in dev/staging/prod.

---

## Design notes worth not re-deriving

**L14 was simpler than its own comment feared.** The old note said auto-resume needed either a
Choice/Wait loop or a redesigned Lambda input contract. Option (a) is expressible:
`States.StringToJson($.checkpoint.Cause)` parses the resume payload out of the exception message. And
**the resumed invocation needs no new input** — the partial watermark is committed *before* the
raise, so re-reading the watermark is what continues from the right position. The resume position
lives in the watermark table, not the state-machine payload. The loop is bounded by
`max_extraction_resume_attempts` (default 12), ending at a visible `Fail`.

The same change fixed the billed-idle ceiling: `_back_off` refuses to sleep past 5 seconds and raises
`ResumeAfterBackoffRequired`, which the workflow converts into the same checkpoint. A `Wait` state
costs nothing.

**Three audit findings were wrong, and the corrections changed the work:**

1. The quality modules are **not** duplicates. `quality_policy_evaluator.py` does per-record field
   checks; `data_quality/quality_checks.py` does batch-level rates. Complementary granularities — a
   batch of individually-valid records can still be 40% incomplete. The fix was a bridge
   (`data_quality/batch_quality_gate.py`), not a deletion.
2. `build_merge_plan` should **not** be executed by the loader: every loader already merges
   correctly inline, so a generated plan would create the duplication finding (1) wrongly alleged.
   Its real value is `default_sizing_profile`, whose indexes cover exactly the columns the RLS
   predicate filters on.
3. **L15 full streaming is not achievable as scoped.** Union-find cannot decide transitive matches
   from a stream (blocking already bounds it), and the batch quality gate needs the whole set to
   compute rates. The contained part — `RecordBlocker.partition` accepting any iterable — is done.

**Defects the gates found in my own work**, which is the clearest evidence they earn their keep:

- `__tenant__` satisfied `SCOPE_UNIT_ID_PATTERN`, so a scope unit literally named that in a
  partitioned tenant would collapse the predicate to match-all. Now reserved at registration but
  still valid inside a claim.
- Two engine enums diverge (`ServingStoreEngine.MYSQL_RDS` = `mysql_rds` vs `ServingEngine.MYSQL` =
  `mysql`). Mapped in one named place — renaming either would invalidate stored config records.
- The pagination token accepted a hand-written bare integer, because `removeprefix` is a no-op when
  the prefix is absent. The marker is now required.

**Ordering hazards** (these bite in this order):

1. Attribution must land **before** the predicate is enforced, or a correct isolation control turns
   into `column not found` across every tenant.
2. ER partitioning must land **before** row filters are trusted — a filter cannot repair a golden
   record that already merged two units.
3. `make migrate-connections` / `make migrate-credentials` must run **before** connection-aware code
   deploys to an environment.

---

## Awaiting a decision before any AWS change

1. **`terraform apply`, naming the environment.** Nothing has been applied at any point.
2. **Lake Formation principals must be named first** — applying revokes a grant three real dev
   principals depend on, and the module now also adds a `scope_unit` LF-Tag.
3. **Two GSIs on deployed tables** (`EdlEntityExtractionConfig`, `EdlRunAuditLog`). Online, but they
   backfill and consume capacity. The code works either way — the Query path is conditional on the
   index existing.
4. **The backfill strategy** (`scripts/backfill_scope_attribution.py`): re-run transformation over
   history, or refuse scope-partitioned queries against pre-backfill partitions. Defaults to
   `--strategy report` so the scale can be measured first.

Two enforce-mode flips also remain — IAM tenant boundary and WAF — each needing its own observation
window with `CrossTenantAccessAttempts` at zero before switching.

---

## Still open

`requirements/WAIVERS.md` is the authoritative list. The substantive ones:

- **DL-SCOPE-13 partially done** — the twin *API* filters, but edge fan-out from a tenant-scoped node
  is unfiltered, so enumerating a shared vendor's edges can still reveal another unit has a
  relationship to it. Needs the twin builder to record each edge's owning scope unit.
- **DL-CFG-06 partially done** — TTL bound and `force_refresh()` exist; the observed
  propagation-lag metric is not emitted for the *credential* cache.
- **L15** — see design note 3 above.
- **Staging is unprovisioned** (DL-OPS-01), which blocks the promotion rehearsal and the DR test.
- DL-04 (agent runtime) and DL-05 (ML platform) remain deferred by agreement, leaving SOW §6.1/§6.2
  uncovered — a real deliverable gap, recorded rather than hidden.
