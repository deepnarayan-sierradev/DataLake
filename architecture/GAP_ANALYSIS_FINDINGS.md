# Enterprise Data Lake — Gap Analysis Findings

**Prepared:** 2026-07-07
**Scope:** End-to-end review of architecture, design patterns, performance, security,
observability, and code reuse, assessed against multi-tenant SaaS requirements.
**Method:** Five independent module-level reviews (architecture, security, performance,
observability, duplication), cross-checked against `architecture/IMPROVEMENT_PLAN.md`,
plus direct verification of every headline claim against the running code (including
`mypy` output, `git diff`/`git show`, and test coverage) — not inference from comments
or docstrings alone.

Each finding has a stable ID (`ARCH-n`, `DP-n`, `PERF-n`, `SEC-n`, `OBS-n`, `DUP-n`) used
by `architecture/MULTI_TENANT_ROLLOUT_PLAN.md` to sequence fixes. Priorities: **P0**
(blocks any multi-tenant customer data), **P1** (required before scaling tenant/entity
count), **P2** (operational hardening).

---

## How to read this document

This document catalogs *what is wrong and why*. The companion document,
`architecture/MULTI_TENANT_ROLLOUT_PLAN.md`, decides *in what order to fix it* and
defines phase-exit criteria for a multi-tenant rollout. Findings here do not repeat
work already scoped and unstarted in `architecture/IMPROVEMENT_PLAN.md` — where this
review confirms an item from that plan is still open, it is cited by section number
rather than re-derived; where this review found it already fixed, that is called out
explicitly so the two documents don't drift out of sync.

---

## Implementation status (as of 2026-07-07)

Every finding below was implemented, partially implemented, or explicitly deferred in a single
end-to-end pass. Each row's Evidence column names the actual file — the same verification standard
(`mypy`, `pytest`, `git diff`) used in the initial gap analysis was re-applied to every change before
it's marked DONE here, including catching and fixing two real regressions introduced mid-pass (a
mypy type error in `configuration_repository.py`'s new `list_configs_for_tenant`, and a complexity
violation in `curated_utils.py`'s `find_latest_curated_prefix`) and one CI blind spot (`analytics_publisher/tests`
was never wired into `pyproject.toml`'s pytest `testpaths`, so 8 tests silently never ran — fixed).

