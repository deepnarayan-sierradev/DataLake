# Platform Status — Enterprise Data Lake

**Last updated:** 2026-07-07
**Prepared by:** Platform Engineering

> **Multi-tenancy note:** `tenant_code` is now a first-class concept (default: `demo`, from
> `contracts/identifier_policy.DEFAULT_TENANT_CODE`). It is prefixed into S3 keys for the
> **curated layer, schema snapshots, and the config repository's S3 backend** — but **not yet**
> for the **raw layer**, which remains unprefixed today (tracked in
> `architecture/MULTI_TENANT_ROLLOUT_PLAN.md`). See the S3 Key Patterns table below for exact,
> per-layer paths — don't assume uniform tenant-prefixing across every bucket.

---

## Current Status: Dev ✅ | Staging 🔲 | Production 🔲

| Environment | Status | Notes |
|---|---|---|
| **Dev** | ✅ Live and operational | All 4 Lambda stages deployed and tested end-to-end |
| **Staging** | 🔲 Not started | Requires DynamoDB pre-creation + Terraform apply |
| **Production** | 🔲 Not started | Requires staging sign-off first |

---

## Live Data (Dev — as of 2026-07-02)

| Entity | Records | Latest analytics_date | Location |
|---|---|---|---|
| Companies (Salesforce Accounts) | 34 | `2026-07-02` | `dev_edl_analytics.company` |
| Persons (Salesforce Contacts) | 49 | `2026-06-29` | `dev_edl_analytics.person` |
| Contracts (MySQL RDS) | 35,971+ | `2026-07-02` | `dev_edl_analytics.contract` |

**Query in Athena (AWS Console → Athena → database: `dev_edl_analytics`, workgroup: `dev-edl-analytics`):**

```sql
-- Always filter by the latest analytics_date for current state
SELECT * FROM dev_edl_analytics.company    WHERE analytics_date='2026-07-02';
SELECT * FROM dev_edl_analytics.person     WHERE analytics_date='2026-06-29';
SELECT COUNT(*) FROM dev_edl_analytics.contract   WHERE analytics_date='2026-07-02';

-- For contracts: filter out soft-deleted records
SELECT COUNT(*) FROM dev_edl_analytics.contract
WHERE analytics_date='2026-07-02' AND is_deleted = false;
```

> **Note:** Use the fully-qualified table name (`dev_edl_analytics.company`) or select `dev_edl_analytics` as the database in the Athena console before running queries. The workgroup must be `dev-edl-analytics`.

---

## Connected Data Sources

| Source | Status | Entities | Extraction mode |
|---|---|---|---|
| **Salesforce CRM** | ✅ Connected | `salesforce-account` (companies), `salesforce-contact` (persons) | Incremental (watermark: `SystemModstamp`) |
| **MySQL RDS** | ✅ Connected | `mysql-rds-contracts` (contracts) | Incremental (watermark: `ModifiedOn`, tombstone soft-delete on `is_deleted`) |
| **Sage Intacct** | ✅ Connected | `sage-intacct-customer` (companies), `sage-intacct-vendor` (suppliers), `sage-intacct-arinvoice` (AR invoices), `sage-intacct-apbill` (AP bills) | Incremental |
| **Sage X3** | ✅ Connected | `sage-x3-customer` (companies), `sage-x3-supplier` (suppliers) | Incremental |
| **NetSuite ERP** | 🟡 Code-complete, not yet live | Connector, auth client, SuiteQL query planner, and raw layer writer are all implemented (`connector_runtime/adapters/netsuite/`) — not "no code changes required" as previously stated. Not yet confirmed whether entity config is seeded/schedule-enabled in dev; verify before assuming live traffic. | Incremental (SuiteQL) |

---

## AWS Resources — Dev Environment

**AWS Account:** `087972550871` | **Region:** `us-east-1`

### S3 Buckets

