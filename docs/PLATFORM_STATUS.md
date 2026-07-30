# Platform Status — Enterprise Data Lake

**Last updated:** 2026-07-28 (second pass — wiring)
**Prepared by:** Platform Engineering

> **Multi-tenancy note:** `tenant_code` is a first-class concept (default: `demo`, from
> `contracts/identifier_policy.DEFAULT_TENANT_CODE`), prefixed into S3 keys for **every** data-plane
> layer (raw, curated, analytics, schema snapshots), and genuinely key-scoped in the watermark and
> entity-type-registry DynamoDB tables. See `docs/PIPELINE_FLOW.md`'s canonical isolation-model table
> for the full layer-by-layer picture, and `docs/KNOWN_GAPS_AND_ROADMAP.md` for what's still open (no
> IAM enforcement **in force** anywhere yet — S3 prefixing is write-path convention, not a
> bucket-policy boundary; the deny-based IAM tenant boundary and the Lake Formation LF-Tags that
> replace Glue/Athena's wildcard grant exist in Terraform but are unapplied, and the IAM boundary
> ships in audit mode).
>
> **This document describes what is deployed.** The SOW requirements programme (2026-07-28) added a
> large amount of code and Terraform that is **not applied anywhere** — see "Declared but not yet
> applied" below, and `docs/KNOWN_GAPS_AND_ROADMAP.md` for the per-item status. Where the two
> disagree, the deployed state in this document is the truth about the running system.

---

## Current Status: Dev ✅ (infrastructure) | Staging 🔲 | Production 🔲

| Environment | Status | Notes |
|---|---|---|
| **Dev** | ✅ Infrastructure deployed, pipeline verified live | All 8 Lambda functions, Step Functions state machine, control-plane API (Cognito + API Gateway), DynamoDB tables, S3 buckets, SQS queues, EventBridge Scheduler group — deployed fresh on 2026-07-09 (an earlier "live" claim for this account had gone stale; the account was found empty and redeployed from scratch). Salesforce and MySQL RDS credentials are populated and the extraction → transformation → entity resolution → analytics pipeline has run end-to-end with real data (see Live Data below). Sage Intacct, Sage X3, and NetSuite credentials are still empty shells — those sources are code-complete but not connected. |
| **Staging** | 🔲 Not provisioned | `terraform validate` is clean. No AWS account/credentials provisioned yet. |
| **Production** | 🔲 Not provisioned | `terraform validate` is clean. Requires staging sign-off first per this repo's promotion policy. |

---

## Declared but not yet applied (SOW requirements programme, 2026-07-28)

All of the following `terraform validate`s cleanly in dev, staging, and prod, and is covered by the
test suite — and **none of it has been applied to any AWS account**. Nothing in the deployed-state
sections below changed.

### Wired vs declared-only — do not read "exists in code" as "runs"

The distinction that matters is whether a **deployed entry point can reach the code**, not whether
the code and its tests exist. `make wiring-gates` computes it; these numbers are from that command
and are regenerated rather than hand-maintained:

| Status | Count | Meaning |
|---|---|---|
| wired | 96 | Reachable from a Lambda handler, API route, or operator script |
| infrastructure | 7 | Enforced by Terraform (KMS, TLS, LF-Tags), not by application code |
| declared-only | 15 | Code and tests exist; **no deployed entry point reaches it** — each waived with the plan item that will wire it |
| missing | 20 | No citation anywhere; process/deployment obligations, each waived with a reason |

Every declared-only and missing item is listed in `requirements/WAIVERS.md` with its reason. The
gates **fail on a stale waiver**, so that file cannot drift into fiction: when a module becomes
reachable, CI breaks until the waiver is removed.

### New Lambda functions declared in Terraform (not applied)

`infrastructure/modules/platform_lambdas/` declares four functions whose handlers previously had no
deployment at all: `datalake-webhook-receiver-dev`, `datalake-connector-writeback-dev`, `datalake-workflow-runner-dev`,
`datalake-portability-dev`. Each has its **own** execution role
(`infrastructure/modules/iam/platform_lambda_roles.tf`) — the write-back role reads only the
`-writeback` secret suffix, and the portability role holds the only bulk `s3:DeleteObject` in the
platform. The webhook API route and the workflow schedules are opt-in per environment and default
to off.