| ID | Status | Evidence / Notes |
|---|---|---|
| `DP-1` (bug) | ✅ DONE | Fixed wrong kwarg + narrowed except in `transformation_pipeline_handler.py` |
| `SEC-1` (bug) | ✅ DONE | `build_auto_classification_policy()` wired into `transformation_pipeline.py::execute()` |
| `ARCH-1` | ✅ DONE | `tenant_code` threaded through `ConfigurationRepositoryClient`, `WatermarkRepository`, `SchemaSnapshotRepository`, `EntityTypeRegistryClient`; S3 keys always tenant-prefixed |
| `ARCH-2` | ✅ DONE | `entity_resolution/entity_type_registry.py::EntityTypeRegistryClient` — DynamoDB single-table (PK=`tenant_code`), falls back to the original hardcoded dicts as seed data |
| `ARCH-3` | ✅ DONE (code-complete; **unverified against real AWS**) | `connector_runtime/api/` (6 routes) + `infrastructure/modules/control_plane/` (HTTP API + Cognito + JWT authorizer); `terraform validate` passes for `dev`. Open item: the exact claims-path an HTTP API + JWT authorizer populates at payload format 1.0 (`authorizer.claims` vs `authorizer.jwt.claims`) was not verified end-to-end against live API Gateway — the handler defensively checks both and fails closed (401) either way |
| `ARCH-4` | ✅ DONE | `tenant_code` is a first-class validated field on `PipelineStageContract` |
| `ARCH-5` | ⬜ NOT STARTED | P2, documentation-only recommendation; not touched in this pass |
| `DP-2` | ✅ DONE | `connector_runtime/credential_client.py::SecretsManagerCredentialClient`, generalized from Sage's manager; Salesforce/NetSuite/MySQL/Sage all migrated |
| `DP-3` | ✅ DONE | `TransientConnectorError`/`DeterministicConnectorError` marker classes in `connector_interface.py`; `MySqlIncrementalExtractorError` deliberately left unmarked (documented — covers both deterministic and ambiguous failures) |
| `PERF-1` | ✅ DONE | `mysql_rds_connector.py` uses `pymysql.cursors.SSDictCursor` |
| `PERF-2` | ✅ DONE | Module-level connection cache with `ping(reconnect=True)` health check in `mysql_rds_connector.py` |
| `PERF-3` | ✅ DONE | `transformation/curated_utils.py::load_curated_records_duckdb()`; wired into `entity_resolution_pipeline_handler.py`; `analytics_publisher_handler.py` reuses `S3ParquetWriter.last_written_schema` instead of re-materializing |
| `PERF-4` | ✅ DONE | Both publishers use `S3ParquetWriter` directly via the new `entity_resolution/publishing_shared.py` |
| `PERF-5` | 🟡 PARTIAL | Checkpoint detection, partial watermark commit, and a Terraform `Catch`→`ExtractionCheckpointed` state are implemented (`orchestration/step_functions/extraction_workflow.py`). **Not implemented:** automatic Step Functions re-invocation from the checkpoint — ASL's `Catch` doesn't feed error details back into a retried Task's `Parameters`; needs a Choice/Wait construct or a redesigned input contract. Documented in the module docstring and Terraform comment rather than guessed at |
| `SEC-2` | 🟡 PARTIAL | S3 paths and the `entity-type-registry` table are genuinely tenant-isolated (prefix / partition key). `entity-extraction-config` and `watermark-repository` DynamoDB tables are **not yet tenant-partitioned at the key level** — isolation there is an application-level guard (`_enforce_tenant_match`, tenant check in `get_watermark`), not IAM-enforced. The S3 bucket-policy condition on tenant prefix is not yet turned on either. Both gaps are covered by regression tests in `tests/test_tenant_isolation.py` specifically because IAM can't back them up |
| `SEC-3` | ✅ DONE | `aws_secretsmanager_secret.sage_intacct_credentials` / `sage_x3_credentials` added to `infrastructure/modules/secrets/main.tf` |
| `SEC-4` | ✅ DONE | `detect-secrets` CI job added to `.github/workflows/ci.yml` |
| `SEC-5` | ✅ DONE | `tenant_code` validated in `extraction_pipeline_handler.py`, `entity_resolution_pipeline_handler.py`, `analytics_publisher_handler.py` |
| `SEC-6` | ✅ DONE | `connector_runtime/credential_rotation/credential_expiry_notifier_handler.py` + daily EventBridge Scheduler rule |
| `OBS-1` | ✅ DONE | try/finally `clear_contextvars()` wrapper pattern in entity resolution and analytics publisher handlers |
| `OBS-2` | ✅ DONE | Top-level structured error log before re-raise, same wrapper |
| `OBS-3` | 🟡 PARTIAL | Entity resolution, analytics publisher, and transformation handlers now all standardize on `structlog.contextvars`. `connector_runtime/extraction_pipeline_handler.py` still threads `run_id` as an explicit kwarg — not migrated in this pass |
| `OBS-4` | ✅ DONE | `_emit_metrics_and_e2e_sla()` in `analytics_publisher_handler.py`; `run_started_at` wired through the Step Functions `PublishAnalytics` state |
| `OBS-5` | ✅ DONE | Non-abstract `health_check()` default method added to `ConnectorInterface` |
| `DUP-1` | ✅ DONE | `connector_runtime/raw_layer_writer.py::RawLayerWriter` base; all four raw layer writers reduced to thin subclasses (~950 lines removed) |
| `DUP-2` | ✅ DONE | Same as `DP-2` |
| `DUP-3` | ✅ DONE | `entity_resolution/publishing_shared.py` shared by both publishers |
| `DUP-4` | 🟡 PARTIAL | `connector_runtime/query_builders/incremental_query_builder.py` consolidates Salesforce/NetSuite/MySQL (byte-identical output verified). Sage's Intacct/X3 query engines are **intentionally left unconsolidated** — they build JSON/OData request bodies, not SQL text; forcing them through the SQL template would be a leaky abstraction (documented in-code) |
| `DUP-5` | ⬜ NOT STARTED | P2, `PipelineHandlerContext` helper + shared `conftest.py`; not touched in this pass |

**Phase 7 (launch readiness):**

