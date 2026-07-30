# Platform Evolution — Requirements Specification & Architecture Assessment

**Version:** 1.0 (Draft)
**Date:** 2026-07-23
**Status:** For review
**Scope:** The `DataLake` platform (extract/load/transform data plane) and the
`enterprise-platform` repos (`datalake-config-service` control plane + `CST-UI` React console).

This document specifies the new capabilities requested — **Relationship Resolution & Digital
Twin**, **Semantic Layer**, **Conversational AI Agent with a verification loop**, **Dashboards &
Reporting**, a deeper **Permissions/RBAC** model, and the **config-UI gaps** (data-quality rules,
PII/classification policy, data catalog) — and grounds them in an evidence-based assessment of the
current codebase across seven engineering criteria.

---

## 0. Guiding Principles & Constraints

These are binding on every requirement in this document.

1. **No existing functionality may break.** The current extraction → transformation → entity
   resolution → analytics → serving pipeline, the control-plane API, and the config console must
   keep working throughout. New capabilities are additive layers or in-place internal
   redesigns behind unchanged stage contracts.
2. **Redesign is allowed; backward compatibility is not required.** Where the current
   implementation cannot carry the new features (notably the in-memory entity-resolution engine —
   see PERF-06..PERF-12), the internals are to be **rebuilt set-based from the ground up**. The
   *contract* each stage exposes (its Step Functions input/output shape) is preserved so the
   pipeline topology is unchanged; the *implementation* behind it is replaced. No dual-path /
   legacy-mode code is to be retained.
3. **Multi-tenant, SaaS-first.** Every new store, endpoint, and job is tenant-scoped through the
   single-sourced `contracts/identifier_policy.py` (`tenant_scoped_key()`, `TENANT_CODE_PATTERN`).
   No new hand-rolled tenant-key construction.
4. **Every new endpoint is authenticated and authorized.** No unprotected data routes; per-capability
   permission checks; tenant-path/claim cross-check; OWASP category cited in security-relevant code.
5. **Every new pipeline stage / job / endpoint is observable.** Structured logs with correlation
   ids, emitted-and-alarmed metrics, audit records, and a trace span — no "dead" alarms (see OBS-01).
6. **Reuse the shared seams; do not multiply boilerplate.** New work extends existing registries,
   the shared contracts layer, the (to-be-built) handler scaffold, and the (to-be-built) generic
   config-service base — it does not copy the per-capability/ per-handler patterns 6× more.
7. **Implementation code style:** follow the repo house rules — banned identifiers
   (`helper`/`util`/`common`/`manager`), OWASP-category comments on security code, the canonical
   Lambda-handler pattern — and **keep inline comments/docstrings to one line at most; do not add
   explanatory prose above every method/class/property.**

### How this document maps to the seven review criteria

| Criterion | Where addressed |
|---|---|
| Architecture | Part A §A, Part B (target architecture), each feature's *Architecture & Integration* |
| Design Patterns | Part A §B, Part B §B2, each feature's *Design & Patterns* |
| Performance | Part A §C, Part B §B5, each feature's *Performance* |
| Security | Part A §D, each feature's *Security & OWASP* |
| Monitoring & Observability | Part A §E, each feature's *Observability* |
| Reusable Code / Redundancy | Part A §F, Part B §B2, each feature's *Reuse* |
| OWASP | Part A §D (per-endpoint matrix + findings mapped to A01–A10) |

---

# PART A — Repository Assessment

Legend — Severity: **H** High / **M** Medium / **L** Low. Repo: **DL** = DataLake, **EP** =
enterprise-platform. Every finding cites a real source location.

## A. Architecture

**Overall:** DL is a mature, well-factored, contracts-first codebase with strong extensibility
seams (registries, template-method ABCs). EP is cleanly layered (router→service→repository→schema)
with a real DI composition root. The load-bearing risks are (1) EP has **no infrastructure-as-code
and no CI**, and (2) the tenant-isolation mechanism is **inconsistent across stores** and **not
IAM-enforced anywhere**.