| Area | What exists in code | Terraform |
|---|---|---|
| Programme tables | 21 new DynamoDB tables (source connections, scope units, effective config, restatements, config governance, quality exceptions/policies, brands, data dictionary, semantic model versions, saved queries, workflow definitions/executions/idempotency/destinations, approval tasks, export requests, deletion requests, webhook dedup, PHI classifications) | `infrastructure/modules/metadata_persistence/programme_tables.tf` |
| Per-metric alarms | One alarm per catalogued `PlatformMetric`, reconciled bidirectionally against the emitters by `observability/tests/test_alarm_emitter_reconciliation.py`; 5 metrics route to a paging SNS topic; Lambda Insights memory alarms | `infrastructure/modules/observability/platform_metric_alarms.tf` |
| Per-stage DLQs | 9 per-stage dead-letter queues, a terminal `datalake-replay-exhausted-dev`, `datalake-webhook-ingest-dev.fifo`, `datalake-report-distribution-dev`, plus depth alarms | `infrastructure/modules/orchestration/per_stage_dlq.tf` |
| WAF | Managed rule sets + rate limiting in front of the control plane, **audit (count) mode** | `infrastructure/modules/waf/` |
| IAM tenant boundary | Deny-based boundary across S3/DynamoDB/Secrets Manager, `tenant_boundary_mode = audit`, CloudTrail metric filter emitting `CrossTenantAccessAttempts` | `infrastructure/modules/iam/tenant_boundary.tf` |
| Lake Formation | Per-tenant and per-department LF-Tags replacing the wildcard grant. **Applying this revokes a grant three real dev principals currently depend on** — confirm the principals with the account owner first | `infrastructure/modules/lake_formation/` |
| Client VPN | Scaffolded with `enabled = false`, pending the customer's answer on the BI network-path decision | `infrastructure/modules/client_vpn/` |

New Lambda entry points that exist in code but have no deployed function yet:
`connector_runtime/webhook_receiver_handler.py` (provider webhooks) and
`connector_runtime/writeback_handler.py` (bi-directional write-back).

**Two data migrations must run before the corresponding code is deployed to an environment**, both
dry-run by default:

```bash
make migrate-connections    # scripts/migrate_to_connection_identity.py — default connections (DL-12)
make migrate-credentials    # scripts/migrate_credentials_to_connection_paths.py — per-connection secrets
```

Twelve sources (HubSpot, MaidCentral, ServMan Pro, WellSky, Housecall Pro, Dialpad, SeniorPlace,
Google Ads, Google Analytics, Meta Ads, ServiceBridge, BePro) are implemented as declarative specs
on the shared REST substrate. ServiceBridge and BePro were added on 2026-07-29 from
customer-supplied API documentation.

**Correction (2026-07-29):** three of those specs — MaidCentral, WellSky and SeniorPlace — had been
written against an API that does not exist (wrong auth kind, wrong paths, wrong response envelope,
wrong pagination parameters, invented entities) and would have failed on their first request. All
three are rewritten against the vendors' published documentation, and
`connector_runtime/tests/test_documented_source_fidelity.py` now asserts the documented facts so a
spec and its source document cannot drift apart silently. Read
[SOURCE_API_FIDELITY_AUDIT.md](SOURCE_API_FIDELITY_AUDIT.md) before trusting any source's spec.
None of it has been exercised against a live vendor account.

**Correction (2026-07-28):** an earlier version of this section said they were "code-complete in
the same sense Sage and NetSuite are". That was wrong and materially so. Sage and NetSuite are
imported by the extraction handler, so the registry can resolve them; the ten new adapters were
imported only by their tests, which meant `resolve_builder()` raised `KeyError` for every one of
them at runtime. They are now imported by the handler and
`connector_runtime/tests/test_handler_connector_reachability.py` asserts — importing *only* the
handler — that all fourteen resolve. **None has credentials, and none is connected**, which is the
remaining and accurate statement. WellSky and SeniorPlace are marked PHI-bearing and are gated by
`portability/phi_gate.py`, which fails closed on an unclassified field.

---

## Live Data (Dev)

Verified as of 2026-07-09, for the two connected sources: **34 Salesforce accounts** and
**36,023 MySQL RDS contract rows** extracted, transformed, resolved, and published — Athena
returns real query results against `datalake_analytics_dev` for these entities. Per-entity counts for
`salesforce-contact` were not re-confirmed with an exact number in this pass; treat the two
figures above as the solid data point and re-check row counts directly
(`aws s3 ls` / Athena `SELECT COUNT(*)`) before quoting others.