| Item | Status |
|---|---|
| Automated cross-tenant isolation test | ✅ DONE — `tests/test_tenant_isolation.py` (6 passing assertions across S3, DynamoDB app-level guards, the tenant-partitioned entity-type-registry table, and the control-plane API's 404-not-403 behavior; Secrets Manager isolation is out of scope today and explicitly tracked via a skipped placeholder test, not silently omitted) |
| Runbook update | ✅ DONE — `docs/PRODUCTION_INCIDENT_RUNBOOK.md` §"Suspected Cross-Tenant Data Incident" |
| Pilot tenant onboarding (1 week, live traffic) | ⬜ NOT STARTED — requires a deployed environment; out of scope for a code-only pass |
| Load test at target scale | ⬜ NOT STARTED — same reason |

**Full verification after every change in this pass:** `mypy` clean (module-scoped; two pre-existing
stub/library gaps documented, not fixed — pyarrow-stubs' `ParquetWriter`/`write_table`/`close` typed
as untyped calls, and Sage's pre-existing `no-any-return`/`arg-type` errors, both confirmed via
`git show HEAD:<file> | mypy -` to predate this pass), `ruff check` clean on every authored file,
**1478 passed, 1 skipped, 0 failed** (`pytest -q`, 96.11% coverage, up from the 1390 baseline at the
start of this implementation pass), `terraform validate` clean for `dev` (staging/prod have 7
pre-existing, unrelated missing-argument errors on the `orchestration` module, confirmed unchanged by
this pass).

---

## Two confirmed live bugs (read this first)

These are not "gaps" in the SaaS-readiness sense — they are defects in the
single-tenant platform running today, verified independently of any AI's say-so.

### `DP-1` — SCD merge is silently disabled for every entity — **P0**

**Observation.** `transformation/transformation_pipeline_handler.py:180-183` constructs
the configuration repository client as:

```python
config_repo = ConfigurationRepositoryClient(
    table_name=config_table,
    region_name=region_name,
)
```

The actual constructor, `connector_runtime/configuration_repository/configuration_repository.py:68-74`,
is:

```python
def __init__(
    self,
    environment: str,
    region_name: str,
    backend: ConfigurationBackend = ConfigurationBackend.DYNAMODB,
    s3_bucket: str | None = None,
) -> None:
```

There is no `table_name` parameter and no `**kwargs` catch-all.

**Proof, not inference.**
- Running `mypy transformation/transformation_pipeline_handler.py` in this repo's own
  configured environment reports: `error: Unexpected keyword argument "table_name" for
  "ConfigurationRepositoryClient" [call-arg]`.
- `ConfigurationRepositoryClient` is defined exactly once in the repo. Its own unit
  tests (`connector_runtime/tests/test_configuration_repository.py:75-77`) construct it
  correctly with `environment=_ENV`, proving the real signature.
- `transformation/tests/` has **no test file for `transformation_pipeline_handler.py`**
  — nothing exercises this call site, which is why the broken keyword argument survives.
- `git show HEAD:...` confirms the bug is present in the last **committed** revision,
  not just in-flight working-tree changes — it predates the current branch's edits.

**Gap / Risk.** The call raises `TypeError` on every invocation. It is caught by
`except Exception as exc:` at line 211 — intended only for `ConfigurationNotFoundError`
— logged as a `warning`, and swallowed. `curated_accumulator` is `None` on every run.

**Impact.** SCD Type-1 merge (`entity_configuration_contract.py`'s `primary_key_field`/
`soft_delete_field`) never executes for any entity. Every entity with a configured
primary key has been running in append-only mode: curated tables likely contain
duplicate and stale rows wherever upstream records were updated or deleted, silently,
since this code shipped.

**Proposed solution.**
1. Fix the call site: `ConfigurationRepositoryClient(environment=environment, region_name=region_name)`.
2. Narrow the `except Exception` at line 211 to `except (ConfigurationNotFoundError, ConfigurationValidationError)` so a genuine programming error fails loudly instead of degrading silently.
3. Add `transformation/tests/test_transformation_pipeline_handler.py` exercising the real handler entry point end-to-end (this test does not exist today).
4. Audit existing curated tables for duplicate/stale rows accumulated while this was broken; consider a one-time backfill/re-merge job.

**Files:** `transformation/transformation_pipeline_handler.py:180-183,211`, `connector_runtime/configuration_repository/configuration_repository.py:68-74`

---

### `SEC-1` — PII/sensitive-field masking is fully built and hardcoded off — **P0**

**Observation.** `governance/data_classification_policy.py`'s `FieldMaskingApplier` and
`EntityClassificationPolicy` (masking, tokenization, hashing) are fully implemented and
unit-tested. `transformation/transformation_pipeline_handler.py:226` constructs the
pipeline with `classification_policy=None`. `transformation/transformation_pipeline.py:361`
only applies masking `if self._classification_policy is not None` — a condition that is
never true in the wired production handler.

**Proof, not inference.** `grep -rn "EntityClassificationPolicy("` across the entire
repository returns exactly two hits, both test files:
`transformation/tests/test_transformation_pipeline.py:347` and
`governance/tests/test_data_classification_policy.py:19`. It is never constructed in
production code.

**Gap / Risk.** The masking feature is real, tested in isolation, and simply never
activated at the one production call site.

**Impact.** Every PII/SENSITIVE_PII field pulled from Salesforce, NetSuite, Sage, or
MySQL flows unmasked into curated and analytics S3 — exactly what Athena/Glue consumers
and BI tools read. This contradicts the module's own documented guarantee and is a
compliance blocker (GDPR/CCPA-class exposure) the moment real customer PII enters the
system, which will happen the moment a second tenant with different data is onboarded.

**Proposed solution.** Load a real `EntityClassificationPolicy` per entity (or a default
auto-classified one via `auto_classify_field`) in `transformation_pipeline_handler.py`
and pass it into `TransformationPipeline` instead of `None`. Treat as a pre-launch
blocker, not a backlog item — do not onboard a second tenant before this ships.

**Files:** `transformation/transformation_pipeline_handler.py:226`, `transformation/transformation_pipeline.py:361`, `governance/data_classification_policy.py`

---

## 1. Architecture

### `ARCH-1` — Multi-tenancy is implemented on one side of the pipeline and absent on the other — P0

**Observation.** `tenant_code` is validated and threaded through
`contracts/entity_configuration_contract.py`, `transformation/transformation_pipeline.py`,
`transformation/curated_layer_writer.py`, `orchestration/pipeline_trigger/pipeline_trigger_handler.py`,
and `observability/metrics_emitter.py`.

**Gap.** `connector_runtime/extraction_pipeline_handler.py` never declares `tenant_code`
as a required event field; `connector_runtime/run_lifecycle/run_lifecycle.py`,
`connector_runtime/configuration_repository/configuration_repository.py`,
`watermark_management/watermark_repository/watermark_repository.py`, and
`schema_management/snapshot_repository/snapshot_repository.py` all build DynamoDB table
keys and S3 paths with zero tenant dimension.

**Impact.** Raw data, watermarks, audit logs, and schema snapshots for every tenant
sharing an environment collide in one shared namespace — the exact hazard
`architecture/IMPROVEMENT_PLAN.md` §1.1 set out to prevent, roughly half-closed.

**Solution.** Extend the transformation-side pattern to the extraction path (see
Rollout Plan Phase 2).

**Files:** `connector_runtime/extraction_pipeline_handler.py`, `connector_runtime/run_lifecycle/run_lifecycle.py`, `watermark_management/watermark_repository/watermark_repository.py`, `schema_management/snapshot_repository/snapshot_repository.py`

---

### `ARCH-2` — Entity type registry is a hardcoded dict, contradicting the platform's own no-code-change principle — P0

**Observation.** `entity_resolution/entity_type_registry.py` defines `ENTITY_ID_TO_TYPE`,
`ENTITY_TYPE_PK_FIELD`, and `ENTITY_TYPE_SOURCES` as static module-level dicts; its own
docstring states that adding an entity requires editing all three and redeploying the
Lambda zip.

**Gap.** Directly contradicts `entity_configuration_contract.py`'s stated design
principle of "no code changes required to onboard entities." The sibling module
`entity_resolution/survivorship_policy.py` already solved this with a versioned,
dataclass-based design (`SurvivorshipPolicy`/`AttributeSurvivorshipRule`) — the
inconsistency is between two adjacent files in the same package.

**Impact.** A tenant wanting a custom entity type — normal in SaaS — requires an
engineer to edit source and redeploy, blocking self-service onboarding.

**Solution.** Replace with a DynamoDB-backed `EntityTypeRegistryClient` keyed on
`(tenant_code, entity_id)`, seeding current dict contents as the `default` tenant's
rows. Already scoped in `architecture/IMPROVEMENT_PLAN.md` §1.3 — needs execution.

**Files:** `entity_resolution/entity_type_registry.py`, `entity_resolution/entity_resolution_pipeline_handler.py`

---

### `ARCH-3` — No control plane; onboarding is engineer-run CLI scripts — P0

**Observation.** Tenant/entity registration and pipeline triggering happen exclusively
through `scripts/seed_entity_config.py`, `scripts/seed_field_mappings.py`,
`scripts/seed_schedules.py`, and `scripts/trigger_extraction.py`.

**Gap.** No API surface exists for onboarding, configuration, or run-status queries
without shell access to the platform's AWS account.

**Impact.** Every new tenant and every entity change requires engineering time — the
largest blocker to SaaS unit economics.

**Solution.** Build the control-plane API scoped in `architecture/IMPROVEMENT_PLAN.md`
§1.2 (API Gateway + Cognito + WAF) as an additive surface; CLI scripts remain for
internal/break-glass use.

**Files:** `scripts/trigger_extraction.py`, `scripts/seed_entity_config.py`; new package `connector_runtime/api/`

---

### `ARCH-4` — `tenant_code` has no formal place in the extraction event contract — P0

**Observation.** `extraction_pipeline_handler.py:190` reads `event.get("tenant_code", "demo")`
purely to tag a CloudWatch metric — it is absent from `_REQUIRED_EVENT_FIELDS` and
`_validate_event`.

**Gap.** The extraction stage has no formal notion of which tenant a run belongs to, so
nothing downstream can be tenant-scoped even in principle until the event contract
requires and validates it.

**Impact.** Blocks `ARCH-1`'s fix directly — this must land first.

**Solution.** Add `tenant_code` to the required Step Functions input schema, validate
against `TENANT_CODE_PATTERN`, and fail fast rather than silently defaulting to `"demo"`.

**Files:** `connector_runtime/extraction_pipeline_handler.py:59-61,190,219-250`, `contracts/identifier_policy.py`

---

### `ARCH-5` — Distributed circuit breaker uses an unusual instance-scoped-singleton-with-fallback pattern — P2

**Observation.** `orchestration/step_functions/extraction_retry_policy.py` combines a
DynamoDB-backed distributed retry counter with an in-process fallback if DynamoDB is
unreachable, documented in `extraction_pipeline_handler.py:64-72`.

**Gap.** Not incorrect, but the fallback degrades protection silently — see `OBS`
findings for the alarm gap on this exact fallback path (now largely closed — see
Monitoring section).

**Impact.** Low standalone; compounds with any alarm gap into days of silently degraded
cross-container circuit protection.

**Solution.** Document the fallback prominently in the module docstring; treat the
`circuit_breaker_ddb_init_failed` CloudWatch alarm as a page, not a dashboard tile.

**Files:** `orchestration/step_functions/extraction_retry_policy.py:87-99,271-299`

---

## 2. Design Patterns

*(`DP-1`, the SCD-merge bug, is documented above under "confirmed live bugs.")*

### `DP-2` — A well-designed pattern exists in one connector and was never generalized — P1

**Observation.** Sage's `sage_credential_manager.py` (Secrets Manager fetch/cache/error
handling) and `sage_errors.py` (Deterministic/Transient exception hierarchy) are clean,
reusable abstractions; `intacct_auth.py` and `x3_auth.py` both delegate to them
successfully.