| ID | Sev | Repo | Finding | Evidence |
|---|---|---|---|---|
| ARCH-01 | — | DL | **(Positive)** Clean shared contracts layer, zero deps on feature modules, imported everywhere; ID/tenant validation single-sourced with no regex drift | `contracts/identifier_policy.py`, `contracts/*_contract.py` |
| ARCH-02 | L | DL | **(Positive)** `tenant_code` is a required, fail-closed field in every handler (no silent `demo` default at boundary) | all `*_handler.py`, `TriggerMessage` |
| ARCH-03 | M | DL | Tenant isolation mechanism differs per store: key-scoped (watermark, serving-store-config, entity-type-registry) vs app-guard-only (`entity_extraction_config`, `run_audit_log`). S3 layers (raw/curated/analytics) are all tenant-prefixed at the write path but **not** IAM/bucket-policy-enforced. **(Verified correction: raw S3 *is* tenant-prefixed — `raw_layer_writer.py:406-413` `{tenant_code}/{source}/{entity_id}/…`, regression test ARCH-1/RAW-1; the repo's `docs/PIPELINE_FLOW.md:131` and `PLATFORM_STATUS.md` are STALE, still calling raw "not isolated".)** Nothing IAM-enforced anywhere | `raw_layer_writer.py:406-413`, `tests/test_tenant_isolation.py`, `configuration_repository.py:290-305` |
| ARCH-04 | M | DL | `entity_resolution` imports transformation internals (`find_latest_curated_prefix`, `load_curated_records_duckdb`, `source_id_to_domain`) directly — cross-feature coupling with no interface | `entity_resolution/entity_resolution_pipeline_handler.py:78-84` |
| ARCH-05 | — | DL | **(Positive)** Decorator-based plugin registries with duplicate-guards make adding a source/serving-engine a localized change | `connector_runtime/registry.py`, `serving_store/registry.py` |
| ARCH-06 | M | EP | Config-service is coupled to DL **at the storage layer** (writes DL's DynamoDB/S3/Secrets directly via a *vendored* `edl_shared_contracts` copy) — no API boundary; drift risk (mitigated, not removed, by contract-drift tests) | `repositories/entity_extraction_repository.py`, `vendor/edl_shared_contracts/*`, `tests/contract/test_*_contract_drift.py` |
| ARCH-07 | **H** | EP | **`infrastructure/` is entirely empty — no IaC exists.** Code comments claim IAM scoping that is not implemented; the service is **not deployable** and its security claims are unenforced aspirations | `datalake-config-service/infrastructure/modules/{dynamodb,ecs,iam,observability}` (empty) |
| ARCH-08 | **H** | EP | **No CI anywhere** (no `.github/`, no pipeline). Real test suites (incl. contract-drift, moto integration, vitest) never run automatically | repo root; `Makefile` only |
| ARCH-09 | L | EP | **(Positive)** Draft/publish split is a sound isolation seam — drafts live only in EP's own `EdlConfigSvcRegistry`; DL tables touched only at publish; deliberately avoids full-table Scan | `repositories/registry_repository.py:50-67` |
| ARCH-10 | M | EP | Multi-tenancy is application-guard-only; the **`tenantId` (Identity GUID) ↔ `tenant_code` (DL slug)** mapping is unresolved. **(Verified refinement: the frontend is genuinely a stub — `dataLakeConfigHttpClient.ts:23-24` uses `tenantId` as a stand-in; the backend is NOT — `tenant.py:27-34` + `identity_api_client.py:32-41` do a real claim-first + Identity-API lookup, just unvalidated end-to-end.)** | `dependencies/tenant.py:27-34`, `identity_api_client.py:32-41`, CST-UI `dataLakeConfigHttpClient.ts:23-24` |
| ARCH-11 | M | EP | New-surface readiness: strong for CRUD-shaped configs (registry + draft/publish generalizes cleanly), weak for AI-agent runtime wiring and for cross-entity/relationship validation (no home today) | `registry_repository.py:11-19` (closed `Capability` Literal), `services/*_service.py` `validate()` |

**Recommendations (Architecture):**
- **AR-1 (H):** Write EP's IaC (DynamoDB, ECS, IAM least-privilege task role, observability) and a CI pipeline before any new surface — ARCH-07/08 are release blockers and gate the security posture.
- **AR-2 (M):** Converge `entity_extraction_config` (and the audit GSI) onto the `tenant_scoped_key()` key-level model used by watermarks; keep `docs/PIPELINE_FLOW.md` as the single isolation reference.
- **AR-3 (M):** Resolve `tenantId ↔ tenant_code` before GA (latent cross-tenant hazard behind a TODO).
- **AR-4 (M):** Introduce a small **curated-layer read interface** in a shared location so entity-resolution (and the new relationship/semantic engines) depend on an interface, not `transformation.curated_layer_reader`.
- **AR-5 (M):** Add a **set-based data-processing substrate** (DuckDB-over-S3 / Athena / Glue) as a first-class architectural layer — the enabling change for relationships, semantic queries, and the agent (see Part B §B2).

## B. Design Patterns

**Patterns in use (sound, consistent):** Registry/plugin, Adapter (connectors, serving loaders),
Template Method (`ServingStoreLoaderInterface`), Repository, Factory (`from_registry`), Strategy
(survivorship, match rules), DTO/frozen-Pydantic contracts. EP adds a capability-agnostic
maker-checker + draft/publish state machine and centralized DI.

| ID | Sev | Repo | Finding | Evidence |
|---|---|---|---|---|
| DP-01 | **H** | DL | **Orphaned duplicate class:** `golden_record_publisher/golden_record_publisher.py` duplicates `canonical_record_publisher/canonical_record_publisher.py` (same `GoldenRecordPublisher`, byte-identical body minus `from_registry`). The golden_record_publisher copy is **unreferenced dead code** (not even tracked in KNOWN_GAPS) | `entity_resolution/golden_record_publisher/`, live import at `entity_resolution_pipeline_handler.py:66` |
| DP-02 | L | DL | Directory/class name mismatch (`GoldenRecordPublisher` lives under `canonical_record_publisher/`); `canonical`/`golden` used interchangeably | `entity_resolution/canonical_record_publisher/` |
| DP-03 | L | DL | `metrics_emitter.py` has 10 near-identical `emit_*` methods (mild boilerplate, readable — accept or lightly table-drive) | `observability/metrics_emitter.py` |
| DP-04 | L | EP | **(Positive)** Maker-checker `ChangeRequestService` is capability-agnostic via registered apply-callbacks (no circular deps); four-eyes enforced; optimistic-concurrency publish | `services/change_request_service.py`, `providers.py:110-121` |
| DP-05 | **M** | EP | **Systematic CRUD/validate/publish duplication** across 5–7 registry-backed services (`list/get/save_draft/publish/_to_response` ~80% identical); the `#`-delimited `capability_key` parsing has three divergent variants | `services/field_mapping_service.py` vs `services/entity_resolution_service.py`; `split("#")` variants |
| DP-06 | M | EP | Several services' `validate()` are no-ops (`return valid=True`) — the `/validate` endpoint is dead for them, and there is **no home for cross-record/semantic validation** the new surfaces need | `services/field_mapping_service.py:98-102`, `entity_resolution_service.py:107-110` |
| DP-07 | L | EP | Maker-checker branch (`if approve-permission … else propose`) is copy-pasted inline in each router's `publish` handler | `routers/schedule.py:94-137` |
| DP-08 | L | EP | **(Positive)** `S3VersionedConfigRepository` is a correctly-generalized generic (extracted on the 2nd shared use, not speculatively) — the model for the service base DP-05 needs | `repositories/s3_versioned_config_repository.py` |

**Recommendations (Design Patterns):**
- **DPR-1 (H):** Delete `entity_resolution/golden_record_publisher/` (pure dead-code removal, zero risk); then unify the `canonical`/`golden` naming (DP-02).
- **DPR-2 (M):** Extract a generic `RegistryBackedConfigService[TDraft,TResponse]` base owning `list/get/save_draft/publish/validate/audit`, plus a `parse_capability_key()` and a shared maker-checker publish step — **before** adding six new surfaces. New capability = ~40-line subclass.
- **DPR-3 (M):** Define a consistent service-level `validate()` contract (even where it delegates to Pydantic) as the home for cross-entity/semantic checks.

## C. Performance

**Overall:** The extraction tier is genuinely streaming and the Redshift serving loader is a
correct set-based design. But **transformation, entity resolution, and analytics publish collapse
to full in-memory `list[dict]` materialization** for any realistic dataset — the "streams via
DuckDB" claim only covers the S3 *read*, then re-materializes into Python. This is the central
scale risk and the hard blocker for relationships/semantic/agent workloads. EP endpoints block the
event loop.

| ID | Sev | Repo | Finding | Evidence |
|---|---|---|---|---|
| PERF-01 | **H** | DL | Salesforce bulk poll timeout (1800s) > Lambda timeout (900s); a large server-side job kills the Lambda mid-poll with no DLQ entry | `salesforce_bulk_query_job_controller.py:143,300` |
| PERF-02 | **H** | DL | NetSuite hard 100k-row/window ceiling (offset/limit); large windows permanently wedge until an operator shrinks the window | `netsuite_connector.py:89,241` |
| PERF-03 | M | DL | MySQL `ORDER BY watermark_field` on a possibly-unindexed column → filesort/full scan on source RDS each run | `mysql_incremental_extractor.py:213` |
| PERF-04 | **H** | DL | Checkpoint auto-resume not wired; **full loads get no checkpoint at all** → a full load that can't finish in 900s fails hard every run with zero progress; checkpoints route to a terminal Succeed with no re-trigger | `extraction_workflow.py:706,726`, `orchestration/main.tf:216` |
| PERF-05 | **H** | DL | Transformation streaming path is bypassed for any PII-bearing entity (auto-classification makes the policy non-None → `_execute_with_list`, full `list[dict]` + masked copy in RAM). **Verified:** even the `_execute_streaming` path accumulates the full canonical `list[dict]` (`:400`), so both paths materialize the output → OOM at scale | `transformation/transformation_pipeline.py:268,284,400,450,599` |
| PERF-06 | **H** | DL | SCD merge round-trips the **full** merged current-state through a Python list rather than `COPY … TO 's3://'` from DuckDB → O(total state) memory | `transformation/curated_layer_reader.py:306,425-430` |
| PERF-07 | M | DL | Small-file proliferation (per-50k-chunk + per-run partition files, no compaction) → degraded Athena/Glue/DuckDB scans (the substrate for the semantic layer & agent) | `raw_layer_writer.py:277` |
| PERF-08 | **H** | DL | Entity resolution holds the **entire cross-source candidate pool in memory** (`all_curated_records.extend(...)` → one `list[dict]`); single Lambda, ≤900s, ≤10GB; only a warn at 500k | `entity_resolution_pipeline_handler.py:351,434,441` |
| PERF-09 | **H** | DL | Match audit trail retains **every pairwise `MatchDecision` in memory** before serializing → memory bomb independent of input size | `matching_engine/match_rule_engine.py:166,208` |
| PERF-10 | H/M | DL | Blocking optional (defaults to O(n²) all-pairs); oversized blocks subdivided by **naive slicing** → true matches split across slices (false negatives) at scale; pure-Python Jaro-Winkler | `match_rule_engine.py:198,384`, `record_blocker.py:127` |
| PERF-11 | **H** | DL | **Planned features are architecturally blocked here:** cross-entity joins, semantic queries, and agent ad-hoc queries cannot run in the single-Lambda in-memory engine | (engine design) |
| PERF-12 | M | DL | Analytics publisher fully materializes golden records into a `list[dict]` | `analytics_publisher_handler.py:476,499,278` |
| PERF-13 | M | DL | RDS serving loaders are sequential single-Lambda row-batch (2k) with ~2 round-trips/batch → time out for millions of rows; no S3-bulk fallback (only Redshift is set-based) | `interfaces/loader_interface.py:288`, `postgresql_loader.py:189` |
| PERF-14 | M | DL | Circuit breaker has no cool-down/half-open — opens after 5 fails, reopens only on **manual** reset → transient outage wedges the entity | `extraction_retry_policy.py:238,252` |
| PERF-15 | M | DL | FIFO trigger queue (~300 msg/s, high-throughput mode off) may bottleneck at multi-tenant fan-out | `orchestration/main.tf:539` |
| PERF-16 | **H** | EP | **All ~51 endpoints are `async def` over synchronous blocking boto3** → one slow AWS call stalls all concurrent requests on the worker | all `routers/*`; no `run_in_threadpool`/`to_thread`/`aioboto3` anywhere |
| PERF-17 | **H** | EP | `/monitoring/logs` busy-polls CloudWatch Logs Insights with a blocking `time.sleep` loop inside an async route → freezes the worker up to the query timeout | `repositories/cloudwatch_logs.py:109-136` |
| PERF-18 | M | EP | `/monitoring/pipeline-executions` N+1 `describe_execution` fan-out (bounded, threadpool, but sync inside async) | `repositories/pipeline_executions_repository.py:59-117` |

**Positives:** Step Functions is STANDARD in all envs; transient-only retries with backoff+jitter,
deterministic errors fail fast; server-side cursors + `fetchmany` in MySQL; Bulk API streaming;
Redshift `COPY`+`MERGE`; EP registry listing is paginated (no Scans).

**Recommendations (Performance):**
- **PR-1 (H):** **Re-platform entity resolution and all record-level transforms onto a set-based engine** (DuckDB-over-S3 with `COPY … TO`, or Athena/Glue for the largest tenants). The Lambda becomes an orchestrator; matching, blocking, survivorship, masking, quality, SCD merge, and relationship joins run as SQL/vectorized ops, never as Python `list[dict]`. This single change resolves PERF-05..PERF-12 and is the prerequisite for every new feature (Part B §B2).
- **PR-2 (H):** Stream masking/quality/analytics-publish per batch; write merge/publish output straight from DuckDB to S3.
- **PR-3 (H):** Bound extraction to Lambda time (cap Salesforce poll; NetSuite keyset pagination; wire full-load checkpoint + ASL auto-resume).
- **PR-4 (M):** Add compaction; route large serving loads to Redshift or S3-bulk (Aurora `aws_s3` / `LOAD DATA FROM S3`); half-open circuit breaker.
- **PR-5 (H):** EP — make handlers plain `def` (FastAPI auto-offloads to a threadpool) or wrap blocking calls in `run_in_threadpool`; async-poll CloudWatch queries.

## D. Security & OWASP

**Overall:** Both repos are unusually security-conscious — parameterized SQL and identifier
allowlists everywhere (injection review **clean**), fail-closed auth on every data route, secrets
never logged or returned, safe error handling, no unsanitized XSS sinks. The real gaps are a
fail-open config default, missing audience verification, a reversible PII hash, and browser token
handling.

| ID | Sev | OWASP | Repo | Finding | Evidence |
|---|---|---|---|---|---|
| SEC-01 | **H** | A05/A07 | EP | Config-service **defaults to `env="local"`**, which honors the `X-Dev-Claims` auth-bypass header and exposes `/docs`; a missing/misspelled env var in prod silently disables JWT verification | `settings.py:11,93-95`, `jwt_verification.py:52-59`, `main.py:42` |
| SEC-02 | M | A01/A02/A07 | EP | JWT **audience not verified** when `jwt_audience` unset (default) → any token from the configured JWKS is accepted (token confusion/replay) | `jwt_verification.py:67-73`, `settings.py:21` |
| SEC-03 | M | A02 | DL | SENSITIVE_PII (SSN/CC/tax-id) auto-classified to **unsalted SHA-256** (brute-forceable for low-entropy inputs) while docstring claims "irreversible" | `governance/data_classification_policy.py:161-170,272-273` |
| SEC-04 | M | A02/A07 | EP | Frontend stores JWT in `localStorage` (XSS-exfiltratable) | `CST-UI/src/services/tokenService.ts:157-170,239-253` |
| SEC-05 | M | A02 | EP | Client-side AES-CBC with a **static all-zero IV** and build-embedded key (no real confidentiality; leaks plaintext equality) | `tokenService.ts:105-111,265-271` |
| SEC-06 | L | A01 | DL | Any authenticated identity can `POST /tenants` (no platform-admin scope) | `control_plane_handler.py:229-240` |
| SEC-07 | L | A05 | EP | CORS `allow_headers=["*"]` + `allow_credentials=True` (acceptable — origins are a strict allow-list) | `main.py:58-62` |
| SEC-08 | L | A02 | DL | Serving-store reader secrets created without explicit CMK (falls back to AWS-managed key) | `serving_store/interfaces/loader_interface.py:397` |
| SEC-09 | — | A01 | DL | **Known/accepted** tenant-isolation gaps: all S3 layers are tenant-prefixed at the write path but **not** IAM/bucket-policy-enforced (raw included — see ARCH-03 correction); one shared Secrets Manager credential per connector (genuinely not isolated); Glue/Athena table-name-prefix only. None IAM-enforced | `raw_layer_writer.py:406-413`, `docs/PIPELINE_FLOW.md:129-143` (stale on raw), `tests/test_tenant_isolation.py` |

**Clean areas (verified):** SQL injection (all values parameterized, identifiers allowlisted),
secrets/logging (only metadata/error-codes logged; writer creds never returned), Lambda handlers
read bucket/table names via `require_env` not payload, XSS (no `dangerouslySetInnerHTML`; DOMPurify
at the one rich-text sink), error handling (no stack traces to callers). IAM `Resource="*"`
statements are all justified (CloudWatch namespace-conditioned, X-Ray, ENI).

**Per-endpoint authN/authZ matrix** — DL control plane and EP config-service: **every tenant-scoped
route pairs authentication with a permission check and a tenant-path/claim match; no data route
omits either.** (Full matrix retained from assessment; health/catalog/discovery routes are
intentionally tenant-agnostic; `/monitoring/*` uses a cross-tenant System-Administrator gate that
structurally cannot be passed into a tenant check.)

**Recommendations (Security/OWASP) — priority order:**
- **SR-1 (H):** Default `env` to a non-local value; refuse to boot in local mode unless an explicit `ALLOW_LOCAL_AUTH_BYPASS` flag is also set; make `jwks_url`/`identity_api_base_url`/`jwt_audience` **required** (no placeholder defaults) in non-local envs and always `verify_aud=True` there. (SEC-01, SEC-02)
- **SR-2 (M):** Route SENSITIVE_PII through keyed `TOKENISE` (HMAC-SHA256), not `HASH`; correct the docstring. (SEC-03)
- **SR-3 (M):** Move JWT out of `localStorage` (MSAL in-memory cache / httpOnly cookie); enforce a strict CSP; stop relying on client-side symmetric crypto for confidentiality. (SEC-04, SEC-05)
- **SR-4 (L):** Gate `POST /tenants` behind an admin scope; explicit CMK for reader secrets. (SEC-06, SEC-08)
- **SR-5 (H, cross-cutting):** Close the tenant-isolation gaps (SEC-09) with IAM/LF enforcement as part of the SaaS-hardening phase — see Part B §B5.

## E. Monitoring & Observability

**Overall:** Strong foundation — JSON structured logs with recursive secret-scrubbing, correlation
context (`run_id`/`source_id`/`entity_id`/`tenant_code`) bound and cleared per invocation, X-Ray on
Lambdas + Step Functions, buffered metrics, tenant-scoped audit log, DLQ→SNS. But several **alarms
are wired to metrics/log-events no code emits** (false confidence), and the failure mode most
likely at scale (OOM/timeout) emits **no metrics at all**.

| ID | Sev | Repo | Finding | Evidence |
|---|---|---|---|---|
| OBS-01 | **H** | DL | **Dead alarm wiring:** `WatermarkLagSeconds` alarm exists but `emit_watermark_lag_seconds` has **no runtime callers — only tests call it** (freshness SLO never fires); log-metric alarms key on strings no code logs (`input_validation_failed`, `credential_retrieval_failed`, `circuit_breaker_opened`, `dlq_message_enqueued`); `emit_extraction_duration`/`emit_retry_count` never called | `observability/main.tf:112-117,568,597,642,726`, `metrics_emitter.py:200` |
| OBS-02 | **H** | DL | Metrics buffered and flushed only at **successful** run end → a killed (OOM/timeout) Lambda emits **nothing**; no Lambda-Insights memory metric or near-OOM alarm (the #1 scale failure has no signal) | `extraction_pipeline_handler.py:220`, `analytics_publisher_handler.py:471` |
| OBS-03 | M | DL | Entity resolution emits `GoldenRecordCount`/`ClusterCount` metrics, but the O(n²) risk signals are unmonitored: input record count and pairwise **decision count** are log-only (`match_rule_engine.py:220`), and **block count / max block size are neither logged nor emitted** (only a `blocking_enabled` boolean) → no alarm can catch a block-size/comparison explosion before OOM | `entity_resolution_pipeline_handler.py:316`, `match_rule_engine.py:220` |
| OBS-04 | H | DL | Hard Lambda kill leaves **no DLQ entry and no RUN_COMPLETION audit** (DLQ enqueue lives inside the Lambda; the SFN Fail state doesn't enqueue) | `run_lifecycle.py:282`, `orchestration/main.tf:217` |
| OBS-05 | M | DL | Only extraction has a DLQ; transformation/ER/analytics/serving failures have no DLQ and no replay path | `orchestration/main.tf:282,344,383` |
| OBS-06 | L/M | DL | Replay produces a new `run_id` with no correlation-id linking the replay chain (manual Logs-Insights joins for MTTR) | `run_lifecycle.py` (`generate_run_id`) |
| OBS-07 | L/M | DL | Best-effort audit writes swallow `ClientError` with no alarm on `audit_log_write_failed` frequency → a throttle silently blinds the audit trail | `run_lifecycle.py:339` |
| OBS-08 | M | EP | EP has no observability IaC (log group "doesn't exist yet"); no metrics/dashboards/alarms for the config service itself | `settings.py` comment; empty `infrastructure/modules/observability` |

**Recommendations (Observability):**
- **OR-1 (H):** Reconcile every alarm/log-filter with an actual emit call-site; add a CI check that each alarmed metric/event name is emitted somewhere. (OBS-01)
- **OR-2 (H):** Emit incremental heartbeat/progress metrics during long extraction/ER; enable Lambda Insights + a near-OOM memory alarm; flush metrics in a `finally`. (OBS-02, OBS-03)
- **OR-3 (H):** Guarantee a failure record on hard kills (SFN-level DLQ enqueue or Lambda destinations `onFailure`); give every stage a DLQ + replay; propagate a stable correlation id across replays. (OBS-04..OBS-06)
- **OR-4 (M):** Alarm on `audit_log_write_failed`; add EP observability IaC (structured logs, request metrics, health, tracing). (OBS-07, OBS-08)

## F. Reusable Code / Redundancy

**Overall:** Prior dedup work is real and worth crediting (DL: `SecretsManagerCredentialClient`,
`publishing_shared.py`, `S3ParquetWriter`, the serving-loader template method; EP: `_shared/` UI
components, `S3VersionedConfigRepository`, composable auth deps). The remaining redundancy is
concentrated in **copy-pasted Lambda-handler scaffolding (DL)** and **per-capability
service/UI-client/list-page boilerplate (EP)** — both of which will multiply if new features copy
the current pattern.

| ID | Sev | Repo | Finding | Evidence |
|---|---|---|---|---|
| REU-01 | **H** | DL | Lambda handler scaffolding copy-pasted across 5–6 handlers: 6 near-identical `_validate_event`, duplicated required-field/known-env constants, the **identical tenant_code fail-closed block+comment**, the `xray→bind_contextvars→try/except-log/finally-clear` shell, the metrics try/emit/flush block | `extraction/transformation/entity_resolution/analytics/serving` `*_handler.py` |
| REU-02 | M | DL | `_SAFE_S3_PREFIX_PATTERN` regex independently defined in **5 files** — the exact drift risk `identifier_policy.py` exists to prevent | `analytics_publisher_handler.py`, `serving_store_loader_handler.py`, `transformation_pipeline.py`, `curated_layer_reader.py`, `entity_resolution_pipeline_handler.py` |
| REU-03 | L | DL | Postgres-family DDL (`_ensure_tenant_container`, `_provision_reader_credential`) near-identical between postgres & redshift loaders (justified for 2; factor a `PostgresFamilyLoader` base on the 3rd) | `postgresql_loader.py`, `redshift_loader.py` |
| REU-04 | M | EP | Per-capability service CRUD/validate/publish triple ~80% identical (see DP-05); DI factories near-clone per capability | `services/*_service.py`, `providers.py` |
| REU-05 | M | EP | CST-UI API clients repeat the same fetch idiom across **13** files; no `createConfigResource(basePath)` factory | `services/dataLakeConfig/*Api.ts` |
| REU-06 | M | EP | CST-UI List/Form pages hand-duplicated per capability (same DataGrid+status+edit+breadcrumbs skeleton); no `ConfigListPage` component | `pages/dataLakeConfig/*/*List.tsx` |
| REU-07 | L | EP | New permission strings are hand-maintained in **2 independent definition sites** — backend `permission_constants.py` and frontend `PermissionConstants.tsx` (backend docstring: "MUST match CST-UI's PermissionConstants.tsx exactly, value for value") — with no single source of truth; `permissionNavHook.ts` only *consumes* them | `permission_constants.py:15-44`, `PermissionConstants.tsx:234-253` |

**Recommendations (Reuse):**
- **RR-1 (H):** Build a shared DL handler scaffold in `observability/` — a `PipelineStageEvent` Pydantic model (single fail-closed tenant validator) replacing the six `_validate_event`s, plus a context-binding wrapper for the bind/try/except/clear lifecycle. Enforces the tenant invariant in one place. (REU-01)
- **RR-2 (M):** Move `SAFE_S3_PREFIX_PATTERN` + `validate_s3_prefix()` into `contracts/identifier_policy.py`. (REU-02)
- **RR-3 (M):** Build EP's `RegistryBackedConfigService` base (DPR-2), a CST-UI `createConfigResource` client factory, and a `ConfigListPage` component **before** the six new screens. (REU-04..REU-06)
- **RR-4 (L):** Generate the permission-string set from one shared manifest consumed by both tiers. (REU-07)

## Consolidated remediation backlog (foundational — precedes new features)

These are the "must-fix or fix-alongside" items that the new features depend on. Full rationale in
Part C §C7; scheduling in Part D.

| Rank | Item | Findings | Why it gates new work |
|---|---|---|---|
| 1 | Set-based processing substrate (DuckDB/Athena/Glue); re-platform ER + record transforms | PERF-01,05–13, ARCH-05 | Relationships, semantic queries, agent all run here — cannot be built on the in-memory engine |
| 2 | EP IaC + CI | ARCH-07, ARCH-08 | Nothing new is deployable or guarded without them |
| 3 | Security defaults hardening | SEC-01, SEC-02, SEC-03 | Fail-open default + reversible PII hash must not ship with more surfaces |
| 4 | Shared DL handler scaffold + `identifier_policy` prefix pattern | REU-01, REU-02 | New stages reuse it instead of copying |
| 5 | EP generic config-service base + CST-UI factories | DP-05, REU-04–06 | Six new surfaces otherwise multiply boilerplate 6× |
| 6 | Observability truth-up (dead alarms, OOM signal, stage DLQs) | OBS-01–05 | New stages must be observable; existing blind spots fixed first |
| 7 | EP `async`→`def` (or threadpool) | PERF-16, PERF-17 | Config UI must stay responsive as usage grows |
| 8 | Delete orphaned duplicate; converge tenant-key isolation | DP-01, ARCH-03 | Clean base before extending |

---

# PART B — Target Architecture

## B1. Layered target — the Intelligence & Experience layers on the existing spine

The current pipeline is preserved end to end. Two new layers are added above the Analytics (Gold)
layer, and one new processing substrate is added *beneath* the existing stages (replacing their
internals without changing their contracts).

```
                        EXISTING SPINE (contracts unchanged)
 Connectors → Bronze(raw) → Silver(curated) → Gold(analytics, golden records) → Serving stores
      │            │              │                    │                             │
      └── all stages re-platformed onto ▼ (internals replaced, Step Functions shape preserved)
 ┌──────────────────────────────────────────────────────────────────────────────────────┐
 │  SET-BASED PROCESSING SUBSTRATE  (new)  — DuckDB-over-S3 / Athena / Glue                │
 │  matching · blocking · survivorship · masking · quality · SCD merge · relationship joins│
 └──────────────────────────────────────────────────────────────────────────────────────┘
                                        │  (Gold golden records + resolved relationships)
                                        ▼
 ┌───────────────────────────┐   ┌───────────────────────────┐
 │  KNOWLEDGE LAYER (new)     │   │  SEMANTIC LAYER (new)      │
 │  • Relationship resolution │──▶│  Governed entities,        │
 │  • Digital Twin (entity +  │   │  metrics, dimensions,      │
 │    edges + lifecycle/history)  │  joins → one contract for  │
 │  • Twin store (graph/relational)│  BI + agent               │
 └───────────────────────────┘   └─────────────┬─────────────┘
                                                │ (governed query contract)
                    ┌───────────────────────────┼───────────────────────────┐
                    ▼                            ▼                           ▼
          ┌──────────────────┐        ┌────────────────────┐      ┌──────────────────┐
          │ DASHBOARDS &      │        │ CONVERSATIONAL AI  │      │ Existing BI       │
          │ REPORTING (new)   │        │ AGENT + VERIFY LOOP│      │ (Athena/QuickSight│
          │ saved queries     │        │ (new)              │      │  serving stores)  │
          └──────────────────┘        └────────────────────┘      └──────────────────┘

  CONTROL PLANE (enterprise-platform, extended):  datalake-config-service  +  CST-UI console
  new config surfaces: quality rules · PII/classification · relationships/twin · semantic model ·
  dashboards · agent · data catalog        ── all behind Permissions/RBAC + maker-checker ──
```

## B2. Key architectural decisions

**AD-1 — Set-based processing substrate (the enabling redesign).** Introduce a `processing_engine`
capability that runs record-level work as SQL/vectorized ops over S3 Parquet (DuckDB in-Lambda for
typical volumes; Athena or Glue/Spark for the largest tenants), never as Python `list[dict]`.
Entity resolution, masking, quality, SCD merge, analytics publish, **and the new relationship
joins** all target it. Selection is config-driven per tenant/entity (small→DuckDB, large→Athena/Glue)
behind one interface — the same registry pattern already used for connectors and serving loaders.
This replaces the internals flagged in PERF-05..PERF-12 while keeping each Step Functions stage's
input/output contract identical (no topology change).

**AD-2 — Knowledge Layer = Relationship resolution + Digital Twin.** A new `knowledge` module runs
*after* per-entity-type golden records exist, resolving **edges** between entity types (company↔
contract↔term↔opportunity↔invoice↔person↔supplier) using config-declared relationship rules, and
persisting a **twin** (current attributes + edges + lifecycle/history). Stored as a
relationship/graph model plus a denormalized twin view for serving. New Step Functions stage,
additive — does not alter existing stages.

**AD-3 — Semantic Layer = governed definitions, versioned like every other config.** A declarative
model (entities, dimensions, metrics, joins, access scoping) mapped to Gold/twin/serving physical
tables, authored in the config console, stored as versioned JSON (same registry/S3-versioned pattern
as field mappings / entity-resolution configs), and served through a **query contract** consumed by
dashboards and the agent. No physical data movement — it is a definition + query-compilation layer.

**AD-4 — Agent consumes the semantic layer only; verification loop is mandatory.** The agent never
free-form-queries raw tables; it resolves NL → a semantic query against declared entities/metrics,
runs a **verification loop** (schema-valid → tenant-scoped → grounded-in-results → cited), and
self-corrects before returning. Read-only, tenant-scoped, permission-gated.

**AD-5 — Reuse the shared seams for all new work.** New Lambda stages use the RR-1 handler scaffold;
new config surfaces subclass the DPR-2 `RegistryBackedConfigService` and reuse the CST-UI factories
(RR-3); new tenant keys use `identifier_policy`. No new bespoke scaffolding.

**AD-6 — EP becomes deployable and guarded first.** IaC (DynamoDB/ECS/IAM/observability) + CI +
`async`→`def` + security-default hardening land before new surfaces (Part D Phase 0).

## B3. Non-breaking integration strategy

| New capability | Integration mechanism | Existing impact |
|---|---|---|
| Set-based substrate | Replaces stage *internals* behind unchanged Step Functions task contracts; feature-flagged rollout per tenant/entity, old path deleted once parity verified | None to topology; internal only |
| Relationship/Twin | **New** SFN stage after Analytics Publish; reads Gold, writes new twin store | Additive stage; skippable if unconfigured (like today's serving-store Pass branch) |
| Semantic Layer | New config artifacts + a query-compile service; no pipeline change | Additive |
| Dashboards | New read service + config surface + serving/Athena reads | Additive |
| AI Agent | New service (its own deployable) reading semantic layer; not on the ingestion path | Additive, isolated |
| Config surfaces (quality/PII/catalog/semantic/twin/dashboards/agent) | New `Capability` enum entries + `RegistryBackedConfigService` subclasses + CST-UI screens via factories | Additive; existing surfaces untouched |
| Quality-rules & PII UI | Surfaces config the pipeline **already consumes** (quality policy, classification policy) — moves authoring from seed scripts to the console | Pipeline already reads these; no behavior change, new authoring path |

**Redesign-without-compat rule applied:** where an internal is replaced (ER engine, transformation
materialization), the old implementation is **removed** once the new path passes parity tests — no
dual-mode retention (per Constraint 2). The seam that stays stable is the *stage contract*, not the
code behind it.

## B4. New / changed data model inventory

**S3 (additive prefixes, all tenant-prefixed):**
- `{tenant}/twin/{entity_type}/...` — twin snapshots (attributes + edges), partitioned by `twin_date`.
- `{tenant}/relationships/{relationship_type}/...` — resolved edges per run.
- config artifacts under existing curated bucket: `semantic-models/{tenant}/{model_version}.json`, `relationship-rules/{tenant}/{entity_type}/{version}.json`, `quality-policies/{tenant}/{entity_id}/{version}.json`, `classification-policies/{tenant}/{entity_id}/{version}.json`, `dashboards/{tenant}/{dashboard_id}/{version}.json`, `agent-config/{tenant}/{version}.json`.

**DynamoDB (new tables, tenant-partitioned from creation):**
- `datalake-twin-index-dev` — PK `tenant_code`, SK `entity_type#golden_id` → current twin pointer + edge summary + lifecycle stage.
- `datalake-semantic-model-dev` — PK `tenant_code`, SK `model_version` → active semantic model pointer.
- `datalake-saved-query-dev` — PK `tenant_code`, SK `query_id` → saved/named queries (dashboards & agent reuse).
- `datalake-agent-sessions-dev` / `datalake-agent-audit-dev` — PK `tenant_code`, SK `session_id#turn` → agent conversation + verification-loop audit (queries issued, checks passed/failed, sources cited).
- EP config tables extended with new `Capability` values (`quality_rules`, `classification_policy`, `relationship_rules`, `semantic_model`, `dashboard`, `agent_config`) — no new EP tables required beyond the registry/audit/change-request/limits set.

**Serving store:** twin denormalized views and semantic-model-materialized tables land in the
existing multi-engine serving store via the existing loader registry (Redshift preferred at scale).

## B5. Cross-cutting NFRs for all new work (mapped to the seven criteria)

Every feature spec in Part C restates these concretely; the platform-wide rules:

- **Architecture:** new module boundaries mirror existing ones (own `<module>/tests/`, registered in `pyproject.toml` `testpaths`/coverage/isort); depend on `contracts/` and interfaces, not sibling internals (fixes ARCH-04 pattern for new code).
- **Design patterns:** registry for pluggable engines/loaders; strategy for rules; repository for stores; template-method for shared stage lifecycle; **no** new god-objects, **no** dual-path legacy modes.
- **Performance:** set-based/streaming by default; batching + pagination on every list endpoint; async/queue for anything > a few seconds; parallelism by key-range where the substrate allows; no full-dataset `list[dict]` in any new Lambda; agent/semantic queries scan partitions, not full tables.
- **Security/OWASP:** every new endpoint authenticated + per-capability authorized + tenant-matched; all inputs Pydantic-validated (`extra="forbid"`); all SQL parameterized with identifier allowlists; the agent is read-only and tenant-scoped with query allow-listing; secrets only in Secrets Manager with explicit CMK; OWASP category cited on security code.
- **Observability:** every new stage/endpoint emits structured logs w/ correlation id, an **emitted-and-alarmed** metric (no dead alarms), an audit record, and a trace span; the agent logs every verification-loop decision; progress/heartbeat metrics on long jobs; Lambda-Insights memory on new heavy stages.
- **Reuse:** new stages use the RR-1 scaffold; new config surfaces subclass DPR-2 + CST-UI factories; shared patterns (SAFE_S3_PREFIX, tenant keys) from `contracts/`.

---

# PART C — Feature Requirement Specifications

Each spec uses: **Objective · Scope · Functional Requirements (FR) · Data Model · API/Interfaces ·
Design & Patterns · Integration & Non-Breaking · Cross-cutting compliance (Performance / Security &
OWASP / Observability / Reuse) · Config-UI · Open Questions.** FR ids are per-feature.

---

## C0. Foundational remediations (Phase 0 — enabling work, precedes the features)

The consolidated backlog (Part A) is a hard prerequisite. Summarized as requirements:

- **FR-F0.1** Introduce the set-based `processing_engine` interface + registry (DuckDB / Athena / Glue implementations) and re-platform entity resolution, transformation record-ops (mask/quality/SCD), and analytics publish onto it; delete the in-memory `list[dict]` paths after parity tests pass. *(PERF-01,05–13, PR-1/PR-2)*
- **FR-F0.2** EP: author IaC (DynamoDB, ECS service + task role least-privilege, KMS grants, Secrets path scoping, observability) and a CI pipeline (ruff/bandit/pytest + contract-drift; eslint/vitest/tsc/build). *(ARCH-07/08)*
- **FR-F0.3** Security defaults: non-local `env` default + explicit local-bypass flag; required `jwks_url`/`identity_api_base_url`/`jwt_audience` + `verify_aud=True` in non-local; SENSITIVE_PII→keyed TOKENISE. *(SEC-01/02/03)*
- **FR-F0.4** DL shared handler scaffold (`PipelineStageEvent` + context-binding lifecycle) in `observability/`; move `SAFE_S3_PREFIX_PATTERN`→`identifier_policy`. *(REU-01/02)*
- **FR-F0.5** EP generic `RegistryBackedConfigService` base + `parse_capability_key()` + shared maker-checker publish step; CST-UI `createConfigResource` client factory + `ConfigListPage` component. *(DP-05, REU-04–06)*
- **FR-F0.6** Observability truth-up: reconcile alarms↔emitters (+ CI check), flush metrics in `finally`, Lambda-Insights memory alarm, guaranteed failure record on hard kill, per-stage DLQ + replay, stable cross-replay correlation id. *(OBS-01–07)*
- **FR-F0.7** EP `async def`→`def` (or `run_in_threadpool`); async-poll CloudWatch monitoring. *(PERF-16/17)*
- **FR-F0.8** Delete orphaned `golden_record_publisher/`; converge `entity_extraction_config` onto `tenant_scoped_key()`; add curated-layer read interface. *(DP-01, ARCH-03/04)*

---

## C1. Relationship Resolution & Digital Twin

**Objective.** Turn independent per-entity golden records into a connected, lifecycle-aware **twin**
of each real-world entity, so BI, dashboards, and the agent see one coherent object (attributes +
relationships + history) instead of scattered tables.

**Scope.** Cross-entity-type edge resolution and twin materialization for the configured entity
types; current-state + historized lifecycle. Out of scope: real-time CDC (roadmap), ML-based edge
inference (v2).

**Functional Requirements.**
- **FR-1.1** Resolve edges between entity types from **config-declared relationship rules** (deterministic key joins + optional probabilistic association), producing typed edges (e.g. `contract→company`, `invoice→company`, `person→company`, `contract-term→contract`).
- **FR-1.2** Rules are versioned JSON per tenant per relationship type (blocking key, join fields, match strategy/threshold, cardinality), authored in the console (C6), published like entity-resolution configs.
- **FR-1.3** Persist a **twin** per entity instance: mastered attributes (from Gold), resolved edges (adjacency), and a **lifecycle stage + history** (state transitions over time — SCD Type-2-style history table).
- **FR-1.4** Expose a twin read API: `get_twin(entity_type, golden_id)` → attributes + 1-hop edges + lifecycle; `expand(golden_id, edge_type, depth≤N)` for bounded traversal.
- **FR-1.5** Emit derived rollups on the twin (e.g. `company.total_contract_value`, `open_invoice_count`) computed set-based from related entities.
- **FR-1.6** Edge/twin builds are **idempotent** and replayable (stable edge ids from sorted endpoint golden_ids + relationship type).
- **FR-1.7** Provenance preserved: every edge and rollup traces to contributing source records (reuse `contributing_source_records`/`field_provenance`).

**Data Model.** `datalake-twin-index-dev` (pointer + edge summary + stage); S3 `{tenant}/twin/{entity_type}/twin_date=…` (attributes+edges), `{tenant}/relationships/{relationship_type}/run_id=…` (edges), lifecycle-history partition; relationship-rule configs in curated bucket. Optional graph representation materialized into the serving store as denormalized twin views.

**API/Interfaces.** New `knowledge` module: `RelationshipResolver` (strategy-driven, runs on the set-based substrate), `TwinBuilder`, `TwinRepository`. New Step Functions stage `BuildTwin` after `AnalyticsPublish`, threading `tenant_code` + `entity_type` (same pattern as serving-store stage). Twin read exposed via the control-plane API (`GET /tenants/{tc}/twins/{entity_type}/{golden_id}`, `GET …/expand`).

**Design & Patterns.** Strategy (relationship rules), Repository (`TwinRepository`), registry for the processing engine; template-method stage lifecycle via the RR-1 scaffold. Reuse `publishing_shared` lineage emission.

**Integration & Non-Breaking.** Additive SFN stage; **skippable** when no relationship rules are configured (Pass branch, exactly like the serving-store stage today). No change to Gold/analytics outputs; twin reads are new endpoints.

**Cross-cutting compliance.**
- *Performance:* edges resolved as **set-based joins** on the substrate (never `list[dict]`); blocking keys required above a row threshold; bounded traversal depth; rollups as SQL aggregates; partitioned writes + compaction.
- *Security/OWASP:* twin/expand endpoints authenticated + `datalake:twin:read` + tenant-matched; traversal capped (prevents unbounded fan-out / A05); no cross-tenant edge resolution (join inputs are tenant-scoped).
- *Observability:* metrics `EdgesResolved`, `TwinsBuilt`, `MaxFanoutDegree`, stage duration — all alarmed; per-stage DLQ; lineage records.
- *Reuse:* RR-1 scaffold, substrate registry, `identifier_policy`, shared lineage.

**Config-UI (C6).** Relationship-rule builder (reuses `ruleBuilder/`), twin/lifecycle-stage definition, twin explorer (read view).

**Open Questions.** Graph store choice (relational adjacency vs. a graph DB) — recommend relational adjacency on the existing serving store first; lifecycle-stage taxonomy per entity type (needs business input).

---

## C2. Semantic Layer

**Objective.** A governed business-definition layer over Gold/twin/serving so every consumer
computes the same metrics the same way, and NL tools have a reliable contract to query.

**Scope.** Declarative entities, dimensions, measures/metrics, joins, and access scoping; a
query-compilation service that turns a semantic request into parameterized SQL against the serving
store/Athena. Out of scope: a full BI modeling GUI (v2 — start with structured config + validation).

**Functional Requirements.**
- **FR-2.1** Define **entities** (mapped to Gold/twin tables), **dimensions**, **measures** (aggregations), and **metrics** (named, governed calculations) in a versioned semantic model per tenant.
- **FR-2.2** Declare **joins/relationships** between entities (aligned with C1 edges) so consumers query concepts, not physical joins.
- **FR-2.3** A **query-compile service**: input = {metrics[], dimensions[], filters[], time grain}; output = parameterized SQL + result, executed tenant-scoped against serving/Athena. Never accepts raw SQL from callers.
- **FR-2.4** **Governed validation** (fills DP-06): a metric/dimension must reference declared, existing fields; publish is blocked otherwise (real cross-record `validate()`).
- **FR-2.5** Access scoping at the semantic layer: per-metric / per-dimension permission tags enforced at compile time (beyond DB GRANTs).
- **FR-2.6** Metric **lineage**: each metric records the physical columns/joins it derives from.
- **FR-2.7** Versioned + maker-checker on publish (definitions are high-blast-radius).

**Data Model.** `datalake-semantic-model-dev` (active-version pointer); S3 `semantic-models/{tenant}/{version}.json` (entities/dimensions/measures/metrics/joins/access-tags). No data movement.

**API/Interfaces.** `semantic` service: `SemanticModelRegistry` (load/validate/version), `QueryCompiler` (semantic request → parameterized SQL), `SemanticQueryService` (compile+execute, tenant-scoped). Control-plane read: `POST /tenants/{tc}/semantic/query` (structured request), `GET …/semantic/model`.

**Design & Patterns.** Registry + versioned-config (mirror `S3VersionedConfigRepository`), Strategy (per-dialect SQL compilation), Repository. The compiler is the single place SQL is generated → central injection-safety + access enforcement.

**Integration & Non-Breaking.** Fully additive; reads existing serving/analytics. Existing Athena/QuickSight usage is unaffected (semantic layer is an *additional* governed path, not a replacement).

**Cross-cutting compliance.**
- *Performance:* compiled SQL is partition-scoped, paginated, and pushed to the serving engine (Redshift/Athena) — no in-app aggregation; result caching for repeated dashboard/agent queries.
- *Security/OWASP (A03/A01):* callers submit **structured requests, never SQL**; compiler parameterizes values and allowlists identifiers from the model; per-metric access tags enforced; tenant scope injected server-side.
- *Observability:* `SemanticQueriesCompiled`, `QueryLatencyMs`, `AccessDenied` metrics (alarmed); every compiled query logged with correlation id (no raw PII in logs).
- *Reuse:* versioned-config repo, maker-checker, DPR-2 base.

**Config-UI (C6).** Metric/dimension/entity/join editor with live validation against the connector/twin catalog; version history; publish + approval.

**Open Questions.** Build vs. adopt (custom compiler vs. embedding a library like Cube/dbt-metrics semantics) — recommend a **thin custom compiler** first (tight fit to the tenant/serving model, no new infra), and revisit adopting a library (e.g. Cube / dbt-metrics semantics) only if metric complexity grows.

---

## C3. Conversational AI Agent + Verification Loop

**Objective.** Let users ask questions in natural language and get **grounded, cited, governed**
answers and reports, plus save/re-run named queries — built on the semantic layer, never on raw
tables.

**Scope.** Read-only analytical Q&A over the semantic layer + twin; report generation from results;
saved queries. Out of scope: write actions / pipeline control via chat (v2, gated); autonomous
scheduling.

**Functional Requirements.**
- **FR-3.1** NL question → **semantic request** (metrics/dimensions/filters), resolved against the tenant's semantic model — the agent emits a structured semantic request, not SQL.
- **FR-3.2** **Verification loop (mandatory):** before returning, the agent runs checks — (a) request references only declared entities/metrics; (b) request is tenant-scoped; (c) compiled query executes without error; (d) the drafted answer is **grounded** in the returned rows (numbers/claims trace to result cells); (e) sources/provenance cited. On any failure the agent **self-corrects and retries** up to N times; if still failing it returns an explicit "cannot answer confidently" with the reason — never an ungrounded guess.
- **FR-3.3** Every turn produces an **agent audit record**: question, resolved semantic request, compiled query, verification results per check, retries, final answer, cited sources.
- **FR-3.4** **Saved queries**: name, store, and re-run a semantic request; shareable within tenant per permissions; power dashboards (C4).
- **FR-3.5** Report generation: render results as tables/summaries; export.
- **FR-3.6** Read-only + permission-scoped: the agent inherits the caller's semantic access tags (cannot surface metrics the user can't see).
- **FR-3.7** Model/provider config is tenant-scoped and stored securely (API keys in Secrets Manager w/ CMK).

**Data Model.** `datalake-saved-query-dev`; `datalake-agent-sessions-dev`/`datalake-agent-audit-dev` (turn-level verification audit); `agent-config/{tenant}/{version}.json` (model, tool wiring, limits). No raw data stored in sessions beyond result references.

**API/Interfaces.** New **standalone service** (own deployable, like the config service) — not on the ingestion path. `POST /tenants/{tc}/agent/ask` (streamed), `GET/POST …/agent/saved-queries`, `POST …/agent/saved-queries/{id}/run`. Consumes `SemanticQueryService` only.

**Design & Patterns.** Orchestrator with an explicit **verify → correct → answer** loop (state machine, not free-form). Tool interface limited to `resolve_semantic_request`, `compile_and_run` (read-only), `cite`. Strategy for the LLM provider (pluggable). For LLM/provider integration follow the current Claude model guidance; keep provider behind an interface.

**Integration & Non-Breaking.** Isolated new service; reads semantic layer + twin; touches no pipeline stage. Fails closed (no answer) rather than degrading other systems.

**Cross-cutting compliance.**
- *Performance:* queries are semantic-compiled (partition-scoped) + cached; streaming responses; per-tenant rate limits; verification adds bounded retries only.
- *Security/OWASP (A01/A03/A09/LLM-injection):* read-only, tenant-scoped, permission-inherited; **no raw SQL from the model** reaches a database — only compiled semantic requests; prompt-injection mitigations (the model cannot widen scope or bypass access tags because execution goes through the governed compiler, not the model's text); provider keys in Secrets Manager; full audit (A09).
- *Observability:* metrics `AgentTurns`, `VerificationFailures`, `Retries`, `AnswerConfidence`, `Latency` (alarmed); every verification decision logged; audit trail per turn.
- *Reuse:* semantic layer, permissions, saved-query store shared with dashboards.

**Config-UI (C6).** Agent settings (model/provider/limits), saved-query management, a conversational panel (net-new component — the one screen that doesn't fit the list/form mold, per F5.1).

**Open Questions.** Provider/model selection & data-residency; confidence-threshold policy for FR-3.2 fallback.

---

## C4. Dashboards & Reporting

**Objective.** Governed dashboards and reports built on saved semantic queries — consistent numbers,
tenant-scoped, permission-aware.

**Scope.** Dashboard definitions (layout + tiles bound to saved semantic queries), scheduled/report
export, embedding in the console. Reuse the serving store + semantic layer; optionally surface
existing QuickSight.

**Functional Requirements.**
- **FR-4.1** Define dashboards as versioned config: tiles referencing **saved semantic queries** (C3/FR-3.4) with viz type + layout.
- **FR-4.2** Tenant-scoped, permission-gated view/edit; per-tile access respects semantic access tags.
- **FR-4.3** Refresh via the semantic query service (cached); no per-tile bespoke SQL.
- **FR-4.4** Export/report (PDF/CSV) and optional scheduled delivery.
- **FR-4.5** Optional: register/link existing QuickSight dashboards for continuity.

**Data Model.** `dashboards/{tenant}/{dashboard_id}/{version}.json`; reuses `datalake-saved-query-dev`.

**API/Interfaces.** Config surface (C6) + a read `GET /tenants/{tc}/dashboards/{id}` returning definition; data via `SemanticQueryService`.

**Design & Patterns.** Versioned-config + registry; composition over the saved-query primitive (dashboards are thin over C3). Read-oriented catalog pattern for listing.

**Integration & Non-Breaking.** Additive; no pipeline impact; existing BI paths untouched.

**Cross-cutting compliance.** *Performance:* cached semantic queries, lazy tile loading, pagination. *Security:* view/edit permissions + semantic access tags; export respects scope. *Observability:* `DashboardViews`, `TileErrors`, refresh latency. *Reuse:* saved queries, semantic service, versioned-config, CST-UI list/detail factories.

**Open Questions.** Native rendering vs. embedding QuickSight — recommend native tiles over the semantic layer for governance consistency; QuickSight linking as an optional bridge.

---

## C5. Permissions / RBAC (deepened)

**Objective.** Extend the existing granular, per-capability permission model to the new surfaces
and enforce it consistently across data (twin/semantic/agent/dashboards), with a single source of
truth for permission strings.

**Scope.** New permission strings for every new capability; enforcement at API + semantic
access-tag level; a shared permission manifest consumed by backend + frontend.

**Functional Requirements.**
- **FR-5.1** Add view/edit (and where relevant, approve) permissions per new capability: `twin`, `semantic_model`, `relationship_rules`, `dashboard`, `agent`, `quality_rules`, `classification_policy`, `data_catalog`.
- **FR-5.2** Every new endpoint pairs `require_permission(...)` + `require_tenant_match(...)` (no exceptions) — extend the existing pattern.
- **FR-5.3** Semantic/twin/dashboard/agent reads enforce **per-metric/per-dimension access tags** (data-level authorization beyond route-level).
- **FR-5.4** **Single source of truth** for permission strings: one manifest generating the backend `permission_constants.py` and frontend `PermissionConstants.tsx` + nav flags (fixes REU-07 drift).
- **FR-5.5** Maker-checker retained for high-blast-radius surfaces (credentials, schedules, serving-store, **semantic model**, **agent config**).
- **FR-5.6** Resolve the `tenantId ↔ tenant_code` identity (ARCH-10) before GA — a prerequisite for trustworthy data-level authz.

**Design & Patterns.** Reuse the composable `require_permission` dependency factory and the two-principal-type design; add data-level authorization in the semantic compiler (single enforcement point).

**Cross-cutting compliance.** *Security/OWASP (A01):* function-level + data-level authz on every new surface; fail-closed; no cross-tenant admin leakage (existing `SystemAdministratorPrincipal` structural guarantee). *Observability:* `AccessDenied` metrics per capability. *Reuse:* one permission manifest; shared dependencies.

**Open Questions.** Whether Identity API can mint the new permission strings + expose a distinct `tenant_code` claim (owner sign-off needed).

---

## C6. Config-UI Extensions & the Config-Service Base

**Objective.** Surface the remaining pipeline config in the console and host the new
capabilities' config — on a generic base so six new surfaces don't 6× the boilerplate.

**Scope.** New config surfaces: **Data-Quality Rules**, **PII/Classification Policy**, **Data
Catalog** (read), plus the config for C1–C4 (relationships/twin, semantic model, dashboards, agent).
Plus the enabling factories (FR-F0.5).

**Functional Requirements.**
- **FR-6.1** **Data-Quality Rules** surface: author per-entity quality policy (null/range/pattern/enum, blocking/warning) — the pipeline already consumes this; move authoring from seed scripts to the console. Draft/validate/publish.
- **FR-6.2** **PII/Classification Policy** surface: per-field classification + masking strategy (redact/partial/tokenise/hash) — with SENSITIVE_PII defaulting to keyed TOKENISE (SEC-03). Draft/validate/publish + maker-checker (high blast radius).
- **FR-6.3** **Data Catalog** (read): browse entities/datasets — schema, freshness, quality score, lineage, and (new) twin/relationship + semantic-metric metadata; drives dynamic form pickers (reuse the read-only catalog pattern).
- **FR-6.4** Config surfaces for relationships/twin (C1), semantic model (C2), dashboards (C4), agent (C3) as `RegistryBackedConfigService` subclasses + CST-UI screens.
- **FR-6.5** Backend: all new surfaces are ~40-line subclasses of the **generic `RegistryBackedConfigService`** (FR-F0.5); real `validate()` per surface (DP-06).
- **FR-6.6** Frontend: all list/detail screens use the **`ConfigListPage`** component + **`createConfigResource`** client factory (FR-F0.5); nav/permission via the shared manifest (FR-5.4).
- **FR-6.7** The **agent panel** is the one net-new UI primitive (conversational/streaming) — not forced into the list/form mold.

**Design & Patterns.** Generic config-service base (template-method + strategy for per-capability specifics); read-only catalog pattern; CST-UI factories. `Capability` enum extended per surface (one-line additions).

**Integration & Non-Breaking.** Existing seven surfaces keep working; the base is introduced by refactoring them onto it first (parity-tested), then new surfaces extend it. Quality/PII surfaces author config the pipeline already reads — no pipeline behavior change.

**Cross-cutting compliance.** *Performance:* EP endpoints non-blocking (FR-F0.7); paginated lists. *Security:* per-capability permissions + tenant match; PII values never returned; maker-checker on PII/semantic/agent. *Observability:* EP request metrics + audit (FR-F0.6/OBS-08). *Reuse:* the whole point — base + factories + shared manifest.

**Open Questions.** Data-catalog freshness/quality-score source (derive from run-audit + quality reports); catalog build cadence.

---

# PART D — Implementation Roadmap

Phased so that (a) foundations that de-risk everything land first, (b) each phase preserves existing
functionality, and (c) each feature ships only after its prerequisites. "Preserves existing" column
states the guarantee.

| Phase | Delivers | Key requirements | Preserves existing |
|---|---|---|---|
| **0 — Foundations** | Set-based substrate + ER/transform re-platform; EP IaC+CI; security-default hardening; DL handler scaffold; EP config-service base + CST-UI factories; observability truth-up; EP async fix; dead-code/isolation cleanup | FR-F0.1–F0.8 | Stage contracts unchanged; old internals deleted only after parity tests pass |
| **1 — Knowledge Layer** | Relationship resolution + Digital Twin (build stage, twin store, read API) + relationship-rule config UI | C1 (FR-1.*), C6 relationship/twin surfaces | New skippable SFN stage; Gold/analytics outputs unchanged |
| **2 — Semantic Layer** | Semantic model + query-compile service + semantic config UI (with real validation) | C2 (FR-2.*), C6 semantic surface, DP-06 fix | Additive; existing Athena/QuickSight paths untouched |
| **3 — Consumption** | AI Agent + verification loop (standalone service) and Dashboards on saved semantic queries | C3, C4, C6 agent panel + dashboard surface | Isolated new service; no pipeline impact |
| **4 — Config completeness & SaaS hardening** | Quality-rules + PII/classification + Data-catalog UIs; permission manifest single-source; tenant-isolation IAM/LF enforcement; `tenantId↔tenant_code` resolution | C5, C6 (FR-6.1–6.3), SEC-09 closure, ARCH-10 | Pipeline already consumes quality/PII config — authoring path only |

**Sequencing rationale.** Phase 0's substrate (FR-F0.1) is the hard prerequisite for Phases 1–3
(relationships, semantic queries, and the agent all execute there). The EP base + factories
(FR-F0.5) must precede the six new surfaces or the boilerplate multiplies. Security/observability
foundations precede exposing new endpoints.

---

# Appendix

## Appendix 1 — Finding → where addressed

| Findings | Addressed by |
|---|---|
| PERF-01,03,04 (extraction limits) | FR-F0.1 substrate + PR-3 (Phase 0) |
| PERF-05–13, PERF-11 (in-memory engine) | FR-F0.1 set-based substrate (Phase 0); C1/C2 built on it |
| PERF-16,17,18 (EP blocking) | FR-F0.7 (Phase 0) |
| ARCH-07,08 (no IaC/CI) | FR-F0.2 (Phase 0) |
| ARCH-03,04,10 (isolation/coupling/identity) | FR-F0.8 + Phase 4 (ARCH-10) |
| DP-01,05,06 (dead code / service dup / empty validate) | FR-F0.8, FR-F0.5, FR-2.4 |
| SEC-01,02,03 | FR-F0.3 (Phase 0) |
| SEC-04,05,06,08 | Phase 0/4 security tasks (SR-3/SR-4) |
| SEC-09 (tenant isolation) | Phase 4 (IAM/LF) |
| OBS-01–08 | FR-F0.6 (Phase 0) + per-feature observability |
| REU-01–07 | FR-F0.4, FR-F0.5, FR-5.4 |

## Appendix 2 — Implementation guidelines (binding on all code)

- **Comments/docstrings:** one line at most; do **not** add explanatory prose above methods,
  classes, or properties. Name by domain concept — no `helper`/`util`/`common`/`manager`.
- **Security code:** cite the OWASP category inline (existing 150+-occurrence convention).
- **Lambda handlers:** use the shared scaffold (FR-F0.4) — thin `lambda_handler` → validated
  `PipelineStageEvent` → context bind → `_run_*` → `finally: clear_contextvars()`; read
  bucket/table names via `require_env`, never the payload.
- **Tenant scoping:** only via `contracts/identifier_policy.py`; never re-derive tenant/ID regexes
  or the S3-prefix pattern.
- **New module checklist:** own `<module>/tests/` registered in `pyproject.toml` `testpaths`,
  `[tool.coverage.run].source`, and isort `known-first-party`; scoped `mypy`; passes `make
  banned-names`.
- **Config artifacts:** versioned JSON via the registry / `S3VersionedConfigRepository` pattern;
  maker-checker for high-blast-radius surfaces; real `validate()` (no no-op).
- **No dual-path legacy code:** when replacing an internal, delete the old path after parity — the
  stable seam is the stage/endpoint contract, not the implementation.

## Appendix 3 — Source of this assessment

Findings are evidence-based from a direct read of both repositories (not docs), 2026-07-23, across
architecture, design patterns, performance, security/OWASP, observability, and reuse. Every finding
in Part A cites a real file/location; re-verify against the code before relying on any specific line
number, as the tree drifts.

<!-- END -->



