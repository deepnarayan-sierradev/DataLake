# Platform Status — Enterprise Data Lake

**Last updated:** 2026-07-14
**Prepared by:** Platform Engineering

> **Multi-tenancy note:** `tenant_code` is a first-class concept (default: `demo`, from
> `contracts/identifier_policy.DEFAULT_TENANT_CODE`), prefixed into S3 keys for every data-plane
> layer except the raw layer, and genuinely key-scoped in the watermark and entity-type-registry
> DynamoDB tables. See `docs/PIPELINE_FLOW.md`'s canonical isolation-model table for the full
> layer-by-layer picture, and `docs/KNOWN_GAPS_AND_ROADMAP.md` for what's still open (no IAM
> enforcement anywhere, the raw-layer gap, shared Secrets Manager credentials, Glue/Athena's
> wildcard grant).

---

## Current Status: Dev ✅ (infrastructure) | Staging 🔲 | Production 🔲

| Environment | Status | Notes |
|---|---|---|
| **Dev** | ✅ Infrastructure deployed, pipeline verified live | All 8 Lambda functions, Step Functions state machine, control-plane API (Cognito + API Gateway), DynamoDB tables, S3 buckets, SQS queues, EventBridge Scheduler group — deployed fresh on 2026-07-09 (an earlier "live" claim for this account had gone stale; the account was found empty and redeployed from scratch). Salesforce and MySQL RDS credentials are populated and the extraction → transformation → entity resolution → analytics pipeline has run end-to-end with real data (see Live Data below). Sage Intacct, Sage X3, and NetSuite credentials are still empty shells — those sources are code-complete but not connected. |
| **Staging** | 🔲 Not provisioned | `terraform validate` is clean. No AWS account/credentials provisioned yet. |
| **Production** | 🔲 Not provisioned | `terraform validate` is clean. Requires staging sign-off first per this repo's promotion policy. |

---

## Live Data (Dev)

Verified as of 2026-07-09, for the two connected sources: **34 Salesforce accounts** and
**36,023 MySQL RDS contract rows** extracted, transformed, resolved, and published — Athena
returns real query results against `edl_analytics` for these entities. Per-entity counts for
`salesforce-contact` were not re-confirmed with an exact number in this pass; treat the two
figures above as the solid data point and re-check row counts directly
(`aws s3 ls` / Athena `SELECT COUNT(*)`) before quoting others.

`edl_curated` (the Glue database, not the S3 layer) stays empty regardless — see the Glue
Catalog section below for why; only `edl_analytics` tables get registered at pipeline runtime,
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
| `edl-raw-087972550871` | Stage A — Extraction | Extraction Lambda | Transformation Lambda | Immutable raw Parquet files written once per extraction run. One Hive-partitioned prefix per entity per date. Never overwritten; watermark prevents re-extraction of unchanged data. |
| `edl-curated-087972550871` | Stage B — Transformation | Transformation Lambda | Entity Resolution Lambda, Athena (Athena access not wired in dev today — see Glue Catalog section) | Field-mapped, quality-checked Parquet (canonical column names). Also stores field-mapping JSON config files under `field-mappings/{source_id}/{entity_id}/`. |
| `edl-analytics-087972550871` | Stages C & D — Entity Resolution + Analytics | Entity Resolution Lambda, Analytics Publisher Lambda | Athena, downstream BI tools | Golden (de-duplicated) records from entity resolution and consumption-optimised Parquet for analytics. Registered in Glue Catalog for Athena queries. |
| `edl-schema-snapshots-087972550871` | Stage A — Extraction (post-extract) | Extraction Lambda | Drift Evaluation (same Lambda) | JSON schema fingerprints captured after every extraction. The drift evaluator compares the new snapshot against `latest.json` to detect breaking changes (added/removed/type-changed columns). Path: `{tenant_code}/{source_id}/{entity_id}/{schema_version}/{extraction_date}.json` + `drift-report-{extraction_date}.json`; latest-pointer at `{tenant_code}/{source_id}/{entity_id}/latest.json`. |
| `edl-access-logs-087972550871` | All stages (passive) | AWS S3 service (automatic) | Security & compliance audits | Receives S3 server access logs from every other data lake bucket. Never written to directly by pipeline code. Used for access auditing, cost attribution, and compliance. Retention: 30 days (dev). |
| `edl-terraform-state-087972550871` | Infrastructure (deploy time only) | Terraform CLI, `make lambda-upload` | Terraform CLI, Lambda service at deploy | Dual-purpose: (1) Terraform remote state file (`environments/dev/terraform.tfstate`) with DynamoDB lock for single-writer safety. (2) Lambda artifact store — `lambda/extraction-pipeline.zip` uploaded here by `make lambda-upload` and pulled by Lambda on every `terraform apply`. Not accessed at pipeline runtime. |