**Gap.** Salesforce, NetSuite, and MySQL each independently hand-roll near-identical
Secrets Manager boilerplate and flat, connector-local exception classes with no shared
base — the proven pattern next door was never lifted out. See `DUP-2` for the full
duplication accounting.

**Impact.** Every new connector re-derives credential-handling and error-classification
logic from scratch.

**Solution.** Promote `SageCredentialManager` and the Deterministic/Transient hierarchy
into `connector_runtime/interfaces/` for all connectors to inherit.

**Files:** `connector_runtime/adapters/sage/common/sage_credential_manager.py`, `connector_runtime/adapters/sage/common/sage_errors.py`

---

### `DP-3` — Anemic error taxonomy forces hand-rolled classification in three of four connectors — P1

**Observation.** `ExtractionErrorClassification` in
`connector_runtime/interfaces/connector_interface.py` is a correct, shared
Strategy-pattern abstraction.

**Gap.** Salesforce (`BulkJobTimeoutError`), NetSuite (`NetSuiteSuiteQLRateLimitError`),
and MySQL (`MySqlIncrementalExtractorError`) are flat exceptions with no
transient/deterministic marker base, so each `classify_extraction_error()` hand-rolls
`isinstance` dispatch instead of inheriting the answer.

**Impact.** Classification logic drifts independently per connector.