| Bucket | Pipeline stage | Written by | Read by | Purpose |
|---|---|---|---|---|
| `dev-edl-raw-layer` | Stage A — Extraction | Extraction Lambda | Transformation Lambda | Immutable raw Parquet files written once per extraction run. One Hive-partitioned prefix per entity per date. Never overwritten; watermark prevents re-extraction of unchanged data. |
| `dev-edl-curated-layer` | Stage B — Transformation | Transformation Lambda | Entity Resolution Lambda, Athena | Field-mapped, quality-checked Parquet (canonical column names). Also stores field-mapping JSON config files under `field-mappings/{source_id}/{entity_id}/`. |
| `dev-edl-analytics-layer` | Stages C & D — Entity Resolution + Analytics | Entity Resolution Lambda, Analytics Publisher Lambda | Athena, downstream BI tools | Golden (de-duplicated) records from entity resolution and consumption-optimised Parquet for analytics. Registered in Glue Catalog for Athena queries. |
| `dev-edl-schema-snapshots` | Stage A — Extraction (post-extract) | Extraction Lambda | Drift Evaluation (same Lambda) | JSON schema fingerprints captured after every extraction. The drift evaluator compares the new snapshot against `latest.json` to detect breaking changes (added/removed/type-changed columns). Path: `{tenant_code}/{source_id}/{entity_id}/{schema_version}/{extraction_date}.json` + `drift-report-{extraction_date}.json`; latest-pointer at `{tenant_code}/{source_id}/{entity_id}/latest.json`. |
| `dev-edl-s3-access-logs` | All stages (passive) | AWS S3 service (automatic) | Security & compliance audits | Receives S3 server access logs from every other data lake bucket. Never written to directly by pipeline code. Used for access auditing, cost attribution, and compliance. Retention: 30 days (dev). |
| `dev-edl-terraform-state` | Infrastructure (deploy time only) | Terraform CLI, `make lambda-upload` | Terraform CLI, Lambda service at deploy | Dual-purpose: (1) Terraform remote state file (`environments/dev/terraform.tfstate`) with DynamoDB lock for single-writer safety. (2) Lambda artifact store — `lambda/extraction-pipeline.zip` uploaded here by `make lambda-upload` and pulled by Lambda on every `terraform apply`. Not accessed at pipeline runtime. |

### S3 Key Patterns

| Layer | Pattern |
|---|---|
| Raw | `s3://dev-edl-raw-layer/raw/{source_id}/{entity_id}/extraction_date=YYYY-MM-DD/run_id={run_id}/data.parquet` — **not tenant-prefixed yet** |
| Curated | `s3://dev-edl-curated-layer/{tenant_code}/curated/{domain}/{entity_id}/curated_date=YYYY-MM-DD/run_id={run_id}/data.parquet` |
| Golden records | `s3://dev-edl-analytics-layer/canonical/{entity_type}/golden_date={date}/run_id={run_id}/golden.parquet` |
| Analytics | `s3://dev-edl-analytics-layer/analytics/{entity_type}/analytics_date=YYYY-MM-DD/data.parquet` |
| Entity config (S3 backend, alternate to DynamoDB) | `s3://<config-bucket>/{tenant_code}/{source_id}/{entity_id}/config.json` |

> `{tenant_code}` defaults to `demo` (`contracts/identifier_policy.DEFAULT_TENANT_CODE`) and is
> always present in prefixed paths above — there is no unprefixed legacy mode for those layers.
> The raw layer and golden/analytics layers are not yet tenant-prefixed; don't assume tenant
> isolation applies uniformly across every bucket.