### S3 Key Patterns

| Layer | Pattern |
|---|---|
| Raw | `s3://edl-raw-087972550871/{source}/{entity_id}/extraction_date=YYYY-MM-DD/run_id={run_id}/data.parquet` — one hyphenated source segment (`salesforce`, `netsuite`, `mysql-rds`, `sage-intacct`, `sage-x3`); no `raw/` root segment and **not tenant-prefixed** (`connector_runtime/raw_layer_writer.py::RawLayerWriter._partition_path`) — see `docs/KNOWN_GAPS_AND_ROADMAP.md` |
| Curated | `s3://edl-curated-087972550871/{tenant_code}/curated/{domain}/{entity_id}/curated_date=YYYY-MM-DD/run_id={run_id}/data.parquet` |
| Golden records | `s3://edl-analytics-087972550871/{tenant_code}/canonical/{entity_type}/golden_date={date}/run_id={run_id}/golden.parquet` |
| Analytics | `s3://edl-analytics-087972550871/{tenant_code}/analytics/{entity_type}/analytics_date=YYYY-MM-DD/data.parquet` |
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

Four of the five tables below are Terraform-managed with `lifecycle { prevent_destroy = true }`
(`infrastructure/modules/metadata_persistence/main.tf`) — never create any of them by hand.
`EdlSourceOnboardingRegistry` is Terraform-managed too but does **not** have `prevent_destroy` set
(confirmed by reading the resource block directly) — a `terraform destroy`/replace on that table
is not blocked the way it is for the other four.

| Table | Purpose | Hash key | Terraform resource |
|---|---|---|---|
| `EdlEntityExtractionConfig` | Entity extraction configuration (source, watermark field, load type, tenant_code, etc.) | `source_id` + `entity_id` (range) | `aws_dynamodb_table.entity_extraction_config` |
| `EdlWatermarkRepository` | Per-entity watermark timestamps for incremental loads. The DynamoDB **key itself is tenant-scoped** — `WatermarkRepository` stores `tenant_scoped_key(tenant_code, source_id)` (e.g. `"demo#salesforce"`) as the `source_id` attribute, not just an application-level guard checked on read | `source_id` (tenant-scoped composite) + `entity_id` (range) | `aws_dynamodb_table.watermark_repository` |
| `EdlRunAuditLog` | Immutable audit record of every pipeline run (including partial/checkpointed runs). `source-entity-time-index` GSI hash key (`source_entity_key`) is tenant-scoped as `{tenant_code}#{source_id}#{entity_id}`, populated for every run, not just DLQ-routed failures | `run_id` + `stage` (range) | `aws_dynamodb_table.run_audit_log` |
| `EdlEntityTypeRegistry` | Tenant-scoped entity-type/entity-id registry (`entity_resolution/entity_type_registry.py::EntityTypeRegistryClient`) — supersedes the old hardcoded dicts, which remain as fallback seed data | `tenant_code` + `sk` (range) | `aws_dynamodb_table.entity_type_registry` |
| `EdlSourceOnboardingRegistry` | Tracks onboarding-gate state (registration → gate transitions → activation) **per `source_id`, not per tenant** (`governance/source_onboarding_registry.py::SourceOnboardingRegistryClient`) — a source-level certification workflow (see `connector_runtime/certification/connector_certification_checklist.py`), distinct from the control-plane API's tenant/entity registration flow. Not currently called from `connector_runtime/api/` — no route in the Control Plane API section below reads or writes this table today. | `source_id` (no range key) | `aws_dynamodb_table.source_onboarding_registry` |

> Table names above follow the `Edl<Table>` PascalCase convention, with no environment prefix —
> each environment (dev/staging/prod) lives in its own separate AWS account, so the account
> boundary provides isolation instead of a name prefix.