**Solution.** Same fix as `DP-2` — a shared Deterministic/Transient base each
connector's exceptions inherit from.

**Files:** `connector_runtime/interfaces/connector_interface.py`, `connector_runtime/adapters/{salesforce,netsuite,mysql_rds}/*`

---

## 3. Performance

### `PERF-1` — The MySQL connector's "streaming" extractor fully buffers before it paginates — P0

**Observation.** `mysql_rds_connector.py:259` opens connections with the default
`pymysql.cursors.DictCursor`; `mysql_incremental_extractor.py:99` loops
`cursor.fetchmany(_FETCH_BATCH_SIZE)`, which the module's docstring frames as
bounded-memory streaming.

**Gap.** `DictCursor` is a buffered cursor — `cursor.execute()` already pulls the entire
result set to the client before `fetchmany` runs; the pagination loop only slices an
already-fully-materialized in-memory list.

**Impact.** Directly threatens "millions of records in one run" for any MySQL source —
a multi-million-row incremental extraction can exhaust Lambda memory regardless of
configured batch size.

**Solution.** Switch to `pymysql.cursors.SSDictCursor` (server-side/streaming cursor).

**Files:** `connector_runtime/adapters/mysql_rds/mysql_rds_connector.py:259`, `connector_runtime/adapters/mysql_rds/mysql_incremental_extractor.py:99`

---

### `PERF-2` — No MySQL connection pooling — P1

**Observation.** `mysql_rds_connector.py:196-205` opens a fresh `pymysql.connect()` per
`execute_extraction()` call.

**Gap.** No pool or RDS Proxy; concurrent Lambda invocations each hold their own
connection for the run's duration.

**Impact.** At multi-tenant scale, concurrent extraction runs across tenants risk
exhausting the source RDS instance's `max_connections`.

**Solution.** Front MySQL sources with RDS Proxy, or reuse a warm-start connection
across invocations of the same container.

**Files:** `connector_runtime/adapters/mysql_rds/mysql_rds_connector.py:196-205`

---

### `PERF-3` — Entity resolution and analytics publisher still fully materialize records in memory — P1

**Observation.** `entity_resolution_pipeline_handler.py:218,233` calls
`load_curated_records()` per contributing source and does
`all_curated_records.extend(...)`; `analytics_publisher_handler.py`'s
`_load_parquet_records()` returns a full list, then rebuilds a second full
`pa.Table.from_pylist(...)` purely for schema inference.

**Gap.** These are the two stages `architecture/IMPROVEMENT_PLAN.md` §3.4 flagged for a
DuckDB-based streaming join; that fix has not landed here, unlike the transformation-side
SCD merge which already moved to DuckDB (§3.1, confirmed done).

**Impact.** Entity resolution across multiple large sources, or a large golden-record
set, can still exhaust Lambda memory at target record counts.

**Solution.** Apply the DuckDB streaming-join pattern already proven in
`transformation/curated_utils.py`; capture schema from the streaming writer instead of
rebuilding a second full Arrow table in the analytics publisher.

**Files:** `entity_resolution/entity_resolution_pipeline_handler.py:218,233`, `analytics_publisher/analytics_publisher_handler.py`

---

### `PERF-4` — Golden and canonical record publishers bypass the shared multipart S3 writer — P1

**Observation.** `entity_resolution/golden_record_publisher/golden_record_publisher.py:162,173`
and `entity_resolution/canonical_record_publisher/canonical_record_publisher.py:213,224`
both build full Parquet bytes locally and call `self._s3.put_object(...)` directly.

**Gap.** `observability/s3_writer.S3ParquetWriter` (multipart-capable) already exists
and is adopted by `CuratedLayerWriter` and the analytics publisher — these two
publishers were not migrated.