> **Curated layer — SCD Type 1 merge:** For incremental entities with `primary_key_field` set, each curated partition holds the **full current state** of all records (not just the day's delta). This ensures entity resolution always sees complete data. Deleted records are retained as tombstones (`is_deleted=True`) rather than physically removed.

### DynamoDB Tables

> **Unresolved doc/infra discrepancy — verify before running `terraform apply` in any environment.**
> `infrastructure/modules/metadata_persistence/main.tf` defines all four tables below as real
> `aws_dynamodb_table` resources with `lifecycle { prevent_destroy = true }` — this is not new,
> it predates the current round of changes (confirmed via `git log` on that file). It directly
> contradicts this section's previous claim that the first three tables are "not Terraform-managed
> and must be created by hand." **Do not assume either narrative.** Before applying in any
> environment, run `terraform state list | grep dynamodb` to check whether these resources are
> already tracked in state — if they exist in AWS but aren't in state, `terraform apply` will
> fail with "already exists" rather than adopting them; if they were manually created under the
> old instructions and never imported, you'll need `terraform import` first.

| Table | Purpose | Hash key | Terraform resource |
|---|---|---|---|
| `dev-edl-entity-extraction-config` | Entity extraction configuration (source, watermark field, load type, tenant_code, etc.) | `source_id` + `entity_id` (range) | `aws_dynamodb_table.entity_extraction_config` |
| `dev-edl-watermark-repository` | Per-entity watermark timestamps for incremental loads, now tenant-checked on read | `source_id` + `entity_id` (range) | `aws_dynamodb_table.watermark_repository` |
| `dev-edl-run-audit-log` | Immutable audit record of every pipeline run (including partial/checkpointed runs) | `run_id` + `stage` (range) | `aws_dynamodb_table.run_audit_log` |
| `dev-edl-entity-type-registry` | **New.** Tenant-scoped entity-type/entity-id registry (`entity_resolution/entity_type_registry.py::EntityTypeRegistryClient`) — supersedes the old hardcoded dicts, which remain as fallback seed data | `tenant_code` + `sk` (range) | `aws_dynamodb_table.entity_type_registry` |

> Table names above use the `-edl-` infix to match the actual Terraform `name` attribute
> (`"${var.environment}-edl-<table>"`). If you see references elsewhere to `dev-entity-extraction-config`
> (no `-edl-` infix), treat that as the stale form and prefer the name shown here.

### Lambda Functions

| Function | Handler | Purpose |
|---|---|---|
| `dev-extraction-pipeline` | `connector_runtime.extraction_pipeline_handler.lambda_handler` | Extract from source → raw layer. Now supports mid-run checkpointing on approaching Lambda timeout (`LambdaTimeoutWarning`, partial watermark commit) — see Known Gotchas in `docs/DEVELOPER_GUIDE.md`. |
| `dev-transformation-pipeline` | `transformation.transformation_pipeline_handler.lambda_handler` | Raw → curated layer (tenant-prefixed S3 keys) |
| `dev-entity-resolution-pipeline` | `entity_resolution.entity_resolution_pipeline_handler.lambda_handler` | Curated → golden records; now streams curated records via DuckDB rather than fully materializing them, and resolves entity types via `EntityTypeRegistryClient` (DynamoDB) with fallback to hardcoded seed dicts |
| `dev-analytics-layer-publisher` | `analytics_publisher.analytics_publisher_handler.lambda_handler` | Golden records → analytics layer; emits an end-to-end pipeline SLA metric. (Not `dev-analytics-publisher` — that shorter form appears in some older references but doesn't match the actual Terraform `function_name`, `infrastructure/modules/analytics_publisher_lambda/main.tf`.) |
| `dev-edl-control-plane` | `connector_runtime.api.control_plane_handler.lambda_handler` | **New.** SaaS control-plane REST API behind a Cognito/JWT authorizer — tenant provisioning, entity registration/listing, pipeline trigger, run status. Code-complete but **not yet verified against a live AWS deployment** — see `connector_runtime/CLAUDE.md`. |
| `dev-edl-credential-expiry-notifier` | `connector_runtime.credential_rotation.credential_expiry_notifier_handler.lambda_handler` | **New.** Daily check of all 5 source-credential secrets' age; publishes an SNS alert if rotation is overdue. |
| `dev-edl-pipeline-trigger` | `orchestration.pipeline_trigger.pipeline_trigger_handler.lambda_handler` | **New.** Rate-limited SQS FIFO consumer that starts Step Functions executions — the single path both `scripts/trigger_extraction.py` and the control-plane API's pipeline-trigger route funnel through. |
| `dev-edl-dlq-processor` | `orchestration.dlq_processor.dlq_processor_handler.lambda_handler` | **New.** Processes the extraction-failure DLQ: writes an audit record, sends an SNS alert, and optionally auto-replays (`AUTO_REPLAY=false` by default). |

All eight Lambdas are deployed from the **same zip** (via `var.lambda_package_s3_bucket` /
`lambda_package_s3_key`, shared across every module): `s3://dev-edl-terraform-state/lambda/extraction-pipeline.zip`

### Step Functions

| State Machine | Purpose |
|---|---|
| `dev-extraction-pipeline` | Full end-to-end pipeline: extraction → transformation → entity resolution → analytics → serving store (optional). Single state machine for all four stages. Now includes an `ExtractionCheckpointed` terminal `Succeed` state, reached via a `Catch` on `LambdaTimeoutWarning` — the extraction stage commits a partial watermark and exits cleanly rather than failing when it detects it's about to hit the Lambda timeout. **Automatic re-invocation from the checkpoint is not yet implemented** — a checkpointed run currently needs a manual re-trigger; see `architecture/GAP_ANALYSIS_FINDINGS.md`'s `PERF-5` entry. |

### EventBridge Scheduler

| Schedule | Target | Purpose |
|---|---|---|
| `dev-edl-credential-expiry-check` | `dev-edl-credential-expiry-notifier` | **New.** Daily (`rate(1 day)`) check of source-credential secret age across all 5 sources. |

### Dead-Letter Queue

| Resource | Purpose |
|---|---|
| `extraction_failure_dlq` (SQS, see `infrastructure/modules/metadata_persistence/main.tf`) | Failed extraction runs land here; `dev-edl-dlq-processor` drains it into an audit record + SNS alert, with optional auto-replay. |

### Glue Catalog

| Database | Tables |
|---|---|
| `dev_edl_analytics` | `company`, `person`, `contract`, `supplier`, `ar_invoice`, `ap_bill` |

### Secrets Manager

| Secret | Contents |
|---|---|
| `dev/sources/salesforce/credentials` | `instance_url`, `client_id`, `client_secret` |
| `dev/sources/netsuite/credentials` | NetSuite OAuth/TBA credentials — connector is code-complete (`connector_runtime/adapters/netsuite/`); confirm this secret is actually populated before assuming live extraction |
| `dev/sources/mysql-rds/credentials` | `host`, `port`, `username`, `password`, `database` |
| `dev/sources/sage/intacct/credentials` | `token_url`, `client_id`, `client_secret`, `base_url`, `company_id` |
| `dev/sources/sage/x3/credentials` | `token_url`, `client_id`, `client_secret`, `base_url`, `folder` |

> All five secrets above are now Terraform-managed (`infrastructure/modules/secrets/main.tf`),
> including Sage's — each has a resource policy (`DenyAllOtherPrincipals`) restricting
> `GetSecretValue` to the extraction runtime role only (`SEC-3`). `terraform apply` creates the
> empty secret **shells**; the actual credential values still need to be populated by hand via
> `aws secretsmanager put-secret-value` — don't assume `terraform apply` alone makes a source live.
>
> **Credential rotation monitoring**: `dev-edl-credential-expiry-notifier` (daily) checks the age
> of all five secrets above and alerts via SNS if rotation is overdue.

---

## Terraform State

| Item | Value |
|---|---|
| Backend | S3 remote state |
| State bucket | `dev-edl-terraform-state` |
| State key | `environments/dev/terraform.tfstate` |
| Lock table | `dev-edl-terraform-state-lock` |

---

## Next Steps

### Activate Sage Intacct and Sage X3 Schedules

Entity configs for all 6 Sage entities are already seeded. To enable live extraction:

- **Populate** (not "create" — `terraform apply` already creates the secret shells, see Secrets
  Manager section above) the values for `dev/sources/sage/intacct/credentials` and
  `dev/sources/sage/x3/credentials` via `aws secretsmanager put-secret-value`
- Set `schedule_enabled=True` for Sage entities in DynamoDB via `seed_entity_config.py`
- Trigger a dry-run: `python scripts/run_sage_connector_local.py --entity-id sage-intacct-customer --dry-run`

### Deploy Staging Environment

**Known blocker as of this writing:** `terraform validate` for `staging` currently fails with 7
pre-existing errors on the `orchestration` module block (missing `lambda_package_s3_key`,
`lambda_package_s3_bucket`, `lambda_package_source_hash`, `run_audit_log_table_name`,
`extraction_failure_dlq_arn`, `pipeline_trigger_role_arn`, `dlq_processor_role_arn` — confirmed via
`git diff`, not introduced by any recent change). **Fix these missing module arguments before
attempting any of the steps below** — they will fail at `terraform apply`, not just `validate`.

Pre-requisites:
- Staging AWS credentials configured
- Confirm whether `staging-edl-watermark-repository`, `staging-edl-run-audit-log`,
  `staging-edl-entity-extraction-config`, and `staging-edl-entity-type-registry` already exist in
  AWS and, if so, whether they're tracked in Terraform state (`terraform state list | grep dynamodb`)
  — see the DynamoDB Tables caveat above. Do not assume they need manual creation; that guidance is
  now unverified.
- Upload Lambda zip: `ARTIFACTS_BUCKET=staging-edl-terraform-state make lambda-upload`

The module apply order below is the same one used for `dev`, but is no longer exhaustive — it
predates `module.entity_resolution_lambda`, `module.analytics_publisher_lambda`, and
`module.control_plane` (which additionally depends on `module.metadata_persistence`). Apply
without `-target` once `terraform validate` is clean, or extend the targeted sequence to cover
every module in `infrastructure/CLAUDE.md`'s module list:

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

Same pattern as staging — use `prod-edl-terraform-state` as `ARTIFACTS_BUCKET`. Lambda log
retention is already configured at 365 days in HCL. `prod` has the same 7 pre-existing
`orchestration` module validation errors as `staging` — fix once, the fix applies to both.

### Onboard NetSuite

The connector is **code-complete**, not "no code changes required — configuration only" as
previously stated: `connector_runtime/adapters/netsuite/` already has a full implementation
(connector, OAuth/TBA auth client, SuiteQL query planner, raw layer writer). Remaining steps are
genuinely configuration-only:

- Confirm `dev/sources/netsuite/credentials` (already Terraform-managed as a secret shell) is
  populated with real OAuth/TBA credentials
- Seed entity config for NetSuite entities via `seed_entity_config.py`
- Set `schedule_enabled=True` once ready for live scheduled extraction