`datalake_curated_dev` (the Glue database, not the S3 layer) stays empty regardless — see the Glue
Catalog section below for why; only `datalake_analytics_dev` tables get registered at pipeline runtime,
by `analytics_publisher/analytics_publisher_handler.py`, not by Terraform.

The three newer entities (`salesforce-opportunity`, `salesforce-contract`,
`mysql-rds-contractterms`) are config-complete but **not yet seeded or scheduled** — no data
exists for them yet. Sage Intacct, Sage X3, and NetSuite have no populated credentials, so
nothing has run for those sources either.

Path to live data for the remaining sources/entities: populate real credentials in the Secrets
Manager shells below, seed entity configs (`scripts/seed_entity_config.py`), enable and sync
schedules (`scripts/seed_schedules.py`), then either wait for a scheduled run or trigger one
manually (`scripts/trigger_extraction.py`).

---

## Connected Data Sources

All five credential secrets are Terraform-managed shells (`terraform apply` creates the secret
resource but does not populate a value). Salesforce and MySQL RDS have had real credentials
written via `aws secretsmanager put-secret-value` and are confirmed reachable. Sage Intacct,
Sage X3, and NetSuite are still empty shells — not reachable until real credentials are written.

| Source | Status | Entities | Extraction mode |
|---|---|---|---|
| **Salesforce CRM** | ✅ Connected, verified live | `salesforce-account` (companies, live), `salesforce-contact` (persons, live), `salesforce-opportunity` (opportunities, configured — not yet seeded), `salesforce-contract` (sales contracts, configured — not yet seeded) | Incremental (watermark: `SystemModstamp`) |
| **MySQL RDS** | ✅ Connected, verified live | `mysql-rds-contracts` (contracts, live — 36,023 rows), `mysql-rds-contractterms` (contract terms, configured — not yet seeded) | Incremental (watermark: `ModifiedOn`, tombstone soft-delete on `is_deleted`) |
| **Sage Intacct** | 🟡 Code-complete, not connected | `sage-intacct-customer` (companies), `sage-intacct-vendor` (suppliers), `sage-intacct-arinvoice` (AR invoices), `sage-intacct-apbill` (AP bills) | Incremental |
| **Sage X3** | 🟡 Code-complete, not connected | `sage-x3-customer` (companies), `sage-x3-supplier` (suppliers) | Incremental |
| **NetSuite ERP** | 🟡 Code-complete, not connected | Connector, auth client, SuiteQL query planner, and raw layer writer are all implemented (`connector_runtime/adapters/netsuite/`). SuiteQL offset/limit pagination hard-stops with an actionable error before requesting `offset > 100,000` — NetSuite's real pagination ceiling. Full keyset-pagination (paginate by a monotonic column instead of offset/limit) is not yet implemented; keep the watermark increment tight enough that a single run's result set stays under 100,000 rows. | Incremental (SuiteQL) |

> `salesforce-opportunity`, `salesforce-contract`, and `mysql-rds-contractterms` are new as of this
> pass — field-mapping config (`config/field_mappings/salesforce/salesforce-opportunity/`,
> `salesforce-contract/`, `config/field_mappings/mysql-rds/mysql-rds-contractterms/`) and
> entity-resolution config (`config/entity_resolution/opportunity/`, `sales-contract/`,
> `contract-term/`) exist, and all three are wired into
> `entity_resolution/entity_type_registry.py`'s fallback seed dicts and
> `scripts/seed_entity_config.py`. No new connector code was needed — the Salesforce and MySQL RDS
> adapters are generic (object/table name comes from config), not per-entity — but nothing is
> seeded into DynamoDB or scheduled in dev yet, so treat these as config-complete, not live.

---

## AWS Resources — Dev Environment

**AWS Account:** `087972550871` | **Region:** `us-east-1`

### S3 Buckets

