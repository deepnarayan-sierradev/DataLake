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
| `ARCH-1` | ✅ DONE (2026-07-08 pass — see note) | `tenant_code` threaded through `ConfigurationRepositoryClient` and `SchemaSnapshotRepository` (done in the original pass). **Direct re-verification on 2026-07-08 found this row was previously overstated**: `WatermarkRepository`'s DynamoDB key was still unscoped (`PK=source_id`, no tenant dimension — two tenants could silently share/overwrite one watermark), and the S3 write paths for `RawLayerWriter` (all 4 connector adapters), the analytics-publisher output, and both golden/canonical-record publishers had **no tenant_code segment at all** — a guaranteed same-key collision, not just a latent risk. All fixed in this pass: `WatermarkRepository` now uses `tenant_scoped_key()` on the `source_id` attribute (see `contracts/identifier_policy.py`); `RawLayerWriter.__init__` takes a required `tenant_code` and prefixes every partition path; `analytics_publisher_handler.py` and both record publishers prefix their S3 output with `{tenant_code}/`; `transformation/curated_utils.py::find_latest_curated_prefix()` and `curated_accumulator.py` (the cross-source ER lookup and the SCD-merge previous-state lookup) now require `tenant_code` and match `CuratedLayerWriter`'s real write path instead of silently missing it. Regression coverage: `tests/test_tenant_isolation.py` (`TestWatermarkRepositoryKeyIsolation`, `TestRawLayerWriterPathIsolation`), `analytics_publisher/tests/test_analytics_publisher_handler.py::TestTenantIsolation`, `entity_resolution/tests/test_canonical_record_publisher.py::TestTenantIsolation` |
| `ARCH-2` | ✅ DONE | `entity_resolution/entity_type_registry.py::EntityTypeRegistryClient` — DynamoDB single-table (PK=`tenant_code`), falls back to the original hardcoded dicts as seed data |
| `ARCH-3` | ✅ DONE (code-complete; **unverified against real AWS**) | `connector_runtime/api/` (6 routes) + `infrastructure/modules/control_plane/` (HTTP API + Cognito + JWT authorizer); `terraform validate` passes for `dev`. Open item: the exact claims-path an HTTP API + JWT authorizer populates at payload format 1.0 (`authorizer.claims` vs `authorizer.jwt.claims`) was not verified end-to-end against live API Gateway — the handler defensively checks both and fails closed (401) either way |
| `ARCH-4` | ✅ DONE (2026-07-08 — corrected scope) | Previously marked done on the strength of `PipelineStageContract`'s field alone, which has a `default="demo"` and is not the actual enforcement point. **Direct re-verification found `tenant_code` was still optional** (absent from `_REQUIRED_EVENT_FIELDS`, silently defaulted) in **all four** pipeline Lambda handlers, not just extraction. Fixed in all four: `connector_runtime/extraction_pipeline_handler.py`, `entity_resolution/entity_resolution_pipeline_handler.py`, `analytics_publisher/analytics_publisher_handler.py`, `transformation/transformation_pipeline_handler.py` now require `tenant_code` and always format-validate it — a missing or malformed value fails the invocation instead of silently running as `"demo"`. `transformation_pipeline_handler.py` previously had **no tenant_code format validation at all** (contradicts the `SEC-5` row below, corrected there too). DLQ replay paths (`orchestration/step_functions/run_replay_controller.py`, `orchestration/dlq_processor/dlq_processor_handler.py`) were updated in the same pass to carry `tenant_code` through, or every replay would have started hard-failing validation |
| `ARCH-5` | ⬜ NOT STARTED | P2, documentation-only recommendation; not touched in this pass |
| `ARCH-6` (new, 2026-07-08) | ✅ DONE | EventBridge schedule name collision: `orchestration/event_bridge/extraction_schedule_client.py`'s schedule name was `{source_id}--{entity_id}` with no tenant dimension, and `create_or_update_schedule()` tries `update_schedule` first — a second tenant onboarding the same source/entity would silently overwrite the first tenant's live schedule (cron, connector_params, embedded tenant_code). Fixed: schedule name is now `{tenant_code}-{source_id}--{entity_id}`. Regression test: `orchestration/tests/test_extraction_schedule_client.py::TestScheduleNameConstruction::test_schedule_name_prefixed_by_tenant_code` |
| `ARCH-7` (new, 2026-07-08) | ✅ DONE | Circuit breaker cross-tenant sharing: `orchestration/step_functions/extraction_retry_policy.py`'s circuit-breaker key was `source_id:entity_id`, so one tenant's consecutive failures on a shared connector type could open the circuit and block another tenant's healthy runs. Fixed: key is now `tenant_code:source_id:entity_id`. Regression test: `tests/test_tenant_isolation.py::TestCircuitBreakerTenantIsolation` |
| `ARCH-8` (new, 2026-07-08) | ✅ DONE | SQS FIFO `MessageGroupId` / Step Functions execution-name collision: `connector_runtime/api/control_plane_handler.py`'s `MessageGroupId` and `orchestration/pipeline_trigger/pipeline_trigger_handler.py`'s execution-name truncation (`[:80]` from the tail) both lacked a tenant dimension — the truncation bug could drop the disambiguating tick entirely for long source/entity IDs, silently no-opping a second tenant's trigger as `ExecutionAlreadyExists`. Fixed: `MessageGroupId` now includes `tenant_code`; execution names are built by `_build_execution_name()`, which truncates the tenant/source/entity prefix but never the trailing tick |
| `ARCH-9` (new, 2026-07-08) | ✅ DONE | DLQ replay dropped `tenant_code`: both `run_replay_controller.py` (`DlqEntry`) and `dlq_processor_handler.py` (`DLQMessage`) omitted `tenant_code` when rebuilding the replay's Step Functions input, audit-log write, and SNS notification — a replayed run would have silently defaulted to `"demo"`. Fixed: `tenant_code` is now a required `DlqEntry` field and a `DLQMessage` field (default `"demo"` for messages predating this fix), threaded through every downstream payload. Regression tests: `orchestration/tests/test_run_replay_controller.py::test_input_payload_carries_tenant_code`, `orchestration/tests/test_dlq_processor_handler.py::test_replay_preserves_tenant_code` |
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
| `SEC-5` | ✅ DONE (2026-07-08 — corrected) | `tenant_code` validated in all **four** handlers: `extraction_pipeline_handler.py`, `entity_resolution_pipeline_handler.py`, `analytics_publisher_handler.py`, and `transformation_pipeline_handler.py`. This row previously omitted `transformation_pipeline_handler.py` and, on direct re-verification, that handler had **no `tenant_code` format check anywhere** — the claim was false, not just incomplete. See `ARCH-4` above; both findings were fixed together since format-validation and required-ness needed the same `_validate_event` change per handler |
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
| `ARCH-10` (new, 2026-07-08) | ⬜ NOT STARTED | Found during the same tenant-isolation sweep: `entity_resolution/resolution_config/resolution_config_registry.py` (survivorship/match-rule policy storage, `entity-resolution/{entity_type}/survivorship_{version}.json`) is **still global, not tenant-scoped** — this resolves `architecture/MULTI_TENANT_ROLLOUT_PLAN.md`'s previously-open "Verify" item on §1.4: it was never built, not merely unverified. Deferred: needs a new tenant-scoped registry client mirroring `ARCH-2`'s design (DynamoDB-backed or tenant-prefixed S3, default-tenant seed) — design-sized work, not a one-line key fix, and no live second tenant exists yet to need per-tenant survivorship rules |
| `ARCH-11` (new, 2026-07-08) | ⬜ NOT STARTED | `transformation/field_mapping/field_mapping_registry.py` (`field-mappings/{source_id}/{entity_id}/{mapping_version}.json`) is likewise not tenant-scoped. Same deferral rationale as `ARCH-10` — bundle both into one follow-up phase, same registry-client design work |
| `ARCH-12` (new, 2026-07-08) | 🟡 PARTIAL | `ConfigurationRepositoryClient`'s DynamoDB backend has the same `PK=source_id/SK=entity_id` (no tenant dimension) pattern `WatermarkRepository` had — but the read-side `_enforce_tenant_match` guard means it manifests as a **409 Conflict blocking Tenant B's onboarding** if both tenants pick the same connector/entity, not a silent data leak (lower severity than the `WatermarkRepository` bug `ARCH-1` fixed). Deferred to the same follow-up pass as `ARCH-10`/`ARCH-11` — apply `tenant_scoped_key()` once that pass is underway |
| `ARCH-13` (new, 2026-07-08) | ⬜ NOT STARTED (deliberately deferred) | Found by an independent adversarial audit of the `ARCH-1` fix: `governance/lineage_record.py`'s `LineageEmitter.emit()`/`.load()` write/read lineage records at `lineage/{entity_id}/{run_id}/{stage}-lineage.json` with **no tenant_code segment or field anywhere** — `.load()` performs zero tenant check, weaker than even the app-level-guard pattern. Live and reachable from both `transformation/transformation_pipeline.py::_emit_transformation_lineage` and `entity_resolution/publishing_shared.py::emit_golden_record_lineage` (used by both record publishers). Contradicts `architecture/IMPROVEMENT_PLAN.md`'s own documented design (`{tenant_code}/lineage/{entity_id}/...`). **Not a same-key overwrite** — `run_id` is globally unique (timestamp+uuid4), so two tenants' lineage records land at different paths and neither corrupts the other — but two tenants' lineage is interleaved under one shared, unscoped `lineage/` prefix with no tenant boundary an IAM policy or a careless `list_objects_v2` scan could rely on. Deferred as lower severity than the `ARCH-1` findings (no data corruption risk), but tracked here rather than silently dropped |
| `ARCH-14` (new, 2026-07-08) | ⬜ NOT STARTED (deliberately deferred) | Same audit, same pattern: `transformation/transformation_pipeline.py::_write_quality_report` (`quality-reports/{source_id}/{entity_id}/{run_id}/quality-report.json`) has no tenant_code. Same risk profile as `ARCH-13` (interleaved-not-colliding, `run_id` prevents overwrite) — deferred for the same reason, tracked for the same reason |
| `ARCH-15` (new, 2026-07-08 — real bug, unrelated to tenant scoping) | ✅ DONE | Found by the same audit while re-verifying `ARCH-7` (circuit breaker): `orchestration/step_functions/extraction_workflow.py`'s circuit-breaker guard check (`is_circuit_open`/`consecutive_failures`) omitted `entity_id` entirely (silently defaulting to `""`), while `record_success`/`record_failure` correctly passed the real `entity_id`. Since real `entity_id` values are always non-empty, the guard's key (`{tenant}:{source}:`) could **never** match the key failures were actually recorded under (`{tenant}:{source}:{entity}`) — the circuit breaker could not open in production regardless of how many real extraction failures occurred, for any tenant. This predates the `ARCH-1`/`ARCH-7` tenant-scoping work (the bug is in the pre-existing `entity_id` wiring, not the added `tenant_code` wiring) and was masked by the original circuit-breaker test also omitting `entity_id` symmetrically on both the record and check sides. Fixed: the guard now passes `entity_id` to both calls, matching `record_success`/`record_failure`. Regression test: `orchestration/tests/test_extraction_workflow.py::TestCircuitBreakerIntegration::test_failures_for_a_different_entity_do_not_open_the_circuit` |
| `ARCH-16` (new, 2026-07-08 — pre-go-live fix) | ✅ DONE | `orchestration/event_bridge/extraction_schedule_client.py::_build_schedule_name()` (line ~341). Superseded `ARCH-6`'s partial fix: the schedule name was `{tenant_code}-{source_id}--{entity_id}` — a single hyphen between `tenant_code` and `source_id` could still collide when either field itself contained a hyphen (e.g. `tenant="acme"`/`source="corp-salesforce"` vs. `tenant="acme-corp"`/`source="salesforce"`), and `create_or_update_schedule()` is update-first, so this was a silent cross-tenant schedule clobber, not cosmetic. Fixed: all three components now join on the same `--` separator (`_SCHEDULE_NAME_SEP`). Also newly handles EventBridge Scheduler's 64-character name cap: names that would exceed it are deterministically collapsed to a truncated prefix + a `_SCHEDULE_NAME_HASH_LEN`-char SHA-256 content-hash suffix (never a naive slice, which could itself collide two distinct long-id tuples) |
| `ARCH-17` (new, 2026-07-08 — pre-go-live fix) | ✅ DONE | `TriggerMessage.tenant_code` (`orchestration/pipeline_trigger/pipeline_trigger_handler.py:86`) and `DLQMessage.tenant_code` (`orchestration/dlq_processor/dlq_processor_handler.py:71`) no longer default to `"demo"` — both are now required Pydantic fields (`Field(..., min_length=2, max_length=48)`). Previously a message that omitted `tenant_code` (malformed, truncated, or attacker-supplied) would silently start a real Step Functions execution / DLQ replay under the `"demo"` tenant instead of failing validation (OWASP A01 — broken access control via an implicit, attacker-reachable default identity) |
| `ARCH-18` (new, 2026-07-08 — pre-go-live fix) | ✅ DONE | `EdlRunAuditLog`'s `source-entity-time-index` GSI hash key (`source_entity_key`) is now tenant-scoped as `{tenant_code}#{source_id}#{entity_id}` (`orchestration/dlq_processor/dlq_processor_handler.py:216-224`) instead of the unscoped `source_id#entity_id`, which let two tenants' runs against the same source/entity collapse onto one GSI partition (OWASP A01 — one tenant's audit query could see another tenant's run history). `connector_runtime/run_lifecycle/run_lifecycle.py::_serialise_contract` (line ~379) now also populates `source_entity_key`/`started_at` for **every** run, not just DLQ-routed failures — previously only `dlq_processor_handler`-written items carried these attributes, so a source/entity run-history query silently omitted every successful run |
| `ARCH-19` (new, 2026-07-08 — pre-go-live fix) | ✅ DONE | `transformation/transformation_pipeline.py::_register_curated_catalog` (lines 733-813). Curated Glue table name is now tenant-scoped: `{tenant_code}_{entity_id}_{domain}_curated` (line 751-754) — previously unscoped, so two tenants running the same `entity_id`/`domain` registered the same Glue table and the second tenant's `register_dataset()` call silently overwrote the first tenant's table `Location` (cross-tenant Athena catalog clobber, not just a naming collision). Same function also now registers the run's `curated_date` partition via `glue_client.create_partition()` / `update_partition()` on `AlreadyExistsException` (lines 787-806) — previously the table declared `partition_keys=("curated_date",)` but no partition value was ever registered, so partitioned Athena queries (including implicit ones most BI tools issue) returned zero rows until a manual `MSCK REPAIR TABLE` |
| `ARCH-20` (new, 2026-07-08 — pre-go-live fix, latent) | ✅ DONE (latent — stage not deployed, no Lambda handler exists yet) | `infrastructure/modules/orchestration/main.tf`'s `LoadServingStore` state (`_serving_store_task_json`, line ~29-51) now threads `"tenant_code.$" = "$.tenant_code"` through its `Parameters`, and `transformation/serving_store_loader.py::ServingStoreLoader.load()` now requires `tenant_code` and tenant-scopes its target MySQL table name (`{tenant_code}_{table_name}`, validated against `_SAFE_TABLE_PATTERN`) before any DDL/DML is built. Latent — `serving_store_loader_lambda_arn` is unset in every environment today, so the state machine takes the `Pass` (no-op) branch (`_serving_store_pass_json`); no Lambda handler exists for this stage yet |
| `SEC-7` (new, 2026-07-08 — pre-go-live fix) | ✅ DONE | `transformation/transformation_pipeline.py::_classify_pass_through_entity` (lines 566-597), wired into `execute()` around line 249. Pass-through entities (no field-mapping rule set registered — canonical field names equal raw field names) now get the same auto-classification PII/SENSITIVE_PII masking (`build_auto_classification_policy()`) as mapped entities, by peeking one record off the raw iterator to enumerate field names before restoring it to the stream. Previously a pass-through entity skipped classification entirely purely because no mapping rule set existed for it (OWASP A01 — PII masking must not be bypassable just by not registering a mapping) |
| `PERF-6` (new, 2026-07-08 — pre-go-live fix) | 🟡 PARTIAL | `connector_runtime/adapters/netsuite/netsuite_connector.py` (`_MAX_SUITEQL_OFFSET = 100_000`, lines 84-109, enforced at line 241). SuiteQL offset/limit pagination now hard-stops with an actionable error before requesting `offset > 100,000` — NetSuite's real, undocumented-until-hit pagination ceiling — instead of failing unpredictably past that point on every retry (permanently wedging the entity). **Not fixed:** the real remedy, keyset pagination on a monotonic column instead of offset/limit, is deferred; the interim guidance is to tighten the watermark increment so a single run's result set stays under 100,000 rows |
| `PERF-7` (new, 2026-07-08 — found during today's audit, not fixed) | ⬜ NOT STARTED | `connector_runtime/configuration_repository/configuration_repository.py::list_configs_for_tenant` (line 185, `.scan()` at line 216) and `connector_runtime/api/control_plane_handler.py::_handle_list_runs` (line 481, `.scan()` at line 504) both implement tenant-scoped listing as a full DynamoDB table `Scan` with a `FilterExpression`, because neither `EdlEntityExtractionConfig` nor `EdlRunAuditLog` has a tenant-keyed GSI (`infrastructure/modules/metadata_persistence/main.tf` — `entity_extraction_config` has no GSI at all, lines 160-193; `run_audit_log`'s only GSI is `source-entity-time-index`, keyed by `source_entity_key`, not bare `tenant_code`). Cost and latency scale with total table size, not the calling tenant's slice — fine at today's single-digit-tenant dev scale, a real problem once tenant/entity count grows. Related to the already-deferred `ARCH-12` (same table, different angle: that finding is about key-level tenant isolation, this one is about query-cost scaling) |
| `PERF-8` (new, 2026-07-08 — found during today's audit, not fixed) | ⬜ NOT STARTED | Full in-memory materialization risk for very large entities: `entity_resolution/entity_resolution_pipeline_handler.py`'s cross-source loader streams each source's curated Parquet via DuckDB (`PERF-3`) but still `all_curated_records.extend(source_records)`s (line 433) every source's full record list into one combined Python list before matching — a documented tradeoff (see the function's own comment, lines 407-415) because `record_blocker.py`/`match_rule_engine.py`'s public contract requires `list[dict[str, Any]]`, not an iterator. Similarly, `transformation/transformation_pipeline.py::_load_raw_records` (lines 601-603) fully materializes a raw prefix via `list(_iter_raw_records_batched(...))` on the standard path (taken whenever a quality policy, masking, or SCD accumulator is configured — i.e. most real entities). Neither has hit a real ceiling at today's data volumes, but both are one large-entity onboarding away from an OOM |
| `PERF-9` (new, 2026-07-08 — found during today's audit, not fixed) | ⬜ NOT STARTED | `orchestration/event_bridge/extraction_schedule_client.py` sets `FlexibleTimeWindow` to `_FLEXIBLE_WINDOW_OFF` (`{"Mode": "OFF"}`, line 78, applied at line 206) for every schedule — no jitter. Every tenant/source/entity schedule using a round cron boundary (e.g. top-of-hour) fires at the exact same instant, with no `FlexibleTimeWindow` to spread invocations. Fine at today's tenant count; a thundering-herd risk (Lambda concurrency, DB connection bursts) once many tenants share common cron boundaries |
| `PERF-10` (new, 2026-07-08 — found during today's audit, not fixed) | ⬜ NOT STARTED | `EdlWatermarkRepository`'s only GSI (`environment-watermark-index`, `infrastructure/modules/metadata_persistence/main.tf` lines 52-56) is hash-keyed on `environment` — a 3-value domain (`dev`/`staging`/`prod`). Every watermark row in an environment lands in one GSI partition regardless of tenant or entity count, a hot-partition design that doesn't scale with tenant/entity growth even though the base table's own key (`ARCH-1`) is now correctly tenant-scoped |
| `SEC-8` (new, 2026-07-08 — found during today's audit, not fixed) | ⬜ NOT STARTED | `connector_runtime/api/control_plane_handler.py::_handle_create_tenant` (`POST /tenants`, lines 229-238) accepts any authenticated caller — the handler's own docstring says so explicitly: "There is no existing tenant to authorize against yet ... this route only requires a valid authenticated caller (any authenticated identity) rather than a tenant_code match. Promoting this to a platform-admin-scoped claim is tracked as follow-up work once an admin authorizer scope exists." Any Cognito-authenticated user (of any existing tenant, once multi-tenant self-service is live) can provision new tenants today |
| `PERF-11` (new, 2026-07-08 — found during today's audit, not fixed) | ⬜ NOT STARTED | `analytics_publisher/analytics_publisher_handler.py` loads all golden records into one list (`golden_records`, line 262) and then builds a second full in-memory list comprehension (`analytics_records`, lines 276-279) to strip internal ER fields before writing — two full copies of the entity type's golden-record set resident at once, in a Lambda configured at `memory_size_mb = 512` (`infrastructure/environments/dev/main.tf:356`). No streaming path today; fine for current dev data volumes (see `docs/PLATFORM_STATUS.md`'s Live Data table), a risk once golden-record counts grow |
| `OBS-6` (new, 2026-07-08 — found during that day's audit, not fixed; **count re-verified 2026-07-09**) | ⬜ NOT STARTED | Once `.github/workflows/ci.yml`'s `typecheck` job was fixed to stop crashing on the bare-`mypy .` duplicate-module collision (scoped invocation now: `mypy -p connector_runtime -p transformation -p entity_resolution -p analytics_publisher -p orchestration -p observability -p watermark_management -p schema_management -p contracts -p governance`), it surfaces pre-existing type errors. Originally reported as 75 errors across 16 files on 2026-07-08; **re-running the identical command on 2026-07-09 against today's working tree gives `Found 71 errors in 15 files`** — the drop tracks incidental fixes in files this session's `INFRA-*` pass also touched (e.g. `watermark_repository.py`, `entity_type_registry.py`, `quality_policy_evaluator.py`), not a deliberate remediation effort. Treat the exact count as a moving target that drifts with unrelated changes, not a fixed backlog size — re-run the command above to get the current number rather than trusting either figure. Confirmed pre-existing (not net-new debt) via `git show HEAD:<file> | mypy -` spot-checks on the affected files. The CI type-check job will report red on this debt until it's separately remediated — tracked here rather than fixed incidentally, per root `CLAUDE.md`'s existing warning about not conflating this with the known untyped-test-fixture noise. **Note:** root `CLAUDE.md` itself still cites the original 75/16 figure as of this writing — stale relative to this row, not yet corrected there |
| `INFRA-1` (new, 2026-07-09 — found during `dev`'s first live deploy, real bug) | ✅ DONE | `AWS_REGION` was set explicitly in Lambda `environment.variables` in two modules — a reserved key that makes `CreateFunction`/`UpdateFunctionConfiguration` fail outright (`InvalidParameterValueException: ... contains reserved keys`). Found in `orchestration/main.tf`'s `aws_lambda_function.pipeline_trigger` and `control_plane/main.tf`'s `aws_lambda_function.control_plane`; every other Lambda module already handled this correctly with a documentation comment instead of setting the value. Fixed: removed the line in both modules. Neither `terraform validate` nor code review caught this — only a real `apply` did |
| `INFRA-2` (new, 2026-07-09 — found during `dev`'s first live deploy, real bug) | ✅ DONE | `metadata_persistence/main.tf`'s `extraction_failure_dlq` queue had `visibility_timeout_seconds = 30`, less than the DLQ processor Lambda's 60s timeout (`orchestration/main.tf`) — AWS rejects `CreateEventSourceMapping` outright for this combination (`Queue visibility timeout: 30 seconds is less than Function timeout: 60 seconds`). Fixed: raised to 300s (margin for retries, not the bare minimum) |
| `INFRA-3` (new, 2026-07-09 — found during `dev`'s first live deploy, real bug) | ✅ DONE | `make lambda-package` is not byte-reproducible — `pyproject.toml` pins dependency *ranges* (e.g. `pydantic>=2.7,<3.0`), not exact versions, so two consecutive builds with no source change produced different SHA-256 hashes live during this deploy. Because `lambda-upload` depends on `lambda-package` in the `Makefile`, running them as two separate commands (as `docs/DEPLOYMENT_GUIDE.md` previously instructed) silently uploads a different artifact than the one whose hash was copied into `terraform.tfvars`. Fixed: `docs/DEPLOYMENT_GUIDE.md` and `infrastructure/CLAUDE.md` now mandate `make lambda-deploy` as a single command, never chained by hand |
| `INFRA-4` (new, 2026-07-09 — found during `dev`'s first live deploy, real bug) | ✅ DONE | `make lambda-deploy`'s `terraform apply` only ever targeted `module.lambda_pipeline` (the extraction Lambda's module), despite `docs/DEPLOYMENT_GUIDE.md` explicitly claiming it updates all eight Lambda functions (extraction, transformation, entity-resolution, analytics-publisher, control-plane, pipeline-trigger, dlq-processor, credential-expiry-notifier). Following the documented "convenience" path would have silently left seven of eight Lambdas running stale code after every future code change — including, ironically, the very pre-go-live fixes (`DP-1`/`SEC-1`/`ARCH-1`..`ARCH-20`) this file documents. Fixed: the `Makefile` target now lists all eight `aws_lambda_function` resources explicitly |
| `INFRA-5` (new, 2026-07-09 — documented gotcha, not a code bug) | ⬜ N/A | A pending change in one module (`metadata_persistence`, from `INFRA-2`'s fix) caused `terraform plan` to defer `data.aws_region`/`data.aws_caller_identity`/`data.aws_vpc` reads to apply-time in every module consuming its outputs, conservatively forcing 8 unrelated `must be replaced` diffs (4 security groups, 4 Lambda permissions) across `lambda_pipeline`/`transformation_lambda`/`entity_resolution_lambda`/`analytics_publisher_lambda` — none of which actually needed to change. Mitigation applied: landed the `INFRA-2` fix via a `-target`'d apply first, clearing the cascade before the full-environment plan. Documented as a standing gotcha in `infrastructure/CLAUDE.md` for any future small module-level fix |
| `INFRA-6` (new, 2026-07-09 — process gap, not a code bug) | ⬜ N/A | The `dev` account had six categories of orphaned resources (SQS queues encrypted with a KMS key already in `PendingDeletion` state, 5 unset placeholder Secrets Manager secrets scheduled for deletion, 7 CloudWatch Logs query definitions, 1 X-Ray group, 1 Glue catalog resource policy, 1 EventBridge Scheduler group) left over from an earlier deployment torn down by deleting only the big, visible resources (S3 buckets, Lambda functions, IAM roles, DynamoDB tables, the Terraform state bucket) instead of via `terraform destroy`. Blocked the first `apply` with `AlreadyExists`/`Conflict` errors. Cleaned up with the user's explicit per-resource sign-off; Terraform recreated all of them cleanly. A pre-flight orphan check (Phase 1, Step 1.6) is now documented in `docs/DEPLOYMENT_GUIDE.md` and `infrastructure/CLAUDE.md` for `staging`/`prod` bootstrap |
| `INFRA-7` (new, 2026-07-09 — documentation only) | ✅ DONE | Two stale claims corrected, both contradicted by direct verification against `dev`'s real, applied state: (1) `infrastructure/CLAUDE.md` claimed `staging`/`prod` had 7 pre-existing `terraform validate` errors — both validate cleanly as of today (the underlying issue was actually fixed by commit `138b692` on 2026-07-08; the doc was never updated to say so). (2) `infrastructure/CLAUDE.md` and `docs/DEVELOPER_GUIDE.md` claimed 3 of 5 DynamoDB tables "must be created manually" — `terraform state list` after a clean `dev` apply shows all 5 as genuine Terraform-managed `aws_dynamodb_table` resources. `docs/DEVELOPER_GUIDE.md` had already flagged this exact contradiction as "unresolved, verify before applying" — now resolved in favor of the code |
| `INFRA-8` (new, 2026-07-09 — found running a real pipeline, pre-existing) | ✅ DONE | Python-2-only `except A, B:` syntax (no parens) — 4 occurrences across 3 files (`orchestration/step_functions/extraction_workflow.py` ×1, `transformation/quality_evaluation/quality_policy_evaluator.py` ×1, `connector_runtime/certification/connector_certification_checklist.py` ×2) — is valid under PEP 758 (Python 3.14, this repo's local dev/tooling version) but a hard `SyntaxError` on the deployed Lambda runtime — broke every extraction invocation with `Runtime.UserCodeSyntaxError`. Fixed by parenthesizing all 4 occurrences (re-verified 2026-07-09: all 4 now correctly parenthesized in the working tree); `pyproject.toml`/ruff/mypy's declared Python version corrected from 3.14 to 3.13 (the actual Lambda target) so this class of bug is caught locally going forward |
| `INFRA-9` (new, 2026-07-09 — found running a real pipeline, pre-existing) | ✅ DONE | 4 of 8 Lambdas ran `python3.12`, the other 4 `python3.13`, all sharing one dependency bundle built for a single Python ABI — 3 of the `python3.12` functions import `pydantic` (compiled Rust core, ABI-specific). Standardized all 8 to `python3.13` (PyPI has no `pyarrow` wheel for 3.14 yet, so that runtime isn't viable regardless) |
| `INFRA-10` (new, 2026-07-09 — found running a real pipeline, pre-existing) | ✅ DONE | An entity's first-ever incremental run used `extraction_window_days` (capped at 365 by `contracts/entity_configuration_contract.py`) as its lookback — any entity with no watermark and no source-record changes in that window silently extracted 0 rows on first run, easily mistaken for a credentials/connectivity failure. Fixed in `WatermarkRepository.compute_extraction_window()`: first run now always backfills from epoch, matching what `initialise_watermark()` already assumed; `extraction_window_days` no longer governs first-run behavior at all |
| `INFRA-11` (new, 2026-07-09 — real bug in this session's own change) | ✅ DONE | Registered two new entity types as `sales_contract`/`contract_term` (underscores) — `entity_resolution/resolution_config/resolution_config_registry.py`'s `_validate_entity_type` requires `^[a-z][a-z0-9\-]{0,63}$` (hyphens only). Renamed to `sales-contract`/`contract-term` throughout (registry, `config/entity_resolution/` folder names, republished S3 configs) |
| `INFRA-12` (new, 2026-07-09 — found running a real pipeline, pre-existing) | ✅ DONE | `scripts/seed_entity_config.py` set `primary_key_field` to the raw source field name (`"Id"`) instead of the canonical post-mapping name (e.g. `account_id`) for every SCD-merge entity, including `salesforce-account`/`salesforce-contact`/`mysql-rds-contracts` — pre-existing, not introduced this session. Silent because `CuratedAccumulator`'s DuckDB path skips real merge logic entirely when no previous curated partition exists (first run only); any entity's *second* run hits the Python fallback merge, which correctly enforces `pk_field` against canonical field names and found 100% of records missing their PK, dropping every row. No prior session had ever run any entity twice, so this had never been observed. Fixed all 6 affected entities' `primary_key_field`. Not fixed, lower urgency: `duckdb` is absent from the Lambda dependency bundle (`Makefile`'s `lambda-package` pip list) — every merge always takes the Python fallback, never the intended DuckDB-accelerated path documented in `PERF-3` |

Also reconfirmed by this deployment, immediately beforehand and independently of this document's
own prior claim: both P0 "confirmed live bugs" below (`DP-1`, `SEC-1`) are genuinely fixed in the
code that actually shipped to `dev` — verified directly against the exact call sites
(`transformation_pipeline_handler.py:200-203`, `transformation_pipeline.py:231-236`), not
re-derived from this file.

**Phase 7 (launch readiness):**

| Item | Status |
|---|---|
| Automated cross-tenant isolation test | ✅ DONE — `tests/test_tenant_isolation.py` (6 passing assertions across S3, DynamoDB app-level guards, the tenant-partitioned entity-type-registry table, and the control-plane API's 404-not-403 behavior; Secrets Manager isolation is out of scope today and explicitly tracked via a skipped placeholder test, not silently omitted) |
| Runbook update | ✅ DONE — `docs/PRODUCTION_INCIDENT_RUNBOOK.md` §"Suspected Cross-Tenant Data Incident" |
| `dev` environment deployed | ✅ DONE (2026-07-09) — 277 resources, all eight Lambdas, control plane (Cognito + API Gateway), full pipeline. `INFRA-8`/`INFRA-9`/`INFRA-10`/`INFRA-12` above are each an actually-observed failure from running the pipeline for real against this deployment — confirmed, not a doc artifact: Salesforce and MySQL RDS credentials were populated and the extraction → transformation → entity resolution → analytics pipeline ran end-to-end, pulling 34 real Salesforce accounts and 36,023 real MySQL contract rows, with Athena returning real query results afterward. `docs/PLATFORM_STATUS.md` previously stated "no pipeline has run yet," left over from before this verification pass — now corrected there too. Sage Intacct, Sage X3, and NetSuite still have empty credential shells and have not run. The control-plane API's live JWT claims-path (`ARCH-3` — no login/token round-trip against the deployed Cognito pool) remains separately unverified. |
| Pilot tenant onboarding (1 week, live traffic) | ⬜ NOT STARTED — a deployed environment now exists (see row above); still requires real source credentials and an actual pilot tenant |
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

> This table reflects the **original 2026-07-07** snapshot, before the `ARCH-1`/`ARCH-4`/`ARCH-6`
> through `ARCH-9` fixes above. Read the "Implementation status" table at the top of this doc for
> current status — as of 2026-07-08, tenant data isolation and data separation are code-complete
> for every guaranteed-collision resource, with `ARCH-10`–`ARCH-14` deliberately deferred
> (design-sized registry work, or interleaved-but-non-colliding audit artifacts — see their
> entries above for why each is lower severity than what `ARCH-1` fixed) and `SEC-2` (IAM
> enforcement) the only genuinely open item in this row.

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