### Lambda Functions

| Function | Handler | Purpose |
|---|---|---|
| `EdlExtractionPipeline` | `connector_runtime.extraction_pipeline_handler.lambda_handler` | Extract from source → raw layer. Now supports mid-run checkpointing on approaching Lambda timeout (`LambdaTimeoutWarning`, partial watermark commit) — see Known Gotchas in `docs/DEVELOPER_GUIDE.md`. |
| `EdlTransformationPipeline` | `transformation.transformation_pipeline_handler.lambda_handler` | Raw → curated layer (tenant-prefixed S3 keys) |
| `EdlEntityResolutionPipeline` | `entity_resolution.entity_resolution_pipeline_handler.lambda_handler` | Curated → golden records; now streams curated records via DuckDB rather than fully materializing them, and resolves entity types via `EntityTypeRegistryClient` (DynamoDB) with fallback to hardcoded seed dicts |
| `EdlAnalyticsLayerPublisher` | `analytics_publisher.analytics_publisher_handler.lambda_handler` | Golden records → analytics layer; emits an end-to-end pipeline SLA metric. |
| `EdlControlPlane` | `connector_runtime.api.control_plane_handler.lambda_handler` | SaaS control-plane REST API behind a Cognito/JWT authorizer — tenant provisioning, entity registration/listing, pipeline trigger, run status. Deployed; end-to-end request flow against the live API Gateway + Cognito authorizer has not yet been exercised. |
| `EdlCredentialExpiryNotifier` | `connector_runtime.credential_rotation.credential_expiry_notifier_handler.lambda_handler` | Daily check of all 5 source-credential secrets' age; publishes an SNS alert if rotation is overdue. |
| `EdlPipelineTrigger` | `orchestration.pipeline_trigger.pipeline_trigger_handler.lambda_handler` | Rate-limited SQS FIFO consumer that starts Step Functions executions — the single path both `scripts/trigger_extraction.py` and the control-plane API's pipeline-trigger route funnel through. |
| `EdlDlqProcessor` | `orchestration.dlq_processor.dlq_processor_handler.lambda_handler` | Processes the extraction-failure DLQ: writes an audit record, sends an SNS alert, and optionally auto-replays (`AUTO_REPLAY=false` by default). |

All eight Lambdas are deployed from the **same zip** (via `var.lambda_package_s3_bucket` /
`lambda_package_s3_key`, shared across every module): `s3://edl-terraform-state-087972550871/lambda/extraction-pipeline.zip`

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
| `EdlExtractionPipeline` | Full end-to-end pipeline: extraction → transformation → entity resolution → analytics → serving store (optional). Single state machine for all four stages. Now includes an `ExtractionCheckpointed` terminal `Succeed` state, reached via a `Catch` on `LambdaTimeoutWarning` — the extraction stage commits a partial watermark and exits cleanly rather than failing when it detects it's about to hit the Lambda timeout. **Automatic re-invocation from the checkpoint is not yet implemented** — a checkpointed run currently needs a manual re-trigger (see `docs/KNOWN_GAPS_AND_ROADMAP.md`). The `LoadServingStore` state (`infrastructure/modules/orchestration/main.tf`) is a conditional `Task`/`Pass` — a `Pass` (no-op) unless `serving_store_loader_lambda_arn` is set. The `serving_store/` module (adapter+registry pattern, four engine loaders — MySQL RDS, PostgreSQL, SQL Server, Azure SQL — behind `ServingStoreLoaderRegistry`) and its Terraform (`infrastructure/modules/serving_store_database`, `infrastructure/modules/serving_store_lambda`) are code-complete and wire that ARN automatically in all three environments' `main.tf`, but none of it has been `terraform apply`'d anywhere, so `serving_store_loader_lambda_arn` still resolves empty today and the state still takes the `Pass` branch. The `Task` branch threads `"tenant_code.$" = "$.tenant_code"` through, and the loader's tenant isolation is now real — database-per-tenant (MySQL) or schema-per-tenant (PostgreSQL/SQL Server/Azure SQL), enforced at the database engine's own GRANT model — code-complete, unexercised in any live deployment. |

### EventBridge Scheduler

