# Enterprise Data Lake — Multi-Tenant Rollout Plan

**Prepared:** 2026-07-07
**Goal:** Take the platform from its current single-tenant state to safely onboarding a
second paying tenant, then to a self-service multi-tenant SaaS product.
**Principle:** Same as `architecture/IMPROVEMENT_PLAN.md` — every change is
backward-compatible; existing dev pipelines keep working under `tenant_code=default`
throughout.

This plan sequences fixes from `architecture/GAP_ANALYSIS_FINDINGS.md` (finding IDs
`ARCH-*`, `DP-*`, `PERF-*`, `SEC-*`, `OBS-*`, `DUP-*`) and reconciles them with the
existing `architecture/IMPROVEMENT_PLAN.md`, which this document supersedes as the
current source of truth for sequencing. It does not restate the technical detail of
either — see the findings doc for evidence and the original plan for the still-valid
design detail on items marked "open" below.

---

## Implementation status (as of 2026-07-08)

Phases 0–6 were implemented in a single pass on 2026-07-07; see
`architecture/GAP_ANALYSIS_FINDINGS.md`'s "Implementation status" table for per-finding detail and
evidence. **A follow-up pass on 2026-07-08 found Phase 2's "done" claim for `ARCH-1`/`ARCH-4` was
overstated** — a repo-wide sweep for the same class of bug found `WatermarkRepository`'s DynamoDB
key, the raw-layer S3 writer, the analytics-publisher output, both golden/canonical-record
publishers, the cross-source curated lookup, the SCD-merge previous-state lookup, the EventBridge
schedule name, the circuit breaker key, the SQS `MessageGroupId`/Step Functions execution name, and
the DLQ replay path all had genuine tenant-collision bugs (guaranteed same-key overwrites in
several cases, not just latent risk) despite the phase being marked complete. All are now fixed —
see `ARCH-1`, `ARCH-4`, and new findings `ARCH-6` through `ARCH-9` in the findings doc — with
regression coverage in `tests/test_tenant_isolation.py` and each affected module's own test suite.
Three lower-severity gaps found in the same sweep (`ARCH-10`–`ARCH-12`: survivorship/match-rule
config, field mapping registry, and `ConfigurationRepositoryClient`'s DynamoDB key are not
tenant-scoped) are deliberately deferred — see their entries in the findings doc for why.

**A second, independent adversarial audit (same day, 2026-07-08)** — five parallel review agents
re-verified every `ARCH-1`/`ARCH-4`/`ARCH-6`–`ARCH-9` fix against the actual code (not the doc's own
claims) and ran a fresh from-scratch sweep for anything the first pass missed. Eight of nine fix
areas were confirmed genuinely correct with non-vacuous regression tests. The audit found and this
pass then closed:
- `ARCH-13`/`ARCH-14` (new, deferred): `governance/lineage_record.py` and
  `transformation_pipeline.py`'s quality-report writer are unscoped by tenant — lower severity
  (`run_id` prevents overwrite, just interleaves audit artifacts), tracked rather than fixed.
- `ARCH-15` (new, **fixed**): a real, pre-existing, tenant-unrelated bug in
  `orchestration/step_functions/extraction_workflow.py` — the circuit-breaker guard check omitted
  `entity_id`, so it read a key nothing ever wrote to, meaning **the circuit breaker could never
  open in production** regardless of real failure counts. Fixed; regression test added.
- Two test-coverage gaps closed: `TestRawLayerWriterPathIsolation` now parametrizes across all 4
  connector adapters (previously only proved Salesforce), and
  `TestScheduleOperationsTenantConsistency` now proves `create_or_update_schedule`/`get_schedule`/
  `delete_schedule` — not just the static `build_schedule_name` helper — honor a non-default
  tenant end-to-end.
- Doc staleness fixed: root `CLAUDE.md`, `docs/PRODUCTION_INCIDENT_RUNBOOK.md`'s Scenario 8 table,
  and a test docstring all described `watermark-repository` isolation as a "post-read
  application-level guard" — that mechanism was removed by the `ARCH-1` fix; isolation is now
  genuine DynamoDB key-level scoping. All three corrected.