**Impact.** Large golden/canonical outputs hit the 5GB single-PUT limit, higher memory
pressure, and full re-upload on retry.

**Solution.** Route both through `S3ParquetWriter`.

**Files:** `entity_resolution/golden_record_publisher/golden_record_publisher.py`, `entity_resolution/canonical_record_publisher/canonical_record_publisher.py`

---

### `PERF-5` — No mid-run checkpoint for Lambda's 900-second hard timeout — P1

**Observation.** `observability/lambda_utils.py` provides `check_lambda_timeout` and
`check_lambda_timeout_periodic`, which abort gracefully before a hard kill.

**Gap.** Graceful abort is not resume — no persisted mid-extraction offset exists, so a
killed run restarts from the last committed watermark.

**Impact.** Entities in the tens-of-millions-of-rows range can repeatedly reprocess
partial work rather than making true incremental progress on retry.

**Solution.** Implement checkpoint-and-resume per `architecture/IMPROVEMENT_PLAN.md` §3.5.

**Files:** `observability/lambda_utils.py`, `orchestration/step_functions/extraction_workflow.py`

---

## 4. Security

*(`SEC-1`, PII masking disabled, is documented above under "confirmed live bugs.")*

### `SEC-2` — No tenant-scoped IAM — isolation is a convention, not an enforced boundary — P0

**Observation.** `infrastructure/modules/iam/main.tf:117-125` grants the extraction
runtime role `secretsmanager:GetSecretValue` on `arn:...:secret:${var.environment}/sources/*`
— every connector's credentials for every tenant sharing that environment. S3 and
DynamoDB statements are scoped to bucket/table ARNs only, with no tenant-prefix or
partition-key condition.

**Gap.** Tenant separation exists only as an application-level string-prefix convention.

**Impact.** A bug in path construction, a compromised dependency, or a mistake in one
tenant's `connector_params` can read/write another tenant's data or exfiltrate another
tenant's source credentials. This is the central blocker to any credible multi-tenant
isolation guarantee.

**Solution.** Scope IAM by tenant before onboarding a second real tenant — per-tenant
roles assumed per run, S3 bucket-policy conditions on the `tenant_code` prefix, and
DynamoDB fine-grained access control keyed on the same field.

**Files:** `infrastructure/modules/iam/main.tf:117-125`

---

### `SEC-3` — Sage credentials are undeclared in Terraform — likely missing the deny-all resource policy — P1

**Observation.** `sage_credential_manager.py:35` reads secrets at
`{environment}/sources/sage/{product_name}/credentials`, but
`infrastructure/modules/secrets/main.tf` only declares `aws_secretsmanager_secret`/
`_secret_policy` resources for Salesforce, NetSuite, and MySQL RDS.

**Gap.** The explicit `DenyAllOtherPrincipals` policy applied to the other three secret
types is never applied to Sage — either created out-of-band or the connector is
undeployed.

**Impact.** If deployed out-of-band, Sage credentials lack the deny-policy backstop.

**Solution.** Add matching `aws_secretsmanager_secret`/`_secret_policy` resources for
both Sage products (Intacct, X3).

**Files:** `infrastructure/modules/secrets/main.tf`

---

### `SEC-4` — Secret-leak scanning is enforced client-side only, not in CI — P1

**Observation.** `.pre-commit-config.yaml` wires `detect-secrets` against
`.secrets.baseline`; `.github/workflows/ci.yml` has no equivalent stage.

**Gap.** A commit made with `--no-verify`, from a machine without pre-commit installed,
or via a bot/automation PR bypasses secret detection before merge.

**Impact.** A leaked credential can reach `main`/`staging`/`prod` undetected.

**Solution.** Add `detect-secrets scan --baseline .secrets.baseline` (or gitleaks) as a
CI job.

**Files:** `.github/workflows/ci.yml`

---

### `SEC-5` — `tenant_code` is trusted unvalidated in 3 of 5 handlers that read it — P1

**Observation.** `extraction_pipeline_handler.py:190`,
`entity_resolution_pipeline_handler.py:301`, and `analytics_publisher_handler.py:311`
all do `event.get("tenant_code", "demo")` with no pattern check.

**Gap.** `pipeline_trigger_handler.py` and `transformation_pipeline_handler.py` both
validate the same field against `TENANT_CODE_PATTERN` — inconsistent enforcement.

**Impact.** Limited today (CloudWatch dimension cardinality/cost abuse), but will matter
once these Lambdas' inputs are less trusted under a control-plane API.

**Solution.** Route `tenant_code` through the same validation in all three handlers.

**Files:** `connector_runtime/extraction_pipeline_handler.py:190`, `entity_resolution/entity_resolution_pipeline_handler.py:301`, `analytics_publisher/analytics_publisher_handler.py:311`

---

### `SEC-6` — Secrets Manager rotation is defined in Terraform but inert in every environment — P1

**Observation.** `infrastructure/modules/secrets/main.tf` gates
`aws_secretsmanager_secret_rotation` on a `*_rotation_lambda_arn` variable.

**Gap.** None of `infrastructure/environments/{dev,staging,prod}` set these variables —
`count = 0` everywhere, including prod. Matches `architecture/IMPROVEMENT_PLAN.md` §4.3,
confirmed still open.