| Bucket | Pipeline stage | Written by | Read by | Purpose |
|---|---|---|---|---|
| `datalake-raw-dev-use1` | Stage A — Extraction | Extraction Lambda | Transformation Lambda | Immutable raw Parquet files written once per extraction run. One Hive-partitioned prefix per entity per date. Never overwritten; watermark prevents re-extraction of unchanged data. |
| `datalake-curated-dev-use1` | Stage B — Transformation | Transformation Lambda | Entity Resolution Lambda, Athena (Athena access not wired in dev today — see Glue Catalog section) | Field-mapped, quality-checked Parquet (canonical column names). Also stores field-mapping JSON config files under `field-mappings/{source_id}/{entity_id}/`. |
| `datalake-analytics-dev-use1` | Stages C & D — Entity Resolution + Analytics | Entity Resolution Lambda, Analytics Publisher Lambda | Athena, downstream BI tools | Golden (de-duplicated) records from entity resolution and consumption-optimised Parquet for analytics. Registered in Glue Catalog for Athena queries. |
| `datalake-schema-snapshots-dev-use1` | Stage A — Extraction (post-extract) | Extraction Lambda | Drift Evaluation (same Lambda) | JSON schema fingerprints captured after every extraction. The drift evaluator compares the new snapshot against `latest.json` to detect breaking changes (added/removed/type-changed columns). Path: `{tenant_code}/{source_id}/{entity_id}/{schema_version}/{extraction_date}.json` + `drift-report-{extraction_date}.json`; latest-pointer at `{tenant_code}/{source_id}/{entity_id}/latest.json`. |
| `datalake-access-logs-dev-use1` | All stages (passive) | AWS S3 service (automatic) | Security & compliance audits | Receives S3 server access logs from every other data lake bucket. Never written to directly by pipeline code. Used for access auditing, cost attribution, and compliance. Retention: 30 days (dev). |
| `datalake-terraform-state-dev-use1` | Infrastructure (deploy time only) | Terraform CLI, `make lambda-upload` | Terraform CLI, Lambda service at deploy | Dual-purpose: (1) Terraform remote state file (`environments/dev/terraform.tfstate`) with DynamoDB lock for single-writer safety. (2) Lambda artifact store — `lambda/extraction-pipeline.zip` uploaded here by `make lambda-upload` and pulled by Lambda on every `terraform apply`. Not accessed at pipeline runtime. |

### S3 Key Patterns

| Layer | Pattern |
|---|---|
| Raw | `s3://datalake-raw-dev-use1/{tenant_code}/{source}/{entity_id}/extraction_date=YYYY-MM-DD/run_id={run_id}/data.parquet` — tenant-prefixed root segment (ARCH-1), then one hyphenated source segment (`salesforce`, `netsuite`, `mysql-rds`, `sage-intacct`, `sage-x3`); no `raw/` root segment (`connector_runtime/raw_layer_writer.py::RawLayerWriter._partition_path`). App-level convention, not yet IAM/bucket-policy-enforced |
| Curated | `s3://datalake-curated-dev-use1/{tenant_code}/curated/{domain}/{entity_id}/curated_date=YYYY-MM-DD/run_id={run_id}/data.parquet` |
| Golden records | `s3://datalake-analytics-dev-use1/{tenant_code}/canonical/{entity_type}/golden_date={date}/run_id={run_id}/golden.parquet` |
| Analytics | `s3://datalake-analytics-dev-use1/{tenant_code}/analytics/{entity_type}/analytics_date=YYYY-MM-DD/data.parquet` |
| Entity config (S3 backend, alternate to DynamoDB) | `s3://<config-bucket>/{tenant_code}/{source_id}/{entity_id}/config.json` |

> `{tenant_code}` defaults to `demo` (`contracts/identifier_policy.DEFAULT_TENANT_CODE`) and is
> always present in every pattern above — there is no unprefixed legacy mode left for any layer.
> This is prefix-level isolation only, enforced by writer code, not yet backed by an S3
> bucket-policy IAM condition — don't treat it as a hard security boundary until that lands (see
> `docs/KNOWN_GAPS_AND_ROADMAP.md`).