Full verification suite (ruff, scoped mypy, `pytest -q` at 96.19% coverage, bandit,
`make banned-names`) green after both passes. Summary:

- **Phases 0–3** (correctness bugs, security hardening, tenant-code data-plane plumbing,
  observability): done and verified — Phase 2's tenant-code plumbing gaps found on 2026-07-08 are
  now closed (see above).
- **Phase 4** (performance): done — MySQL streaming cursor + connection pooling, DuckDB-based
  streaming loads in entity resolution/analytics, publisher consolidation onto the shared S3
  writer. Checkpoint/resume detection is implemented; automatic Step Functions re-invocation from
  a checkpoint is not (documented gap — needs an ASL redesign, not a code change).
- **Phase 5** (code consolidation): done — credential clients, raw layer writers, and record
  publishers consolidated. Query-builder consolidation covers Salesforce/NetSuite/MySQL;
  Sage's Intacct/X3 engines are intentionally left separate (JSON/OData, not SQL — see `DUP-4`).
- **Phase 6** (control plane): code-complete — `connector_runtime/api/` + Cognito/JWT Terraform
  module, `terraform validate` clean for `dev`. **Not verified against a live AWS deployment** —
  this was an accepted tradeoff for doing all 7 phases in one pass (see the risk register below).
- **Phase 7** (launch readiness): the automated isolation test and runbook update are done (see
  below). Pilot tenant onboarding and load testing at scale require a live deployment and were not
  attempted.

**What this means before onboarding a second real tenant:** everything above the control plane is
code-complete and test-verified today. Before any real tenant beyond `default` is onboarded, the
control plane needs to actually be deployed to a real AWS account and exercised end-to-end (the JWT
claims-path assumption in particular — see `ARCH-3` in the findings doc), and Phase 7's pilot-tenant
and load-test items need to actually run. Nothing below has a fake or mocked verification standing
in for that.

---

## Reconciliation with `architecture/IMPROVEMENT_PLAN.md`

Verified against the current code before writing this plan, so effort isn't duplicated:

| Item | Status | Notes |
|---|---|---|
| §1.1 Multi-Tenancy Data Model | **Done (2026-07-08)** | Was marked "Partial" pending watermark/audit/raw-layer work; that work is now complete — see `ARCH-1`, `ARCH-4` in the findings doc for the full list of what was actually fixed on 2026-07-08 (watermark key, raw layer, analytics/golden/canonical publishers, curated lookups, schedule name, circuit breaker, SQS/SFN naming, DLQ replay) |
| §1.2 SaaS Control Plane API | Open | Not started — `ARCH-3` |
| §1.3 Config-Driven Entity Type Registry | Open | Not started — `ARCH-2` |
| §1.4 Config-Driven Survivorship Policy | **Open (resolved 2026-07-08)** | The "Verify" was answered: `survivorship_policy.py` is versioned/dataclass-based logic only — its *persistence* layer (`resolution_config_registry.py`) is global, not tenant-scoped. This was never built, not merely unverified. Tracked as `ARCH-10`, deferred alongside `ARCH-11`/`ARCH-12` (same registry-client design work, no live second tenant needs it yet) |
| §1.5 Terraform-managed DynamoDB tables | Open | Not verified as started; carried into Phase 2 |
| §1.6 SQS Burst Buffer | **Done** | FIFO queue, `pipeline_trigger` Lambda, reserved concurrency, `Lambda.TooManyRequestsException` retry all confirmed in `infrastructure/modules/orchestration/main.tf` |
| §2.1 Remove dead `_GATE_ORDER` | **Done** | Confirmed — single canonical `_GATE_ORDER` tuple, no dead variable |
| §2.2 Per-connector params validation | **Done** | All four `*_params.py` already use Pydantic with `extra="forbid"` |
| §2.3 Batch iteration in `_table_to_records` | **Done** (superseded) | Uses `to_batches`; function is now dead code, superseded by `_iter_raw_records_batched` |
| §3.1 DuckDB SCD merge | **Done** | Confirmed in `transformation/curated_utils.py` |
| §3.2 Stream canonical records | **Partial** | Streaming path exists but only activates when no quality policy/masking/accumulator is configured — the list-based path is likely the common case in practice — `PERF-3`-adjacent, tracked in Phase 4 |
| §3.3 Shared S3ParquetWriter | **Partial** | Curated layer + analytics publisher done; raw layer writers (`DUP-1`) and golden/canonical publishers (`PERF-4`) not migrated |
| §3.4 Stream ER + analytics publisher | Open | Not started — `PERF-3` |
| §3.5 Checkpoint-and-resume | Open | Graceful abort exists, no resume — `PERF-5` |
| §3.6 NetSuite page size | **Done** | Confirmed `_PAGE_SIZE = 10_000` |
| §3.7 Lambda memory sizing per entity | Not verified | Low priority; revisit after Phase 4 |
| §4.2 Circuit breaker DDB fallback alarm | **Done** | Confirmed in `infrastructure/modules/observability/main.tf` |
| §4.3 Automated Secrets Manager rotation | Open | `rotation_lambda_arn` unset everywhere — `SEC-6` |
| §4.4 DLQ consumer + replay audit | **Done** | Fully implemented, production-grade — `orchestration/dlq_processor/dlq_processor_handler.py` |
| §4.5 Security event metric filters | **Done** | Confirmed for circuit breaker; verify remaining filters during Phase 1 |
| §5.1–§5.6, §5.8 Observability/alerting | **Done** | Stage dimension, ER/analytics metrics, DLQ depth alarm, Lambda alarms, X-Ray, Logs Insights queries, PagerDuty all confirmed present |
| §5.7 End-to-end SLA metric | Open | Not started — `OBS-4` |

**New findings not in the original plan at all:** `DP-1` (SCD-merge bug), `SEC-1` (PII
masking disabled), `OBS-1`/`OBS-2`/`OBS-3` (correlation-ID and error-handling gaps),
`SEC-3`/`SEC-4`/`SEC-5` (Sage secrets Terraform, CI secret scanning, tenant_code
validation), `PERF-1`/`PERF-2` (MySQL streaming/pooling), all of `DUP-*` (code
consolidation). These did not exist as tracked work before this review.

---

## Phase 0 — Correctness & Compliance Emergency Fixes

**Goal:** Stop the platform from silently corrupting data or exposing PII before a
single additional byte of customer data flows through it. Blocks every other phase.

| ID | Work item | Files |
|---|---|---|
| `DP-1` | Fix `ConfigurationRepositoryClient` call site; narrow the swallowing `except`; add the missing handler test | `transformation/transformation_pipeline_handler.py:180-183,211` |
| `SEC-1` | Wire a real `EntityClassificationPolicy` into `TransformationPipeline` instead of `None` | `transformation/transformation_pipeline_handler.py:226` |

**Also in this phase:** audit existing curated tables for duplicate/stale rows
accumulated while `DP-1` was broken, and decide whether a one-time backfill/re-merge is
needed before this fix ships (the fix alone does not retroactively clean already-written
data).

**Exit criteria:** `mypy` clean on both files; a new integration test constructs
`TransformationPipeline` via the real handler path and asserts (a) the accumulator is
wired when `primary_key_field` is set, and (b) a PII-classified field is masked in the
output. Both fixes deployed to dev and one full pipeline run observed end-to-end before
touching staging/prod.

**Backward compatibility:** Additive/corrective only — no schema or event contract
changes. Existing entities without a `primary_key_field` or PII-classified fields are
unaffected.

---

## Phase 1 — Security Hardening

**Goal:** Close the gaps that make holding a second tenant's data unsafe at the
infrastructure level, independent of the tenant-code plumbing work in Phase 2.