**Impact.** Connector credentials never rotate automatically.

**Solution.** Build the rotation Lambda wrapper per §4.3, or at minimum an SNS
pre-expiry notification for connectors without programmatic rotation (Sage Intacct).

**Files:** `infrastructure/modules/secrets/main.tf`, `infrastructure/environments/{dev,staging,prod}`

---

## 5. Monitoring and Observability

> Most gaps originally flagged in `architecture/IMPROVEMENT_PLAN.md` §5 are **already
> fixed** in the current codebase: the `Stage` metrics dimension (§5.1), CloudWatch
> metrics on entity resolution/analytics publisher (§5.2), DLQ depth alarm (§5.3),
> Lambda error/duration/throttle alarms (§5.4), X-Ray `patch_all()` instrumentation
> (§5.5), saved Logs Insights queries (§5.6), and PagerDuty SNS integration (§5.8) were
> all verified present in the current code. Do not re-do this work — verify it still
> passes CI, and move on to the gaps below.

### `OBS-1` — Structlog context is bound but never cleared on warm Lambda containers — P0

**Observation.** `entity_resolution_pipeline_handler.py:146-150` and
`analytics_publisher_handler.py:157-161` call
`structlog.contextvars.bind_contextvars(run_id=..., source_id=..., entity_id=...)` but
never call `clear_contextvars()` in a `finally` block, unlike
`extraction_workflow.py:491-494`, which clears explicitly for this exact reason.

**Gap.** If a warm container's next invocation fails `_validate_event()` — which runs
*before* `bind_contextvars` — its error log carries the previous invocation's stale
`run_id`/`source_id`/`entity_id`.

**Impact.** Actively misleading during incident correlation — an on-call engineer
chasing a failed run by `run_id` can be pointed at the wrong invocation entirely.

**Solution.** Wrap both handler bodies in `try/finally: structlog.contextvars.clear_contextvars()`.

**Files:** `entity_resolution/entity_resolution_pipeline_handler.py:146-150`, `analytics_publisher/analytics_publisher_handler.py:157-161`

---

### `OBS-2` — ER and analytics handlers lack top-level structured error handling — P0

**Observation.** In both handlers, only the metrics-emission block is wrapped in
try/except — the actual business logic (load, publish, catalog registration) has no
top-level handler.

**Gap.** Compare to `extraction_workflow.py`'s `_handle_stage_failure` + DLQ enqueue, or
`pipeline_trigger_handler.py`'s explicit try/except → structured log → re-raise. An
unhandled exception here prints a raw Python traceback via the default Lambda runtime
logger, not JSON.

**Impact.** The saved Logs Insights query `failed_runs_last_24h` (`filter level =
"error"`) misses these failures entirely.

**Solution.** Give both handlers the same try/except → structured `"*_stage_failed"` log
→ re-raise wrapper used elsewhere.

**Files:** `entity_resolution/entity_resolution_pipeline_handler.py`, `analytics_publisher/analytics_publisher_handler.py`

---

### `OBS-3` — Two different correlation-ID mechanisms across pipeline stages — P1

**Observation.** Extraction and transformation thread `run_id` as an explicit per-call
kwarg; entity resolution and analytics publisher rely on `structlog.contextvars`
binding.

**Gap.** Two mechanisms for one guarantee — a future refactor is likely to drop one or
the other.

**Impact.** Increases the odds of `OBS-1`-style correlation gaps recurring.

**Solution.** Standardize on contextvars binding with mandatory `clear_contextvars()`
platform-wide.

**Files:** `observability/structured_logger.py`

---

### `OBS-4` — No end-to-end pipeline SLA metric — P1

**Observation.** `observability/metrics_emitter.py` emits per-stage `StageDurationMs`
only; nothing sums extraction-trigger to analytics-publication latency. Matches
`architecture/IMPROVEMENT_PLAN.md` §5.7, confirmed still open.