> **Curated layer — SCD Type 1 merge:** For incremental entities with `primary_key_field` set, each curated partition holds the **full current state** of all records (not just the day's delta). This ensures entity resolution always sees complete data. Deleted records are retained as tombstones (`is_deleted=True`) rather than physically removed.

> **Curated layer — Glue table naming:** The curated Glue table
> name is now tenant-scoped — `{tenant_code}_{entity_id}_{domain}_curated`
> (`transformation/transformation_pipeline.py::_register_curated_catalog`, line ~751). Previously
> the table name carried no tenant segment, so two tenants running the same `entity_id`/`domain`
> registered the *same* Glue table and silently overwrote each other's `Location` — the second
> tenant's transformation run would repoint the first tenant's Athena table at its own data
> (now fixed — table names are tenant-scoped). The
> `curated_date` partition is now also registered per run via `glue_client.create_partition()`
> (falling back to `update_partition()` on `AlreadyExistsException`) immediately after catalog
> registration — previously the table declared `partition_keys=("curated_date",)` but no
> partition value was ever registered, so partitioned Athena queries returned zero rows until a
> manual `MSCK REPAIR TABLE`.
>
> **This code path is currently unreachable in dev** — see the Glue Catalog section below;
> `glue_catalog_database` is not set on the dev `transformation_lambda` module, so
> `_register_curated_catalog` is never invoked there today regardless of how correct its logic is.

### DynamoDB Tables

Five of the six tables below are Terraform-managed with `lifecycle { prevent_destroy = true }`
(`infrastructure/modules/metadata_persistence/main.tf`) — never create any of them by hand.
`datalake-source-onboarding-registry-dev` is Terraform-managed too but does **not** have `prevent_destroy` set
(confirmed by reading the resource block directly) — a `terraform destroy`/replace on that table
is not blocked the way it is for the other five.

| Table | Purpose | Hash key | Terraform resource |
|---|---|---|---|
| `datalake-entity-extraction-config-dev` | Entity extraction configuration (source, watermark field, load type, tenant_code, etc.). The `source_id` attribute holds `tenant_scoped_key(tenant_code, connection_id)` — e.g. `"demo#salesforce"` — since the tenant-key migration was applied to dev on 2026-07-24; for a single-connection source `connection_id == source_id`, which is what kept every existing key byte-identical | `source_id` (tenant+connection-scoped composite) + `entity_id` (range) | `aws_dynamodb_table.entity_extraction_config` |
| `datalake-watermark-dev` | Per-entity watermark timestamps for incremental loads. The DynamoDB **key itself is tenant-scoped** — `WatermarkRepository` stores `tenant_scoped_key(tenant_code, source_id)` (e.g. `"demo#salesforce"`) as the `source_id` attribute, not just an application-level guard checked on read | `source_id` (tenant-scoped composite) + `entity_id` (range) | `aws_dynamodb_table.watermark_repository` |
| `datalake-run-audit-log-dev` | Immutable audit record of every pipeline run (including partial/checkpointed runs). `source-entity-time-index` GSI hash key (`source_entity_key`) is tenant-scoped as `{tenant_code}#{source_id}#{entity_id}`, populated for every run, not just DLQ-routed failures | `run_id` + `stage` (range) | `aws_dynamodb_table.run_audit_log` |
| `datalake-entity-type-registry-dev` | Tenant-scoped entity-type/entity-id registry (`entity_resolution/entity_type_registry.py::EntityTypeRegistryClient`) — supersedes the old hardcoded dicts, which remain as fallback seed data | `tenant_code` + `sk` (range) | `aws_dynamodb_table.entity_type_registry` |
| `datalake-source-onboarding-registry-dev` | Tracks onboarding-gate state (registration → gate transitions → activation) **per `source_id`, not per tenant** (`governance/source_onboarding_registry.py::SourceOnboardingRegistryClient`) — a source-level certification workflow (see `connector_runtime/certification/connector_certification_checklist.py`), distinct from the control-plane API's tenant/entity registration flow. Not currently called from `connector_runtime/api/` — no route in the Control Plane API section below reads or writes this table today. | `source_id` (no range key) | `aws_dynamodb_table.source_onboarding_registry` |
| `datalake-serving-store-config-dev` | Which tenant/entity_type pairs load into a serving store, and into which engine (`serving_store/serving_store_config_repository.py::ServingStoreConfigRepositoryClient`) — tenant-partitioned from creation, no `tenant_scoped_key()` composite-key needed. Keyed by `entity_type` (the analytics-layer entity, e.g. `company`), not a source-level `entity_id` — one entity_type's analytics dataset can be fed by several contributing sources. **Created and deployed in dev** (2026-07-24) but **empty** — no tenant/entity onboarded yet, so the loader skips every run (see Stage 16 in `docs/PIPELINE_FLOW.md`; onboard via `scripts/seed_serving_store_config.py`) | `tenant_code` + `entity_type` (range) | `aws_dynamodb_table.serving_store_config` |

> Table names above follow the `datalake<Table>` PascalCase convention, with no environment prefix —
> each environment (dev/staging/prod) lives in its own separate AWS account, so the account
> boundary provides isolation instead of a name prefix.

### Lambda Functions

| Function | Handler | Purpose |
|---|---|---|
| `datalake-extraction-dev` | `connector_runtime.extraction_pipeline_handler.lambda_handler` | Extract from source → raw layer. Now supports mid-run checkpointing on approaching Lambda timeout (`LambdaTimeoutWarning`, partial watermark commit) — see Known Gotchas in `docs/DEVELOPER_GUIDE.md`. |
| `datalake-transformation-dev` | `transformation.transformation_pipeline_handler.lambda_handler` | Raw → curated layer (tenant-prefixed S3 keys) |
| `datalake-entity-resolution-dev` | `entity_resolution.entity_resolution_pipeline_handler.lambda_handler` | Curated → golden records; now streams curated records via DuckDB rather than fully materializing them, and resolves entity types via `EntityTypeRegistryClient` (DynamoDB) with fallback to hardcoded seed dicts |
| `datalake-analytics-devLayerPublisher` | `analytics_publisher.analytics_publisher_handler.lambda_handler` | Golden records → analytics layer; emits an end-to-end pipeline SLA metric. |
| `datalake-control-plane-dev` | `connector_runtime.api.control_plane_handler.lambda_handler` | SaaS control-plane REST API behind a Cognito/JWT authorizer — entity registration/listing, pipeline trigger, run status, plus the config/semantic-governance routes in `api/config_governance_routes.py`. **No tenant/user/role provisioning route** — identity is owned by the Identity API (see the root `CLAUDE.md` system boundary); the deployed function still carries the older code until redeployed. Deployed; end-to-end request flow against the live API Gateway + Cognito authorizer has not yet been exercised. |
| `datalake-credential-expiry-notifier-dev` | `connector_runtime.credential_rotation.credential_expiry_notifier_handler.lambda_handler` | Daily check of all 5 source-credential secrets' age; publishes an SNS alert if rotation is overdue. |
| `datalake-pipeline-trigger-dev` | `orchestration.pipeline_trigger.pipeline_trigger_handler.lambda_handler` | Rate-limited SQS FIFO consumer that starts Step Functions executions — the single path both `scripts/trigger_extraction.py` and the control-plane API's pipeline-trigger route funnel through. |
| `datalake-dlq-processor-dev` | `orchestration.dlq_processor.dlq_processor_handler.lambda_handler` | Processes the extraction-failure DLQ: writes an audit record, sends an SNS alert, and optionally auto-replays (`AUTO_REPLAY=false` by default). |

All eight Lambdas are deployed from the **same zip** (via `var.lambda_package_s3_bucket` /
`lambda_package_s3_key`, shared across every module): `s3://datalake-terraform-state-dev-use1/lambda/extraction-pipeline.zip`

### Control Plane API

| Resource | Value |
|---|---|
| API Gateway (HTTP API) endpoint | `https://qy5g7az09f.execute-api.us-east-1.amazonaws.com` — **not re-verified this session**; this is a runtime value assigned at `terraform apply` and isn't derivable from HCL source. Confirm with `terraform output` or the API Gateway console before relying on it. |
| Cognito User Pool ID | `us-east-1_7tjRZnlIa` — same caveat as above, unverified this session |
| Routes | `POST /tenants`, `GET/POST /tenants/{tenant_code}/entities`, `POST /tenants/{tenant_code}/pipelines/trigger`, `GET /tenants/{tenant_code}/runs/{run_id}`, `GET /tenants/{tenant_code}/runs` (`infrastructure/modules/control_plane/main.tf` route map, confirmed against source) |

### Networking

| Resource | Value |
|---|---|
| VPC ID | `vpc-0698799f8ec063837` — unverified this session, see caveat above |
| Private subnet IDs | `subnet-0f5556d36179a7e83`, `subnet-01eee5cd6d9cad41a` — unverified this session |
| NAT Gateway public IP | `52.203.144.191` (allowlist this in Salesforce/NetSuite/MySQL RDS security groups before extraction can reach those sources) — unverified this session |

### Step Functions

| State Machine | Purpose |
|---|---|
| `datalake-extraction-dev` | Full end-to-end pipeline: extraction → transformation → entity resolution → analytics → serving store (optional). Single state machine for all four stages. Now includes an `ExtractionCheckpointed` terminal `Succeed` state, reached via a `Catch` on `LambdaTimeoutWarning` — the extraction stage commits a partial watermark and exits cleanly rather than failing when it detects it's about to hit the Lambda timeout. **Automatic re-invocation from the checkpoint is not yet implemented** — a checkpointed run currently needs a manual re-trigger (see `docs/KNOWN_GAPS_AND_ROADMAP.md`). The `LoadServingStore` state (`infrastructure/modules/orchestration/main.tf`) is a conditional `Task`/`Pass` — a `Pass` (no-op) unless `serving_store_loader_lambda_arn` is set. The `serving_store/` module (adapter+registry pattern, five engine loaders — MySQL RDS, PostgreSQL, SQL Server, Azure SQL, Redshift — behind `ServingStoreLoaderRegistry`) and its Terraform (`infrastructure/modules/serving_store_database`, `infrastructure/modules/serving_store_lambda`, and `infrastructure/modules/serving_store_redshift`) are code-complete and wire that ARN automatically in all three environments' `main.tf`. **In dev this has been `terraform apply`'d (2026-07-24)**: `serving_store_loader_lambda_arn` resolves to the live `datalake-serving-store-loader-dev` Lambda, so the state takes the `Task` branch there (staging/prod remain un-applied → `Pass`). The `Task` branch threads `"tenant_code.$" = "$.tenant_code"` and `"entity_type.$" = "$.analytics.entity_type"` through (the analytics-layer entity type produced by the prior stage, not the source-level `entity_id` that triggered the run — kept alongside it for tracing only), and the loader's tenant isolation is real — database-per-tenant (MySQL) or schema-per-tenant (PostgreSQL/SQL Server/Azure SQL/Redshift), enforced at the database engine's own GRANT model. **The dev serving store is deployed but unpopulated: `datalake-serving-store-config-dev` has no rows, so the loader skips every run and the `datalake-serving-store-mysql-dev` RDS instance holds no databases or tables. Onboard a tenant/entity with `scripts/seed_serving_store_config.py`.** Redshift is the exception to the row-upsert path: it is a columnar MPP warehouse (Serverless), so its adapter loads set-based via `COPY` from the analytics Parquet in S3 (`supports_s3_bulk_load`) with an IAM-auth writer — no writer password — rather than driver row upserts. |

### EventBridge Scheduler

| Schedule | Target | Purpose |
|---|---|---|
| `datalake-credential-expiry-check-dev` | `datalake-credential-expiry-notifier-dev` | **New.** Daily (`rate(1 day)`) check of source-credential secret age across all 5 sources. |

Per-entity extraction schedules (one per tenant/source/entity, created via
`orchestration/event_bridge/extraction_schedule_client.py`) are named
`{tenant_code}--{source_id}--{entity_id}` — double-hyphen throughout, specifically because a
single-hyphen delimiter between `tenant_code` and `source_id` would let two
distinct tenants collide on one literal schedule name whenever either field itself contained a
hyphen (e.g. `tenant="acme"`/`source="corp-salesforce"` vs. `tenant="acme-corp"`/`source="salesforce"`)
— a silent cross-tenant schedule clobber, since `create_or_update_schedule()` is update-first. For
tenant/source/entity combinations whose joined name would exceed EventBridge Scheduler's 64-character
limit, the name is deterministically collapsed to a truncated prefix plus a SHA-256 content-hash
suffix, never a naive slice (`_build_schedule_name()`).

### Dead-Letter Queue

| Resource | Purpose |
|---|---|
| `datalake-extraction-failure-dlq-dev` (SQS, `aws_sqs_queue.extraction_failure_dlq` in `infrastructure/modules/metadata_persistence/main.tf`) | Failed extraction runs land here; `datalake-dlq-processor-dev` drains it into an audit record + SNS alert, with optional auto-replay. |

### Glue Catalog

| Database | Wired to | Tables |
|---|---|---|
| `datalake_curated_dev` | **Nothing, in dev today.** `infrastructure/modules/transformation_lambda/main.tf`'s `glue_catalog_database` variable defaults to `""` (registration disabled) and `infrastructure/environments/dev/main.tf`'s `module.transformation_lambda` block never sets it. | Created by Terraform; permanently empty in dev until someone wires this variable. The curated-registration code path (`transformation/transformation_pipeline.py::_register_curated_catalog`, one table per `{tenant_code}_{entity_id}_{domain}_curated`) exists and is exercised by tests, but is currently dead code in the deployed dev Lambda. |
| `datalake_analytics_dev` | `module.analytics_publisher_lambda` (`glue_catalog_database = module.glue.analytics_database_name` in `infrastructure/environments/dev/main.tf`) | Created by Terraform; empty until a pipeline run completes. Tables are registered by `analytics_publisher/analytics_publisher_handler.py` (not `_register_curated_catalog`) after a full run reaches the analytics-publish stage. |

> Both Glue databases are provisioned in every environment (`infrastructure/modules/glue/main.tf`),
> but only `datalake_analytics_dev` is actually reachable by a running Lambda in dev right now — don't assume
> curated-layer tables will appear in Athena without first wiring `glue_catalog_database` on the
> transformation Lambda module block.

### Secrets Manager

| Secret | Contents |
|---|---|
| `datalake/<env>/sources/salesforce/credentials` | `instance_url`, `client_id`, `client_secret` |
| `datalake/<env>/sources/netsuite/credentials` | NetSuite OAuth/TBA credentials — connector is code-complete (`connector_runtime/adapters/netsuite/`); confirm this secret is actually populated before assuming live extraction |
| `datalake/<env>/sources/mysql-rds/credentials` | `host`, `port`, `username`, `password`, `database` |
| `datalake/<env>/sources/sage/intacct/credentials` | `token_url`, `client_id`, `client_secret`, `base_url`, `company_id` |
| `datalake/<env>/sources/sage/x3/credentials` | `token_url`, `client_id`, `client_secret`, `base_url`, `folder` |

> All five secrets above are now Terraform-managed (`infrastructure/modules/secrets/main.tf`),
> including Sage's — each has a resource policy (`DenyAllOtherPrincipals`) restricting
> `GetSecretValue` to the extraction runtime role only. `terraform apply` creates the
> empty secret **shells**; the actual credential values still need to be populated by hand via
> `aws secretsmanager put-secret-value` — don't assume `terraform apply` alone makes a source live.
>
> **Credential rotation monitoring**: `datalake-credential-expiry-notifier-dev` (daily) checks the age
> of all five secrets above and alerts via SNS if rotation is overdue.

---

## Terraform State

| Item | Value |
|---|---|
| Backend | S3 remote state |
| State bucket | `datalake-terraform-state-dev-use1` |
| State key | `environments/dev/terraform.tfstate` |
| Lock table | `datalake-terraform-state-lock-dev` |

---

## Next Steps

### Onboard a source in Dev

No entity config is seeded yet for any source. To bring one online (Salesforce, MySQL RDS, Sage
Intacct, Sage X3, or NetSuite):

- Populate the relevant Secrets Manager shell (see Secrets Manager section above) via
  `aws secretsmanager put-secret-value`
- Seed its entity config records via `scripts/seed_entity_config.py`
- Set `schedule_enabled=True` for its entities and sync schedules via `scripts/seed_schedules.py`
- Trigger a dry-run first, e.g. `python scripts/run_sage_connector_local.py --entity-id sage-intacct-customer --dry-run`

### Deploy Staging Environment

`terraform validate` is clean for `staging`. Prerequisites:
- Staging AWS account and credentials
- Terraform state bootstrap (state bucket, lock table, KMS key — `docs/DEPLOYMENT_GUIDE.md` Phase 1,
  including the orphaned-resource check in Step 1.6)
- `ARTIFACTS_BUCKET=datalake-terraform-state-{staging_account_id} make lambda-deploy` once the staging
  account ID is known (always as a single command — see `infrastructure/CLAUDE.md`)

Apply order (see `infrastructure/CLAUDE.md` for the full module list and dependency mechanics):

```bash
cd infrastructure/environments/staging
terraform init
terraform apply -target=module.iam
terraform apply -target=module.metadata_persistence
terraform apply -target=module.lambda_pipeline -target=module.transformation_lambda -target=module.entity_resolution_lambda -target=module.analytics_publisher_lambda
terraform apply -target=module.orchestration
terraform apply -target=module.control_plane
```

### Deploy Production Environment

Same pattern as staging — use `datalake-terraform-state-{prod_account_id}` as `ARTIFACTS_BUCKET`. Lambda
log retention is already configured at 365 days in HCL. `terraform apply`/`destroy` against
`infrastructure/environments/prod` requires explicit operator sign-off outside of automated tooling.

### Onboard NetSuite

`connector_runtime/adapters/netsuite/` has a full implementation (connector, OAuth/TBA auth
client, SuiteQL query planner, raw layer writer). Remaining steps are configuration-only:

- Populate `datalake/<env>/sources/netsuite/credentials` with real OAuth/TBA credentials
- Seed entity config for NetSuite entities via `seed_entity_config.py`
- Set `schedule_enabled=True` once ready for live scheduled extraction