| Schedule | Target | Purpose |
|---|---|---|
| `EdlCredentialExpiryCheck` | `EdlCredentialExpiryNotifier` | **New.** Daily (`rate(1 day)`) check of source-credential secret age across all 5 sources. |

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
| `EdlExtractionFailureDlq` (SQS, `aws_sqs_queue.extraction_failure_dlq` in `infrastructure/modules/metadata_persistence/main.tf`) | Failed extraction runs land here; `EdlDlqProcessor` drains it into an audit record + SNS alert, with optional auto-replay. |

### Glue Catalog

| Database | Wired to | Tables |
|---|---|---|
| `edl_curated` | **Nothing, in dev today.** `infrastructure/modules/transformation_lambda/main.tf`'s `glue_catalog_database` variable defaults to `""` (registration disabled) and `infrastructure/environments/dev/main.tf`'s `module.transformation_lambda` block never sets it. | Created by Terraform; permanently empty in dev until someone wires this variable. The curated-registration code path (`transformation/transformation_pipeline.py::_register_curated_catalog`, one table per `{tenant_code}_{entity_id}_{domain}_curated`) exists and is exercised by tests, but is currently dead code in the deployed dev Lambda. |
| `edl_analytics` | `module.analytics_publisher_lambda` (`glue_catalog_database = module.glue.analytics_database_name` in `infrastructure/environments/dev/main.tf`) | Created by Terraform; empty until a pipeline run completes. Tables are registered by `analytics_publisher/analytics_publisher_handler.py` (not `_register_curated_catalog`) after a full run reaches the analytics-publish stage. |

> Both Glue databases are provisioned in every environment (`infrastructure/modules/glue/main.tf`),
> but only `edl_analytics` is actually reachable by a running Lambda in dev right now — don't assume
> curated-layer tables will appear in Athena without first wiring `glue_catalog_database` on the
> transformation Lambda module block.

### Secrets Manager

| Secret | Contents |
|---|---|
| `edl/sources/salesforce/credentials` | `instance_url`, `client_id`, `client_secret` |
| `edl/sources/netsuite/credentials` | NetSuite OAuth/TBA credentials — connector is code-complete (`connector_runtime/adapters/netsuite/`); confirm this secret is actually populated before assuming live extraction |
| `edl/sources/mysql-rds/credentials` | `host`, `port`, `username`, `password`, `database` |
| `edl/sources/sage/intacct/credentials` | `token_url`, `client_id`, `client_secret`, `base_url`, `company_id` |
| `edl/sources/sage/x3/credentials` | `token_url`, `client_id`, `client_secret`, `base_url`, `folder` |

> All five secrets above are now Terraform-managed (`infrastructure/modules/secrets/main.tf`),
> including Sage's — each has a resource policy (`DenyAllOtherPrincipals`) restricting
> `GetSecretValue` to the extraction runtime role only. `terraform apply` creates the
> empty secret **shells**; the actual credential values still need to be populated by hand via
> `aws secretsmanager put-secret-value` — don't assume `terraform apply` alone makes a source live.
>
> **Credential rotation monitoring**: `EdlCredentialExpiryNotifier` (daily) checks the age
> of all five secrets above and alerts via SNS if rotation is overdue.

---

## Terraform State

| Item | Value |
|---|---|
| Backend | S3 remote state |
| State bucket | `edl-terraform-state-087972550871` |
| State key | `environments/dev/terraform.tfstate` |
| Lock table | `EdlTerraformStateLock` |

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
- `ARTIFACTS_BUCKET=edl-terraform-state-{staging_account_id} make lambda-deploy` once the staging
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

Same pattern as staging — use `edl-terraform-state-{prod_account_id}` as `ARTIFACTS_BUCKET`. Lambda
log retention is already configured at 365 days in HCL. `terraform apply`/`destroy` against
`infrastructure/environments/prod` requires explicit operator sign-off outside of automated tooling.

### Onboard NetSuite

`connector_runtime/adapters/netsuite/` has a full implementation (connector, OAuth/TBA auth
client, SuiteQL query planner, raw layer writer). Remaining steps are configuration-only:

- Populate `edl/sources/netsuite/credentials` with real OAuth/TBA credentials
- Seed entity config for NetSuite entities via `seed_entity_config.py`
- Set `schedule_enabled=True` once ready for live scheduled extraction