**Impact.** No way to alarm on or report against a SaaS SLA commitment (e.g., "data
refreshed within 4 hours").

**Solution.** Emit a cross-stage `PipelineEndToEndDurationMs` from
`analytics_publisher_handler.py` using the run's `started_at` from
`{env}-edl-run-audit-log`.

**Files:** `analytics_publisher/analytics_publisher_handler.py`, `observability/metrics_emitter.py`

---

### `OBS-5` — No live connector health-check — P2

**Observation.** `connector_runtime/certification/connector_certification_checklist.py`
is a static structural checklist (abstract methods exist, no stray `os.environ` access,
ID format) run once at onboarding.

**Gap.** It never opens a live connection or validates current credentials.

**Impact.** A stale/revoked credential surfaces only as a failed production run and a
DLQ entry, not a proactive signal.

**Solution.** Add a lightweight `health_check()` method to `ConnectorInterface`
performing a minimal authenticated call.

**Files:** `connector_runtime/certification/connector_certification_checklist.py`, `connector_runtime/interfaces/connector_interface.py`

---

## 6. Reusable Code / Redundancy

### `DUP-1` — Four near-identical raw layer writers, ~950 lines, ignoring the shared writer that already exists — P1

**Observation.** `salesforce_raw_layer_writer.py`, `netsuite_raw_layer_writer.py`,
`sage/common/sage_raw_layer_writer.py`, and `mysql_rds_raw_layer_writer.py` each define
near-identical `write_partition`, `write_partition_streaming`, `_records_to_parquet`,
`_partition_path`, and `_validate_stable_id`.

**Gap.** `observability/s3_writer.S3ParquetWriter` already exists and is used by
`curated_layer_writer.py` — none of these four raw writers adopt it.

**Impact.** A format/partitioning change must be applied identically in four places;
every new connector adds a fifth near-copy.

**Solution.** Extract a `RawLayerWriter` base class parameterized by source, built on
`S3ParquetWriter`.

**Files:** `connector_runtime/adapters/{salesforce,netsuite,sage/common,mysql_rds}/*_raw_layer_writer.py`, `observability/s3_writer.py`

---

### `DUP-2` — Credential-client boilerplate duplicated 3x — the fix already exists in the fourth connector — P1

**Observation.** `salesforce_auth_client.py:189-221`, `netsuite_auth_client.py:224-271`,
and `mysql_rds_credentials_client.py:120-174` are near line-for-line identical Secrets
Manager fetch/cache/error-handling boilerplate.

**Gap.** Sage already generalized this via `SageCredentialManager` (`DP-2` above) — the
pattern exists and was not reused.

**Impact.** Highest-consequence code (credential handling) maintained inconsistently
four times.

**Solution.** Promote `SageCredentialManager` into a shared
`SecretsManagerCredentialClient`; each connector supplies only its secret path and
required-key list.

**Files:** `connector_runtime/adapters/salesforce/salesforce_auth_client.py`, `connector_runtime/adapters/netsuite/netsuite_auth_client.py`, `connector_runtime/adapters/mysql_rds/mysql_rds_credentials_client.py`

---

### `DUP-3` — Golden and canonical record publishers copy-paste the same serialization/lineage logic — P2

**Observation.** Both modules independently define `_to_parquet()` and near-identical
`_serialise_decisions()`/`_emit_golden_record_lineage()`, differing by a couple of lines.

**Impact.** ~350 lines maintained twice; compounds with `PERF-4`.

**Solution.** Factor a shared serialization/lineage module, migrated to
`S3ParquetWriter` at the same time as `PERF-4`.

**Files:** `entity_resolution/canonical_record_publisher/canonical_record_publisher.py`, `entity_resolution/golden_record_publisher/golden_record_publisher.py`

---

### `DUP-4` — Five independently hand-rolled incremental query builders — P2

**Observation.** `query_builders/salesforce_soql_query_builder.py`,
`sage/products/{intacct,x3}/*_query_engine.py`, `mysql_incremental_extractor.py`, and
`netsuite_incremental_query_planner.py` each build a select + watermark-filter +
order-by clause, each with its own exception class.

**Impact.** A correctness fix to incremental-filter logic must be independently applied
in five places.

**Solution.** Introduce an `IncrementalQueryBuilder` protocol with a templated `build()`
and pluggable placeholder style.

**Files:** `connector_runtime/query_builders/salesforce_soql_query_builder.py`, `connector_runtime/adapters/sage/products/{intacct,x3}/*_query_engine.py`, `connector_runtime/adapters/mysql_rds/mysql_incremental_extractor.py`, `connector_runtime/adapters/netsuite/netsuite_incremental_query_planner.py`

---

### `DUP-5` — Handler boilerplate and test fixtures repeated across the four Lambda entrypoints — P2

**Observation.** All four handlers independently wire logger/metrics/X-Ray/env helpers;
no `conftest.py` exists under any `connector_runtime/tests/` subfolder, so 13+ test
files repeat moto S3/DynamoDB bootstrap setup.

**Impact.** Friction that scales with every new connector and pipeline stage.

**Solution.** A small `PipelineHandlerContext` helper, and a shared `conftest.py` with
reusable moto fixtures.

**Files:** `connector_runtime/tests/{mysql_rds,netsuite,sage,salesforce}/`

---

## SaaS readiness scorecard

| Dimension | Status | Note |
|---|---|---|
| Tenant data isolation | Convention only | S3/DynamoDB prefixing exists on the transformation side (`ARCH-1`); no IAM-enforced boundary (`SEC-2`) |
| Tenant configuration | Not present | `ARCH-2` — hardcoded entity type registry |
| Data separation (raw/audit/watermark) | Not present | `ARCH-1` |
| Onboarding | Manual only | `ARCH-3` |
| Scaling / throughput | Mixed | SQS buffer + DuckDB merge done; MySQL (`PERF-1`) and ER/analytics (`PERF-3`) still fully materialize |
| Licensing / usage metering | Not present | No per-tenant usage tracking exists anywhere |
| Operational support | Mostly ready | Most alarms/tracing from `IMPROVEMENT_PLAN.md` §5 already merged; `OBS-1`..`OBS-4` remain |
| Security posture | Blocking | `SEC-1` (masking off), `SEC-2` (no tenant IAM) are launch blockers |

See `architecture/MULTI_TENANT_ROLLOUT_PLAN.md` for the phased fix sequence.