| ID | Work item | Files |
|---|---|---|
| `SEC-2` | Scope IAM by tenant: per-tenant role or S3/DynamoDB/Secrets condition on `tenant_code` prefix, replacing the environment-wide `secretsmanager:GetSecretValue` wildcard | `infrastructure/modules/iam/main.tf:117-125` |
| `SEC-3` | Add missing Sage secret + deny-policy Terraform resources | `infrastructure/modules/secrets/main.tf` |
| `SEC-4` | Add `detect-secrets` (or gitleaks) as a CI job, not just a pre-commit hook | `.github/workflows/ci.yml` |
| `SEC-5` | Validate `tenant_code` against `TENANT_CODE_PATTERN` in the 3 handlers currently trusting it unvalidated | `connector_runtime/extraction_pipeline_handler.py:190`, `entity_resolution/entity_resolution_pipeline_handler.py:301`, `analytics_publisher/analytics_publisher_handler.py:311` |
| `SEC-6` | Activate Secrets Manager rotation (wire `rotation_lambda_arn` per environment, or SNS pre-expiry notification where programmatic rotation isn't supported) | `infrastructure/modules/secrets/main.tf`, `infrastructure/environments/{dev,staging,prod}` |
| §4.5 (verify) | Confirm remaining security event metric filters beyond circuit breaker are in place | `infrastructure/modules/observability/main.tf` |

**Exit criteria:** `SEC-2`'s IAM scoping is provable — a scoped-down role for a test
`tenant_code` cannot `s3:GetObject`/`secretsmanager:GetSecretValue` outside its own
prefix (write an automated policy-simulation test, not just a manual check). CI fails a
PR that introduces a detected secret.

**Backward compatibility:** `SEC-2` is the riskiest item here — IAM tightening can break
existing dev pipelines if scoping is wrong. Roll out `tenant_code=default` with an IAM
condition that matches the existing unscoped behavior first, prove it in dev, then
tighten.

---

## Phase 2 — Complete the Multi-Tenant Data Plane

**Status: ✅ DONE (2026-07-08).** Goal was to finish the tenant_code plumbing that Phase 1's IAM
scoping depends on. All items below are complete; `ARCH-1`/`ARCH-4` in particular required a second
pass on 2026-07-08 after direct re-verification found the 2026-07-07 "done" mark was overstated
(see `architecture/GAP_ANALYSIS_FINDINGS.md` for the full list of what was actually still broken).

| ID | Work item | Files | Status |
|---|---|---|---|
| `ARCH-4` | Require `tenant_code` in every pipeline stage's event contract; validate against `TENANT_CODE_PATTERN` | All four Lambda handlers (`extraction_pipeline_handler.py`, `entity_resolution_pipeline_handler.py`, `analytics_publisher_handler.py`, `transformation_pipeline_handler.py`) | ✅ Done — required + always format-validated in all four, not just extraction |
| `ARCH-1` | Thread `tenant_code` through every tenant-owned resource's table keys and S3 paths | `watermark_repository.py`, `raw_layer_writer.py` (+ 4 adapters), `analytics_publisher_handler.py`, golden/canonical record publishers, `curated_utils.py`, `curated_accumulator.py` | ✅ Done — see `ARCH-1` finding for the full list; `configuration_repository.py`/`snapshot_repository.py` were already correct from the 2026-07-07 pass |
| `ARCH-2` | Replace `entity_type_registry.py` hardcoded dicts with a DynamoDB-backed `EntityTypeRegistryClient` keyed on `(tenant_code, entity_id)`; seed current dicts as the `default` tenant | `entity_resolution/entity_type_registry.py`, `entity_resolution/entity_resolution_pipeline_handler.py` | ✅ Done |
| §1.4 (verify/finish) | Confirm whether `survivorship_policy.py`'s existing versioned design needs an S3-backed, tenant-scoped registry client on top | `entity_resolution/survivorship_policy.py` | **Resolved, still open** — it does need one; tracked as `ARCH-10`, deferred (design-sized work, see findings doc) |
| §1.5 (finish) | Migrate DynamoDB tables into Terraform-managed resources (import existing state), add tenant GSIs where the above items need them | `infrastructure/modules/metadata_persistence/main.tf` | Not re-verified in this pass — carries forward as open |

**Exit criteria (met for the code-level guarantee):** A synthetic second tenant
(`tenant_code=acme-test`) can run extraction → transformation → entity resolution → analytics with
every S3 path, DynamoDB key, and audit record correctly prefixed/scoped — proven at the
repository/unit level by `tests/test_tenant_isolation.py` and each affected module's own test
suite (`TestWatermarkRepositoryKeyIsolation`, `TestRawLayerWriterPathIsolation`,
`TestCircuitBreakerTenantIsolation`, and the `TestTenantIsolation` classes in
`analytics_publisher/tests/test_analytics_publisher_handler.py` and
`entity_resolution/tests/test_canonical_record_publisher.py`). A live, deployed end-to-end run
with a real second tenant is still Phase 7's job, not this phase's.

**Backward compatibility:** `tenant_code` defaults to `"default"` everywhere until this
phase completes; existing dev pipelines are unaffected. Do not remove the default until
Phase 6's control plane can assign real tenant codes.

---

## Phase 3 — Observability & Correctness Hardening

**Goal:** Make sure the platform can be operated and debugged once more than one
tenant's runs are interleaved through shared logs/dashboards.

| ID | Work item | Files |
|---|---|---|
| `OBS-1` | Add `clear_contextvars()` in a `finally` block to prevent cross-invocation log corruption | `entity_resolution/entity_resolution_pipeline_handler.py:146-150`, `analytics_publisher/analytics_publisher_handler.py:157-161` |
| `OBS-2` | Add top-level structured error handling (try/except → structured log → re-raise) to ER and analytics handlers | same two files |
| `OBS-3` | Standardize on one correlation-ID mechanism (contextvars binding) platform-wide | `observability/structured_logger.py` and all four handlers |
| `OBS-4` | Add an end-to-end `PipelineEndToEndDurationMs` metric | `analytics_publisher/analytics_publisher_handler.py`, `observability/metrics_emitter.py` |
| `OBS-5` | Add a live `health_check()` to `ConnectorInterface` | `connector_runtime/interfaces/connector_interface.py`, all four adapters |

**Exit criteria:** A deliberately-triggered failure in ER/analytics produces a JSON log
line matching the `failed_runs_last_24h` saved query, with the correct `run_id` — tested
by forcing two back-to-back warm-container invocations (one success, one failure) and
asserting the failure's logged `run_id` matches the failing invocation, not the prior one.

**Backward compatibility:** Fully additive — no behavior changes to successful runs.

---

## Phase 4 — Performance at Multi-Tenant Scale

**Goal:** Ensure no connector or pipeline stage regresses under concurrent multi-tenant
load or genuinely large (multi-million-row) entities.

| ID | Work item | Files |
|---|---|---|
| `PERF-1` | Switch MySQL extractor to `SSDictCursor` for real server-side streaming | `connector_runtime/adapters/mysql_rds/mysql_rds_connector.py:259`, `mysql_incremental_extractor.py:99` |
| `PERF-2` | Add MySQL connection pooling / front sources with RDS Proxy | `connector_runtime/adapters/mysql_rds/mysql_rds_connector.py:196-205` |
| `PERF-3` | DuckDB-based streaming join for entity resolution; streaming schema capture for analytics publisher | `entity_resolution/entity_resolution_pipeline_handler.py:218,233`, `analytics_publisher/analytics_publisher_handler.py` |
| `PERF-4` | Migrate golden/canonical record publishers to `S3ParquetWriter` | `entity_resolution/{golden_record_publisher,canonical_record_publisher}/*.py` |
| `PERF-5` | Implement checkpoint-and-resume for extractions exceeding 900s | `observability/lambda_utils.py`, `orchestration/step_functions/extraction_workflow.py` |
| §3.2 (finish) | Confirm the streaming pipeline path (`can_stream`) is the actual common case, not just the exception, once masking (`SEC-1`) is turned on — masking may currently force the list-based path more often than expected | `transformation/transformation_pipeline.py:207-233` |

**Exit criteria:** A load test against a synthetic 5M-row MySQL table completes within
Lambda memory limits using `SSDictCursor`; a load test with 3 tenants running concurrent
extractions against the same source type does not exhaust RDS `max_connections` or
Lambda reserved concurrency.

**Backward compatibility:** No API/contract changes; purely internal implementation
swaps. Verify NetSuite/Sage/Salesforce connectors are unaffected (they don't use
`SSDictCursor`/pooling changes).

---

## Phase 5 — Code Consolidation

**Goal:** Reduce the per-connector maintenance cost before the connector count grows
(new tenants will eventually mean new source types, not just new tenant codes on
existing ones).

| ID | Work item | Files |
|---|---|---|
| `DUP-2` / `DP-2` | Promote `SageCredentialManager` + Deterministic/Transient error hierarchy to a shared base in `connector_runtime/interfaces/` | Sage `common/` + all adapters |
| `DUP-1` | Extract a `RawLayerWriter` base on top of `S3ParquetWriter` | four `*_raw_layer_writer.py` files |
| `DUP-4` | Introduce an `IncrementalQueryBuilder` protocol | five query-builder/extractor files |
| `DUP-3` | Factor shared serialization/lineage logic for golden/canonical publishers (pairs with `PERF-4`) | `entity_resolution/{golden_record_publisher,canonical_record_publisher}/*.py` |
| `DP-3` | Fold connector-specific exceptions into the shared Deterministic/Transient base from `DP-2` | `connector_runtime/adapters/{salesforce,netsuite,mysql_rds}/*` |
| `DUP-5` | Add `PipelineHandlerContext` helper + shared `conftest.py` for connector tests | four handlers, `connector_runtime/tests/` |

**Exit criteria:** All four connectors' credential clients, raw writers, and query
builders subclass the same base with no behavioral change (full existing test suite
passes unmodified in assertions, only fixture/setup code changes).

**Backward compatibility:** Pure refactor — no functional or schema changes. Do this
phase before Phase 6 adds a fifth connector's worth of new code to write in the old,
duplicated style.

---

## Phase 6 — SaaS Control Plane

**Goal:** Enable self-service tenant onboarding, replacing CLI-script-only operation.

| ID | Work item | Files |
|---|---|---|
| `ARCH-3` | Build the control-plane API (API Gateway + Cognito + WAF) per `architecture/IMPROVEMENT_PLAN.md` §1.2: `POST /tenants`, entity registration, pipeline trigger, run status | new `connector_runtime/api/` package, new `infrastructure/modules/control_plane/` |
| — | Add per-tenant usage metering (records processed per tenant per period) — no existing code to reference; new capability required for consumption-based billing | new, likely a Lambda subscribed to the same metrics stream as `metrics_emitter.py` |

**Exit criteria:** A new tenant can be onboarded, have an entity registered, and trigger
a pipeline run entirely through the API, with zero engineer-run CLI commands, and the
resulting run is fully isolated per Phases 1–2's guarantees.

**Backward compatibility:** Additive — `scripts/trigger_extraction.py` and friends
continue to work for internal/break-glass use, per the original plan's explicit
guarantee.

---

## Phase 7 — Multi-Tenant Launch Readiness

**Goal:** Prove the whole system before the second real tenant's data lands in it.

- **Isolation test:** ✅ DONE — `tests/test_tenant_isolation.py`. An automated test (not a manual
  check) that provisions two tenants and asserts Tenant B cannot read Tenant A's S3 objects
  (`ConfigurationRepositoryClient` S3 backend, `SchemaSnapshotRepository`), DynamoDB rows
  (`ConfigurationRepositoryClient` DynamoDB backend, `WatermarkRepository`,
  `EntityTypeRegistryClient`), or the control plane's run-status endpoint (404, never 403). Secrets
  Manager isolation is not applicable yet — credentials aren't tenant-scoped in the current design
  (tracked via a skipped placeholder test rather than a fake pass, and as a `SEC-2` follow-up).
- **Runbook update:** ✅ DONE — `docs/PRODUCTION_INCIDENT_RUNBOOK.md` gained a "Suspected
  Cross-Tenant Data Incident" scenario covering which isolation mechanism applies to which
  resource, blast-radius triage commands, and the currently-missing real-time detection alarm.
- **Pilot tenant:** ⬜ NOT STARTED — onboard one real (or realistic synthetic) second tenant end-to-end
  through the Phase 6 control plane, running in parallel with `tenant_code=default` for
  at least one full week of scheduled runs with no cross-tenant incidents. Requires a live
  deployment; not attempted in this pass.
- **Load test at target scale:** ⬜ NOT STARTED — the volume/entity-count numbers used to justify this
  rollout (reference the same 80–100 entity target used in `architecture/IMPROVEMENT_PLAN.md`
  §1.6, multiplied by the initial tenant count).

**Exit criteria:** All four items above pass before any tenant beyond the pilot is
onboarded.

---

## Risk register (multi-tenant-specific)

| Risk | Mitigation |
|---|---|
| `SEC-2` IAM tightening breaks existing dev pipelines | Roll out scoped-but-permissive-for-`default` first; tighten only after Phase 2 completes and is verified in dev |
| `DP-1`'s fix exposes previously-hidden duplicate/stale curated data | Audit and backfill before or immediately after the fix ships, not left indefinitely |
| `SEC-1`'s fix changes output schema/values for existing consumers reading unmasked curated/analytics data today | Communicate to downstream consumers before enabling; consider a short dual-write/verification window in dev/staging |
| Phase 2's `tenant_code` plumbing touches high-traffic hot-path repositories (`watermark_repository.py`) | Default to `tenant_code="default"` and land changes behind the pattern already proven safe on the transformation side; test against existing dev entities before staging |
| Phase 5 consolidation introduces a regression shared across all connectors at once (vs. today's isolated blast radius per connector) | Land one connector's migration to the new base at a time, full regression suite between each |
| Phase 6 control plane becomes a second, inconsistent way to trigger pipelines | Route the control plane through the *same* `states:StartExecution` / SQS trigger path as `scripts/trigger_extraction.py`, not a parallel code path |

---

## Non-goals (unchanged from the original plan)

- Re-architecting the Step Functions state machine topology
- Replacing DynamoDB with a different metadata store
- Migrating existing raw/curated S3 data to new tenant-scoped paths retroactively (new
  tenants use the new scheme from day one; `default` stays where it is)
- Replacing structlog, Parquet/Snappy, or the Lambda-based execution model

---

## Sequencing summary

```
Phase 0  Fix DP-1 (SCD merge) + SEC-1 (PII masking)         ── blocks everything below
Phase 1  Security hardening (IAM, secrets, CI scanning)     ── blocks Phase 2's IAM tightening
Phase 2  Complete tenant_code data-plane plumbing           ── blocks Phase 6 (control plane needs real tenant scoping)
Phase 3  Observability hardening                            ── parallel with Phase 2/4
Phase 4  Performance at scale                                ── parallel with Phase 2/3
Phase 5  Code consolidation                                  ── before Phase 6 adds new connector-shaped code
Phase 6  SaaS control plane                                  ── requires Phase 1+2 complete
Phase 7  Launch readiness (isolation test, pilot, load test) ── final gate before 2nd real tenant
```

Phases 3 and 4 can run in parallel with Phase 2 by a separate workstream — they don't
share files with the tenant-code plumbing. Phase 5 should land before Phase 6 so the
control plane isn't onboarding tenants onto connector code that's about to be
refactored underneath them.
