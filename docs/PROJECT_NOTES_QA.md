# Project Notes — Q&A Deep Dive

Consolidated notes from a walkthrough of the Enterprise Data Lake Platform: schedules, S3
buckets, credentials, Glue's role, Lambda names, how data merges across layers, the full source
table inventory, and the exact rules applied at each layer. See also
[`docs/DEVELOPER_GUIDE.md`](DEVELOPER_GUIDE.md) — §4 "First-Time Setup" for local environment
setup, §2 "Codebase Module Map" for a pipeline overview.

---

## 1. Pipeline run schedule

> **AWS services to show live:** **EventBridge Scheduler** (Console → Amazon EventBridge →
> Scheduler → Schedules, group `EdlExtractionSchedules`) and **DynamoDB**
> (`EdlEntityExtractionConfig` table — holds `schedule_cron`/`schedule_enabled` per entity).
> CLI: `aws scheduler list-schedules --group-name EdlExtractionSchedules --region us-east-1`.

All pipelines run **once daily, staggered a few minutes apart between 02:00–03:05 UTC**
(likely to avoid overloading shared downstream resources when multiple pipelines fire).

| Source / Entity | Cron (UTC) | Enabled? |
|---|---|---|
| salesforce-account | `cron(0 2 * * ? *)` → 02:00 | Yes |
| salesforce-contact | `cron(15 2 * * ? *)` → 02:15 | Yes |
| salesforce-opportunity | `cron(20 2 * * ? *)` → 02:20 | Yes |
| salesforce-contract | `cron(25 2 * * ? *)` → 02:25 | Yes |
| mysql-rds-contracts | `cron(30 2 * * ? *)` → 02:30 | Yes |
| mysql-rds-contractterms | `cron(35 2 * * ? *)` → 02:35 | Yes |
| sage-intacct-customer | `cron(45 2 * * ? *)` → 02:45 | Yes |
| sage-intacct-vendor | `cron(50 2 * * ? *)` → 02:50 | Yes |
| sage-intacct-arinvoice | `cron(55 2 * * ? *)` → 02:55 | Yes |
| sage-x3-customer | `cron(55 2 * * ? *)` → 02:55 | Yes |
| sage-intacct-apbill | `cron(5 3 * * ? *)` → 03:05 | Yes |
| sage-x3-supplier | `cron(0 3 * * ? *)` → 03:00 | No (disabled) |
| netsuite-customer | none | No (disabled) |

Three new entities (`salesforce-opportunity`, `salesforce-contract`, `mysql-rds-contractterms`)
were added to `scripts/seed_entity_config.py` since this section was last checked — all three are
seeded enabled and scheduled. `sage-intacct-arinvoice` and `sage-x3-customer` still share the same
02:55 cron slot (pre-existing, not newly introduced) — that's a real coincidence in the seed data,
not a bug, since each entity's Step Functions execution is independent.

**Where it lives in code** — schedules are **data in DynamoDB, not hardcoded in Terraform**:
- Source of truth: `scripts/seed_entity_config.py` — a Python dict per entity with
  `schedule_cron`, `schedule_enabled`, `schedule_timezone` fields. Running it writes these
  into the `EdlEntityExtractionConfig` DynamoDB table (same table name in every environment —
  each environment now lives in its own AWS account, so no env prefix is needed).
- Sync to AWS: `scripts/seed_schedules.py` reads that DynamoDB table and calls
  `ExtractionScheduleClient.create_or_update_schedule(...)` to create/update the actual
  EventBridge Scheduler entry (named `{source_id}--{entity_id}`). Disabled entities have
  their EventBridge schedule deleted.
- Client wrapper: `orchestration/event_bridge/extraction_schedule_client.py` — generic,
  takes cron/timezone as parameters, defines no values itself.
- Terraform (`infrastructure/modules/orchestration/main.tf`) only creates the **empty**
  `aws_scheduler_schedule_group` container — no cron/rate expressions exist in Terraform at all.

**To change a schedule**: edit `schedule_cron`/`schedule_enabled` in `scripts/seed_entity_config.py`
→ `make seed-entity-config` → `make seed-schedules`.

---

## 2. S3 buckets — 6 total

> **AWS services to show live:** **S3** (Console → S3 → search `edl-`) and **KMS**
> (Console → Key Management Service → look up the CMK each bucket's default encryption
> points to). CLI: `aws s3 ls | grep edl-`.

| # | Bucket (dev account: `087972550871`) | Purpose |
|---|---|---|
| 1 | `edl-raw-087972550871` | Immutable raw extraction output (Hive-partitioned Parquet). Object Lock enabled (GOVERNANCE mode, 30-day retention); only the extraction Lambda role can write/delete. |
| 2 | `edl-curated-087972550871` | Field-mapped, quality-checked Parquet with canonical column names; also stores field-mapping JSON config. |
| 3 | `edl-analytics-087972550871` | Golden (deduplicated) records + BI-ready Parquet, registered in Glue for Athena. Also doubles as the Athena query-results bucket. |
| 4 | `edl-schema-snapshots-087972550871` | JSON schema fingerprints per extraction run, used for schema drift detection. |
| 5 | `edl-access-logs-087972550871` | Central S3 access-log target for the other 4 data buckets. Populated automatically by AWS. |
| 6 | `edl-terraform-state-087972550871` | Dual-purpose: Terraform remote state (+ DynamoDB lock table) and Lambda deployment artifact storage. Bootstrapped manually, outside the Terraform `storage` module. |

**Defined in**: `infrastructure/modules/storage/main.tf` (buckets 1–5, each with KMS encryption,
versioning, public-access-block, TLS-enforcement policy). Bucket 6 is set up separately,
referenced in `infrastructure/environments/dev/backend.tf` / `terraform.tfvars`.

**Not provisioned**: an optional `GOVERNANCE_S3_BUCKET` (lineage) is wired into code/Terraform
variables but defaults to `""` and isn't actually created in dev — doesn't count as a live bucket.

---

## 3. Credential storage (Salesforce & MySQL)

> **AWS services to show live:** **Secrets Manager** (Console → Secrets Manager → filter
> `edl/sources`) and **IAM** (Console → IAM → Roles → extraction Lambda role → Permissions,
> to show the scoped `secretsmanager:GetSecretValue` policy). CLI:
> `aws secretsmanager list-secrets --region us-east-1 --query 'SecretList[].Name'`.

Stored **exclusively in AWS Secrets Manager — never in code, env vars, or config files.**

Pattern: `edl/sources/{source_id}/credentials` — the env segment was dropped since each
environment now lives in its own AWS account, so the same path is correct everywhere:

| Source | Secret path | Fields inside |
|---|---|---|
| Salesforce | `edl/sources/salesforce/credentials` | `instance_url`, `client_id`, `client_secret` (OAuth2 client-credentials flow) |
| MySQL RDS | `edl/sources/mysql-rds/credentials` | `host`, `port`, `username`, `password`, `database` |

`connector_runtime/adapters/salesforce/salesforce_auth_client.py` states this explicitly as a
security control (OWASP A07/A09): "Client credentials retrieved from Secrets Manager only —
never from env vars, constructor arguments, or config files." The token is cached in memory
only, never logged/persisted.

**Access control**: `infrastructure/modules/iam/main.tf` grants the extraction Lambda role
`secretsmanager:GetSecretValue` on the wildcard `arn:aws:secretsmanager:...:secret:edl/sources/*`.

**Not seeded from code** — unlike entity config/schedules, there is no seeding script for
secrets. The only related tooling is a read-only check:
```bash
aws secretsmanager list-secrets --region us-east-1 --query 'SecretList[].Name' | grep edl/sources
```
Secrets are created manually/out-of-band by whoever has AWS console/CLI access.

**Known gaps found while tracing this — both RESOLVED**:
- ~~`mysql_rds_credentials_client.py` does not exist in the repo~~ — **RESOLVED**: it now
  exists at `connector_runtime/adapters/mysql_rds/mysql_rds_credentials_client.py`, a real
  credentials client used by the MySQL connector, local runner script, and tests.
- ~~`infrastructure/modules/secrets/` does not exist~~ — **RESOLVED**: `infrastructure/modules/secrets/main.tf`
  now exists and is a substantial, real Terraform module — it defines Secrets Manager secrets
  for all 5 sources plus the `credential_expiry_notifier` Lambda (see §6 and §11 for more on
  that Lambda).

---

## 4. Role of AWS Glue

> **AWS services to show live:** **AWS Glue Data Catalog** (Console → Glue → Data Catalog →
> Databases → `edl_analytics` → Tables — show `company`, `person`, `contract`, `supplier`,
> `ar_invoice`, `ap_bill`). CLI: `aws glue get-tables --database-name edl_analytics`.

**Glue is a pure metadata catalog here — not an ETL/Spark engine.** No `aws_glue_job`,
`aws_glue_crawler`, or PySpark exists anywhere in the repo. All actual transformation happens
in Python/pandas/pyarrow inside Lambda functions. Glue's only job: register table/partition
metadata in the Glue Data Catalog so Athena can query the S3 Parquet files.

**How tables/partitions get created** (`governance/data_catalog_registration.py`):
- `get_database` → `create_database` if missing.
- `create_table` optimistically; falls back to `update_table` on `AlreadyExistsException`
  (TOCTOU-safe for concurrent Lambda runs).
- Table schema built from the PyArrow schema (Parquet/Snappy SerDe).
- Partitions created explicitly via `glue_client.create_partition`/`update_partition` for
  each day's partition — **no crawler, no `MSCK REPAIR TABLE`.**

**New since last check**: `infrastructure/modules/glue/main.tf` now provisions a *second* Glue
database, `edl_curated` (plus matching Lake Formation `SELECT`/`DESCRIBE` grants), alongside the
existing `edl_analytics`. This backs a curated-layer catalog-registration code path that's
code-complete in `transformation/transformation_pipeline.py` (`_register_curated_catalog`, gated
on `ctx.glue_catalog_database` / the `GLUE_CATALOG_DATABASE` env var). **It is not active in dev
today**: `infrastructure/environments/dev/main.tf`'s `transformation_lambda` module block never
sets `glue_catalog_database` (no default in `infrastructure/modules/transformation_lambda/variables.tf`
means it defaults to `""`), so the env var is empty and the transformation Lambda skips catalog
registration — curated Parquet is written to S3 but has no Glue table today, so it's not yet
Athena-queryable. Wiring `glue_catalog_database = module.glue.curated_database_name` into that
module block would turn it on.

---

## 5. Curated → Golden → Analytics data flow

> **AWS services to show live:** **Lambda** (Console → Lambda → `EdlEntityResolutionPipeline`
> and `EdlAnalyticsLayerPublisher` — show recent invocations), **S3** (bucket
> `edl-analytics-087972550871`, prefixes `canonical/{entity_type}/` then `analytics/{entity_type}/`),
> and **Glue** (table registered only after step 4 below). CLI:
> `aws s3 ls s3://edl-analytics-087972550871/canonical/ --recursive`.

1. **Entity Resolution reads curated data** — `entity_resolution/entity_resolution_pipeline_handler.py`
   loads curated Parquet from S3 for all sources feeding a given entity type.
2. **Matching/clustering** — `entity_resolution/matching_engine/match_rule_engine.py`
   `MatchRuleEngine.cluster()` groups records likely representing the same real-world entity,
   using Jaro-Winkler (fuzzy string match) and Jaccard/token-set similarity.
3. **Survivorship → golden record** — `entity_resolution/canonical_record_publisher/canonical_record_publisher.py`
   `GoldenRecordPublisher.publish()` merges each matched cluster into one canonical record
   (tagged `golden_id`, `contributing_source_records`, `field_provenance`), writes:
   `s3://{analytics_bucket}/canonical/{entity_type}/golden_date=.../golden.parquet`
   plus a `decisions.json` audit trail. **No Glue table is registered for this layer** (see §7).
4. **Analytics Publisher** — Lambda `EdlAnalyticsLayerPublisher`
   (`analytics_publisher/analytics_publisher_handler.py`, handler
   `analytics_publisher.analytics_publisher_handler.lambda_handler`) reads the golden Parquet,
   strips internal bookkeeping fields, re-serializes, writes:
   `s3://{analytics_bucket}/analytics/{entity_type}/analytics_date=.../data.parquet`
   then registers/updates the Glue table + day's partition.
5. **Athena queries it** — once registered, Athena can `SELECT` directly against the S3 Parquet.

**Note**: an older/parallel implementation, `transformation/analytics_layer_publisher.py`
(`AnalyticsLayerPublisher`), duplicates similar logic with a slightly different partition path
and is still the one named in `docs/PIPELINE_FLOW.md` — but it is **not** the Lambda actually
wired into the Step Functions state machine. That's a doc/code drift, not a second live path.

---

## 6. Lambda function names (AWS Console — dev environment)

> **AWS services to show live:** **Lambda** (Console → Lambda → Functions → filter `Edl`) and
> **Step Functions** (Console → Step Functions → State machines → `EdlExtractionPipeline` →
> Definition tab, to show how these four Lambda ARNs are wired into one workflow). CLI:
> `aws lambda list-functions --region us-east-1 --query 'Functions[?starts_with(FunctionName, `Edl`)].FunctionName'`.

| Pipeline stage | Console name | Handler |
|---|---|---|
| Extraction | `EdlExtractionPipeline` | — |
| Transformation (raw→curated) | `EdlTransformationPipeline` | `transformation.transformation_pipeline_handler.lambda_handler` |
| Entity Resolution (curated→golden) | `EdlEntityResolutionPipeline` | — |
| Analytics Publisher (golden→analytics) | `EdlAnalyticsLayerPublisher` | `analytics_publisher.analytics_publisher_handler.lambda_handler` |

Confirmed via Terraform locals in each module's `main.tf` (`function_name = "Edl..."`, a fixed
PascalCase literal — no longer interpolated with `var.environment`; the environment is tracked
only via the `Environment` tag now) and by tracing the Step Functions orchestration module's
`analytics_publisher_lambda_arn` wiring back to `module.analytics_publisher_lambda`.

**Historical doc mismatch (resolved)**: `docs/PLATFORM_STATUS.md` used to list the analytics
publisher as the shortened `dev-analytics-publisher`, while Terraform used the longer
`dev-analytics-layer-publisher` — a real inconsistency under the old naming scheme. The new
convention resolves this: there is exactly one correct name, **`EdlAnalyticsLayerPublisher`**,
and it no longer varies by environment (only the AWS account does).

**Four newer Lambdas exist outside the core 4-stage pipeline above** — not wired into the
`EdlExtractionPipeline` state machine, so they're easy to miss if you only look there:

| Console name | Handler | Purpose |
|---|---|---|
| `EdlControlPlane` | `connector_runtime.api.control_plane_handler.lambda_handler` | SaaS control-plane REST API (Cognito/JWT auth) for self-service tenant provisioning — code-complete, not yet deployment-verified. See new §6.1 below. |
| `EdlCredentialExpiryNotifier` | `connector_runtime.credential_rotation.credential_expiry_notifier_handler.lambda_handler` | Daily secret-age check, sends an SNS alert on stale credentials (see §3 and §11's rotation correction). |
| `EdlPipelineTrigger` | `orchestration.pipeline_trigger.pipeline_trigger_handler.lambda_handler` | Rate-limited SQS FIFO → Step Functions trigger. |
| `EdlDlqProcessor` | `orchestration.dlq_processor.dlq_processor_handler.lambda_handler` | DLQ → audit log + SNS alert + optional replay. |

Names confirmed via Terraform locals: `infrastructure/modules/control_plane/main.tf`
(`control_plane_lambda_name`), `infrastructure/modules/secrets/main.tf` (`credential_expiry_notifier`
resource), `infrastructure/modules/orchestration/main.tf` (`pipeline_trigger_lambda_name`,
`dlq_processor_lambda_name`).

### 6.1 Control plane / multi-tenancy (new, code-complete but unverified in a live deployment)

`tenant_code` is now threaded through the config, watermark, and schema-snapshot repositories
(e.g. `connector_runtime/configuration_repository/configuration_repository.py`,
`watermark_management/watermark_repository/watermark_repository.py`,
`schema_management/snapshot_repository/snapshot_repository.py`), plus a new
`EdlEntityTypeRegistry` DynamoDB table that — unlike the other config tables in §13.3 — *is*
Terraform-managed (`infrastructure/modules/metadata_persistence/`). A Cognito-authenticated SaaS
control-plane API (`EdlControlPlane` above) now exists for self-service tenant
provisioning. Both are code-complete but **not yet verified against a live AWS deployment** —
treat as "built, not yet demoed." Canonical references: `tests/test_tenant_isolation.py` and the
cross-tenant-incident scenario in `docs/PRODUCTION_INCIDENT_RUNBOOK.md`.

---

## 7. Combining multiple sources — which layer does it, and why query only Analytics

> **AWS services to show live:** **Athena** (Console → Athena → Query editor → database
> `edl_analytics`, workgroup `EdlAnalytics` — run a `SELECT *` on `company` to show
> the merged row live), **Glue** (Data Catalog entry backing that table), and **IAM** (Role →
> `entity-resolution-role` has no Glue permissions, which is *why* `canonical/` isn't
> queryable — good to show the policy JSON directly to prove the point).

### Which entity types combine multiple sources

Mapping lives in `entity_resolution/entity_type_registry.py` (`ENTITY_TYPE_SOURCES`):

| Entity type | Sources merged |
|---|---|
| **company** | Salesforce Account + NetSuite Customer + Sage Intacct Customer + Sage X3 Customer |
| **supplier** | Sage Intacct Vendor + Sage X3 Supplier |
| person | Salesforce Contact only |
| contract | MySQL RDS Contracts only |
| ar_invoice | Sage Intacct AR Invoice only |
| ap_bill | Sage Intacct AP Bill only |
| opportunity | Salesforce Opportunity only |
| sales-contract | Salesforce Contract only (kept as its own entity type rather than merged into `contract` — Salesforce Contract and MySQL RDS Contracts share no common key, so fuzzy-matching them risks combining unrelated records; see `entity_resolution/entity_type_registry.py`) |
| contract-term | MySQL RDS ContractTerms only |

### Where the merge happens, by layer

| Layer | Multi-source handling |
|---|---|
| **Raw** | Kept separate per source: `raw/{source_id}/{entity_id}/...` |
| **Curated** | Still separate per source — cleaned/mapped, but not yet merged |
| **Golden (canonical)** | **Merge happens here.** Entity Resolution clusters + applies survivorship across all sources feeding an entity type, writes one `golden.parquet` per entity type |
| **Analytics** | Inherits the already-merged golden data; just cleans/republishes it — no additional merge logic |

### Why you can't query the Golden layer directly

Two reasons, confirmed in code:

1. **Hard blocker — no Glue table exists for `canonical/`.** `GoldenRecordPublisher.publish()`
   only writes Parquet + an audit log to S3 and emits a lineage record — it never calls
   `DataCatalogRegistrationClient`/Glue. The entity-resolution Lambda's IAM role has **no Glue
   permissions at all** (confirmed in `infrastructure/modules/iam/main.tf`), so it couldn't
   register a table even if the code tried. Athena can only query tables registered in the Glue
   Catalog — an un-registered S3 folder is invisible to it.
2. **Intentional — golden records still carry internal bookkeeping fields** not meant for
   analysts: `contributing_source_records`, `field_provenance`, `match_run_id`,
   `survivorship_version` (`_INTERNAL_FIELDS_TO_DROP` in `analytics_publisher_handler.py`).
   These are stripped only on the way to the Analytics layer.

**Doc mismatch**: `docs/PIPELINE_FLOW.md` and `docs/GLOSSARY_AND_TERMINOLOGY.md` claim the
`canonical/` layer is "Glue-catalogued, Athena-ready" and give an example of querying
`canonical.company` directly — this is **not true of the current code**. Only `analytics/` is
Glue-catalogued and queryable today.

### Layman's explanation

- **Raw layer** = messy filing cabinets, one per system, exactly as each system gave it to you.
- **Curated layer** = same filing cabinets, but cleaned up and labeled consistently.
- **Golden layer** = someone went through all the cabinets, figured out which papers describe
  the same company, and stapled them into one master file per company — but this master file
  drawer isn't listed in the library's search catalog yet, and it still has sticky-note
  metadata attached (which drawer each page came from, etc.).
- **Analytics layer** = that master file, photocopied (sticky notes removed) and placed on the
  public shelf that's listed in the library catalog — this is what analysts/BI tools actually
  query.

**Example**: "Acme Corp" exists in Salesforce (CA address, sales rep), Sage Intacct (TX billing
address, Net-30 terms), and Sage X3 (tax ID, credit limit). Instead of an analyst manually
figuring out these are the same company and combining the fields, the `company` table in
Analytics already has one row with everything merged — best available value picked per field
from whichever source has it — ready to query via Athena with no joins or dedup needed.

---

## 8. All source tables/entities being extracted

> **AWS services to show live:** **DynamoDB** (Console → DynamoDB → Tables →
> `EdlEntityExtractionConfig` → Explore table items — every row in the table below is one
> item here). CLI: `aws dynamodb scan --table-name EdlEntityExtractionConfig --region us-east-1`.

| Source | Entity ID | Actual table/object queried | Load type | Active? | Scheduled? |
|---|---|---|---|---|---|
| Salesforce | salesforce-account | `Account` (SOQL object) | Incremental (`SystemModstamp`) | Yes | Yes |
| Salesforce | salesforce-contact | `Contact` (SOQL object) | Incremental (`SystemModstamp`) | Yes | Yes |
| Salesforce | salesforce-opportunity | `Opportunity` (SOQL object) | Incremental (`SystemModstamp`) | Yes | Yes |
| Salesforce | salesforce-contract | `Contract` (SOQL object) | Incremental (`SystemModstamp`) | Yes | Yes |
| MySQL RDS | mysql-rds-contracts | `Contracts` table | Incremental (`ModifiedOn`) | Yes | Yes |
| MySQL RDS | mysql-rds-contractterms | `ContractTerms` table | Incremental (`ModifiedOn`) | Yes | Yes |
| Sage Intacct | sage-intacct-customer | `accounts-receivable/customer` | Incremental | Yes | Yes |
| Sage Intacct | sage-intacct-vendor | `accounts-payable/vendor` | Incremental | Yes | Yes |
| Sage Intacct | sage-intacct-arinvoice | `accounts-receivable/invoice` | Incremental | Yes | Yes |
| Sage Intacct | sage-intacct-apbill | `accounts-payable/bill` | Incremental | Yes | Yes |
| Sage X3 | sage-x3-customer | `BPCUSTOMER` | Incremental | Yes | Yes |
| Sage X3 | sage-x3-supplier | `BPSUPPLIER` | Incremental | Yes (config) | **No** (schedule disabled) |
| NetSuite | netsuite-customer | `customer` (SuiteQL) | Incremental | **No** (disabled) | No |

Source: `scripts/seed_entity_config.py`, cross-checked against each adapter's query builder
(`connector_runtime/query_builders/`, `connector_runtime/adapters/{salesforce,mysql_rds,sage,netsuite}/`).

**Correction**: `salesforce-account` and `mysql-rds-contracts` were previously listed here as
`Full` load — that was stale. Both are configured `"load_type": "incremental"` in
`scripts/seed_entity_config.py` today (watermarks `SystemModstamp` and `ModifiedOn` respectively).

**Known issues found in this config**:
- **NetSuite is broken as configured** — seeded with empty `connector_params: {}`, but
  `connector_runtime/adapters/netsuite/netsuite_connector.py` requires a `record_type` key and
  raises `ValueError` without it. Currently masked because the entity is `active: False`.
- **Sage X3 Supplier is active but unscheduled** — `active: True` but `schedule_enabled: False`,
  so it only runs if someone manually triggers it; it never fires on cron.
- **mysql-rds-contractterms's watermark/primary-key columns are assumed, not confirmed** — the
  seed script's own comment flags that it reuses the `mysql-rds-contracts` convention
  (`ModifiedOn`/`Id`) without having verified `ContractTerms` actually uses those column names;
  adjust if the real table differs.

---

## 9. Rules applied at each layer, with real examples

> **AWS services to show live:** **S3** (schema snapshots in `edl-schema-snapshots-087972550871`,
> quality reports in `edl-curated-087972550871/quality-reports/...`), **Lambda**
> (`EdlTransformationPipeline` logs), and **CloudWatch** (Console → CloudWatch → Alarms —
> where a BLOCKING quality violation or BREAKING drift would fire). CLI:
> `aws s3 cp s3://edl-schema-snapshots-087972550871/salesforce/salesforce-account/latest.json -`.

### 9.1 Raw layer — structural validation only, no data-value rules
Fixed sequence per run (`orchestration/step_functions/extraction_workflow.py`): load config →
get credentials → discover metadata → build query → extract → **capture schema snapshot** →
**evaluate schema drift** → write raw Parquet → update watermark.

Schema drift classification (`schema_management/drift_evaluation/drift_evaluator.py`):

| Change | Classification | Effect |
|---|---|---|
| Field removed / type changed / nullable→non-nullable | BREAKING | Downstream transformation blocked for this run |
| Precision/scale/length changed | POTENTIALLY_BREAKING | Flagged, not blocked |
| New nullable field added / non-nullable→nullable | NON_BREAKING | No effect |
| No change / first run | NO_DRIFT | No effect |

No validation of actual data *values* happens at this layer — only whether the shape of the
data matches the previous run.

### 9.2 Curated layer — field mapping + data quality

**Field mapping** (declarative JSON per entity, e.g.
`config/field_mappings/salesforce/salesforce-account/v1.json`):
```json
{ "source_fields": ["Name"], "canonical_field": "full_name", "transformation": "rename" }
{ "source_fields": ["AnnualRevenue"], "canonical_field": "annual_revenue",
  "transformation": "cast", "transformation_params": {"type": "decimal"} }
{ "source_fields": ["CreatedDate"], "canonical_field": "created_date",
  "transformation": "date_format" }
```
For `salesforce-contact`, first/last name are **concatenated**:
`{"source_fields": ["FirstName","LastName"], "canonical_field": "full_name", "transformation": "concat", "transformation_params": {"separator": " "}}`

Five transformation kinds exist: `rename`, `concat`, `date_format`, `cast`, `mask`. Each mapped
field also declares a `missing_field_behavior`: `raise_error` (discard the whole record),
`use_default` (substitute a value), or `drop_field` (omit the canonical key).

**Data quality checks** — the engine (`transformation/quality_evaluation/quality_policy_evaluator.py`)
supports `null_check`, `range_check`, `pattern_check`, `allowed_values`, each markable `warning`
or `blocking`. **Gap found**: no production `QualityPolicy` is actually wired in for any entity
today — it's an optional parameter that's currently `None` in the live pipeline, so quality
evaluation is skipped entirely. Example rules like "email must match `^[\w.]+@[\w.]+\.\w+$`"
only exist in unit tests, not in real config — worth fixing if data quality enforcement matters.

### 9.3 Golden layer — match rules + survivorship

**Match rules** (who counts as "the same entity") —
`config/entity_resolution/company/match_rules_v1.json`:
```json
{ "rule_id": "email-exact", "strategy": "deterministic",
  "fields": [{ "field_name": "email_address" }] },
{ "rule_id": "name-country-fuzzy", "strategy": "probabilistic", "match_threshold": 0.85,
  "fields": [
    { "field_name": "full_name", "weight": 0.70, "similarity_kind": "jaro_winkler" },
    { "field_name": "billing_country", "weight": 0.30, "similarity_kind": "exact" }
  ] }
```
In plain terms: two company records are considered the same entity if their **emails match
exactly**, OR if their **names are fuzzy-similar (weighted 70%) and countries match exactly
(weighted 30%)**, combining to a score ≥ 0.85. `person` and `supplier` have their own rule sets
with different blocking keys and thresholds (0.88 for person, 0.80 for supplier).

**Survivorship rules** (when 2+ sources disagree on a field, who wins) —
`config/entity_resolution/company/survivorship_v1.json`:
```json
{ "canonical_field": "full_name", "strategy": "source_priority",
  "source_priority": ["netsuite","sage","salesforce"] },
{ "canonical_field": "annual_revenue", "strategy": "source_priority",
  "source_priority": ["salesforce","netsuite","sage"] },
{ "canonical_field": "credit_limit", "strategy": "source_priority",
  "source_priority": ["sage","netsuite","salesforce"] },
{ "canonical_field": "created_date", "strategy": "most_recent" }
```
In plain terms: for legal company **name**, trust NetSuite/Sage (ERP is the source of truth for
identity) over Salesforce. For **annual revenue**, trust Salesforce (sales team keeps this
current) over the ERPs. For **credit limit**, trust Sage (the finance ledger of record). For
date fields, just take whichever value was most recently updated. Four strategies exist overall:
`source_priority`, `most_recent`, `longest` (longest non-null string), `first_non_null` (default).

**Gap found**: the `supplier` entity's match-rule config uses a different/incompatible schema
(`"similarity": "trigram"`, `"confidence_threshold"`) than what `match_rule_engine.py` actually
reads (`similarity_kind`, `match_threshold`), and `"trigram"` isn't one of the three implemented
similarity kinds (`exact`, `jaro_winkler`, `token_set`). This rule is effectively broken/ignored
as written — worth fixing if supplier matching needs to work correctly.

### 9.4 Analytics layer — no new business rules, just cleanup + cataloging

This layer does not apply any additional business logic. It:
1. Strips 6 internal audit fields (`_record_id`, `_source_id`, `contributing_source_records`,
   `survivorship_version`, `match_run_id`, `field_provenance`) — keeps `golden_id`.
2. Republishes every other field **verbatim** — no renaming, no recalculation.
3. Registers/updates the Glue table + today's partition so Athena can query it immediately.

So the "rule" at this layer is really: make the golden record presentable and queryable, not
add more transformation logic.

---

## 10. Golden layer merge = row merge + column merge + new pipeline columns

> **AWS services to show live:** **S3** (bucket `edl-analytics-087972550871`, prefix
> `canonical/company/golden_date=.../golden.parquet` — download and open one file to show the
> 19 columns: 14 business + 5 system fields) and **Lambda**
> (`EdlEntityResolutionPipeline` — the code that produces this file). CLI:
> `aws s3 ls s3://edl-analytics-087972550871/canonical/company/ --recursive`.

The golden layer merges data in three ways at once, not just one:

1. **Row merging**: multiple source records representing the same real company (Salesforce
   Account + Sage Customer + NetSuite Customer, etc.) are collapsed into **one row** via entity
   matching.
2. **Column merging**: that one row combines fields pulled from whichever source has them — a
   union, not an intersection. The `company` golden record has 14 fields, but only 4
   (`full_name`, `phone_number`, `created_date`, `last_modified_date`) exist across all 4
   sources; the rest (e.g. `annual_revenue`/`employee_count`/`industry` from Salesforce-only,
   `credit_limit`/`outstanding_balance` from NetSuite/Sage-only) are source-exclusive fields
   that still get their own permanent column, populated only when that source provides it.
3. **New pipeline-added columns**: on top of that, the pipeline injects fields that exist in no
   source at all — `golden_id` (kept through to the Analytics layer) and internal bookkeeping
   fields like `match_run_id`, `field_provenance`, `contributing_source_records`,
   `survivorship_version` (these are stripped before the Analytics layer, but are real columns
   in the golden/canonical layer).

**Known issue found — now RESOLVED**: `config/entity_resolution/company/survivorship_v1.json`
previously contained two full copies of the policy object concatenated back-to-back (leftover
from the commit that added Sage support), so `json.load()` failed with `Extra data: line 122
column 3`. As of this check the file is valid JSON — a single copy of the same 14
`output_fields` — so the column-merge analysis above holds and the file loads cleanly at runtime.

---

## 11. Demo talking points, by source document

> **AWS services to show live:** none required for this section itself — it's narration and
> business framing. If a business-side question calls for evidence, the fastest supporting
> screens are **Athena** (live record counts, ties to §7), **Cost Explorer** (Console → AWS
> Cost Explorer → filter by tag `cost_center` — backs the COST_ANALYSIS_AND_ROI numbers), and
> **CloudWatch** (alarm history — backs the "we get alerted within 60s" claim).

Condensed, demo-relevant highlights pulled from every doc in the repo. Not a re-summary of
each doc — just the points worth actually saying out loud.

### `docs/LEADERSHIP_BRIEF.md` + `docs/EXECUTIVE_OVERVIEW.md`
- **The problem, in one line**: customer/financial data lived in Salesforce, MySQL, NetSuite
  (pending), Sage Intacct, and Sage X3 with no shared identity, 24–72 hour manual extraction
  delays, no audit trail, and credentials scattered in scripts.
- **What was built**: an automated pipeline that extracts on a schedule, lands data in three
  governed S3 layers, resolves the same customer/company across sources into one golden record,
  masks PII before curated/analytics, and adds a new source as configuration rather than new
  connector code (for sources already onboarded via the generic connector pattern).
- **Live today (dev, rebuilt from scratch and re-verified 2026-07-09)**: 34 Salesforce accounts
  and 36,023 MySQL RDS contract rows — real, queryable in Athena right now, not a mockup. Sage
  Intacct, Sage X3, and NetSuite are code-complete but have no populated credentials, so nothing
  has run for them yet. Say "two of five sources live," not "the platform is live."
- **Before → after table** (good to screen-share): time to data 24–72h → 1–4h (design target,
  not yet measured under sustained load); customer identity 3 disconnected views → 1 golden
  record; new source onboarding 2–4 weeks → 2–3 days config-only (for the already-onboarded
  connector pattern); credential security scripts/.env → Secrets Manager (no automatic rotation
  wired up yet — `rotation_lambda_arn` is unset for every source in
  `infrastructure/modules/secrets/main.tf`; the current mitigation is a daily
  `credential_expiry_notifier` Lambda that sends an SNS alert when a secret is stale, not
  automatic rotation).
- **Status honesty**: Dev deployed and pipeline-verified for 2 of 5 sources; Staging 🔲 not
  provisioned; Production 🔲 pending staging — say this plainly, don't oversell readiness.

### `docs/FAQ_FOR_MANAGEMENT.md`
- **"Why not just buy Fivetran?"** — SaaS list pricing runs $3K–$5K/month *per source* by public
  vendor pricing pages; this platform is an estimated ~$770–800/month total infrastructure
  regardless of source count (not independently benchmarked), plus no vendor lock-in (raw
  Parquet + versioned JSON configs are portable).
- **"What happens if extraction breaks?"** — designed for alerts + automatic retries + DLQ
  replay so previous clean data never disappears and worst case is day-old data — this is the
  design intent, not yet proven under a real production incident.
- **"Are we secure / PII-safe?"** — raw layer is access-controlled and unmasked; PII is masked
  before it ever reaches curated/analytics; all credentials live only in Secrets Manager, never
  logged. Source connections currently traverse the **public internet via the dev NAT Gateway**,
  not a private VPC endpoint — don't claim "no public internet" (see `SEC-2` in
  `architecture/GAP_ANALYSIS_FINDINGS.md`).
- **"What if source schema changes?"** — non-breaking changes (new optional field) flow through
  automatically; breaking changes (field removed/retyped) halt only the transformation stage
  and alert — raw data is never lost, so nothing needs to be re-extracted from the source.
- The "six-gate" source-onboarding process (SOURCE_REGISTRATION → CREDENTIAL_REGISTRATION →
  ENTITY_MAPPING → EXTRACTION_PROFILE → SECURITY_GOVERNANCE → ACCEPTANCE_VALIDATION) is a
  documented checklist concept today, not yet wired into the control-plane API as an enforced
  workflow — say "planned gate," not "nothing skips review."

### `docs/COST_ANALYSIS_AND_ROI.md`
- **AWS infra cost**: ~$770–800/month estimate at current dev-scale volumes — flagged as needing
  a fresh AWS Pricing Calculator check, not an observed bill (staging/prod don't exist yet).
  NAT Gateway is required (Salesforce/NetSuite/Sage are internet APIs), not optional.
- **ROI figures are a planning projection**, not a measured result — no pilot tenant has run at
  scale yet. Present them as "the model says," not "we achieved."
- **Build vs. buy vs. this platform**: directional comparison only; commercial SaaS list pricing
  vs. an in-house build estimate vs. this platform's estimated infra cost — say clearly none of
  these are measured outcomes yet.

### `docs/PLATFORM_STATUS.md` (exact values to actually show on screen)
- Athena: database `edl_analytics`, workgroup `EdlAnalytics` — always filter queries
  by the latest `analytics_date` shown in this doc.
- Live tables today: `edl_analytics.company` (34 rows, from Salesforce accounts). MySQL RDS
  contract data (36,023 rows) is verified extracted and transformed — confirm the exact
  `edl_analytics` table name for contracts against `docs/PLATFORM_STATUS.md` before quoting it
  on screen, since entity-type-to-table naming should be double-checked live.
- Connected sources today: Salesforce ✅, MySQL RDS ✅. Sage Intacct 🟡, Sage X3 🟡, NetSuite 🟡 —
  all three are code-complete but have no populated credentials; say "code-complete, not yet
  connected," not "active."

### `docs/PIPELINE_FLOW.md` + `docs/GLOSSARY_AND_TERMINOLOGY.md`
- Plain-language flow: **Extract → Raw (immutable) → Transform/mask → Curated → Match/merge →
  Golden record → Analytics (Athena-ready)**. Use the filing-cabinet analogy from this file's
  §7 "Layman's explanation" — it lands well with non-technical audiences.
- Golden record = one trusted row per real-world company/person, built by (1) merging matching
  rows, (2) unioning columns from whichever source has them, (3) adding pipeline-only columns
  like `golden_id` and `field_provenance` — see §10 above for the full breakdown.
- SCD Type 1 merge (recent addition, ship this as a highlight): the curated layer for
  incremental entities always holds the **full current state**, not just today's delta, and
  deletions are kept as tombstones (`is_deleted=True`) rather than silently vanishing — this is
  what makes entity resolution and Athena counts trustworthy day to day.
- **Correction to make out loud if PIPELINE_FLOW.md is referenced live**: that doc claims the
  `canonical/` (golden) layer is "Glue-catalogued, Athena-ready" — it is not, today. Only
  `analytics/` is registered in Glue. See §7 above ("Why you can't query the Golden layer
  directly") for the two real reasons (no Glue table, bookkeeping fields not yet stripped).

### `docs/GO_LIVE_READINESS_CHECKLIST.md`
- Good closing slide: dev is fully checked off; staging requires DynamoDB pre-creation +
  Terraform apply; production requires staging sign-off first. Frame remaining work as a
  checklist, not an unknown.
- Rollback story is reassuring to leadership: disabling schedules takes under 5 minutes if a
  critical issue is found post-go-live.

### `docs/PRODUCTION_INCIDENT_RUNBOOK.md` and `docs/SAGE_ERP_IMPLEMENTATION_PLAN.md`
- Only bring these up if asked about operational maturity or the Sage rollout specifically —
  not primary demo material.
- If asked "what happens when something breaks in production": every failure mode (extraction
  failure, quality violation, breaking schema drift, DLQ aging, Lambda OOM) has a named runbook
  with concrete AWS CLI diagnosis steps and an escalation owner — this isn't improvised.
- If asked about Sage specifically: Intacct customer/vendor/AR-invoice/AP-bill are live;
  Sage X3 customer is live. Of the hardening gaps once tracked for "Phase 6," the **Terraform
  secrets placeholder is now DONE** — `infrastructure/modules/secrets/main.tf` defines
  `aws_secretsmanager_secret.sage_intacct_credentials` and `sage_x3_credentials`, each with a
  `DenyAllOtherPrincipals` resource policy restricting reads to the extraction runtime role.
  Remaining open items (credential cache invalidation, supplier entity resolution) are still a
  known, managed backlog, not a surprise.

---

## 12. Live demo walkthrough script (mixed business + technical audience)

> **AWS services to show live, in the order the script uses them:** **EventBridge Scheduler**
> (Step 2) → **Step Functions** (Step 3, console: State machines → `EdlExtractionPipeline`)
> → **S3** (Step 4, all three data buckets) → **Athena** (Step 5, query editor) →
> **Glue** (implicit in Step 5 — the table backing the query) → **CloudWatch** (have Alarms
> open in a spare tab in case Step 8's "what if it breaks" question comes up). Open all of
> these consoles as tabs *before* starting so switching is instant.

An ordered script for actually running the demo, mixing narration with real commands/values
already confirmed elsewhere in this file. Each step notes its source so it stays traceable.

### Step 1 — Frame the problem (30 sec, business)
Say: "Before this platform, customer and financial data lived in five disconnected systems —
Salesforce, MySQL, Sage Intacct, Sage X3, and soon NetSuite — with 24–72 hour manual extraction
delays and no audit trail." *(Source: `docs/LEADERSHIP_BRIEF.md` §"The Problem")*

### Step 2 — Show the schedule (technical, ties to business "it just runs")
Point at the cron table in §1 of this file — pipelines fire nightly, staggered a few minutes
apart between 02:00–03:05 UTC, entirely config-driven from DynamoDB, no Terraform redeploy
needed to change a schedule.

### Step 3 — Show or trigger a pipeline run
If live-triggering, use the exact pattern from `docs/DEVELOPER_GUIDE.md` §8:
```bash
python scripts/trigger_extraction.py \
  --source-id salesforce --entity-id salesforce-account \
  --environment dev --region us-east-1 \
  --state-machine-arn arn:aws:states:us-east-1:087972550871:stateMachine:EdlExtractionPipeline \
  --param object_name=Account
```
Otherwise, open the Step Functions console for `EdlExtractionPipeline` and show a recent
successful execution's stage-by-stage timeline.

### Step 4 — Walk the three S3 layers
Reference bucket purposes from this file's §2:
- `edl-raw-087972550871` — immutable, exactly as received from the source
- `edl-curated-087972550871` — field-mapped, quality-checked, PII-masked, full current state
  (SCD Type 1 merge, see §10)
- `edl-analytics-087972550871` — golden records + BI-ready Parquet, registered in Glue

### Step 5 — Query Athena live
Use the exact working queries from `docs/PLATFORM_STATUS.md` ("Live Data" section):
```sql
SELECT * FROM edl_analytics.company WHERE analytics_date='2026-07-02';
SELECT * FROM edl_analytics.person  WHERE analytics_date='2026-06-29';
SELECT COUNT(*) FROM edl_analytics.contract
WHERE analytics_date='2026-07-02' AND is_deleted = false;
```
Say: "This is real data, no exports, no scripts — anyone with Athena access can run this today."

### Step 6 — Explain the golden record merge (the "wow" moment)
Use the Acme Corp example from `docs/EXECUTIVE_OVERVIEW.md`: "Acme Corp" exists in Salesforce
(CA address, sales rep), Sage Intacct (TX billing address, Net-30 terms), and Sage X3 (tax ID,
credit limit) — the pipeline recognizes these are the same company and produces one row with
everything merged, best value picked per field. Reinforce with this file's §10: it's a row
merge (dedupe), a column merge (union of fields across sources), and new pipeline-only columns
(`golden_id`, `field_provenance`) all in one step.

### Step 7 — Close with the numbers (business)
From `docs/COST_ANALYSIS_AND_ROI.md`: ~$770–$800/month estimated AWS cost for dev; ROI figures
there are a planning projection, not a measured result (no pilot tenant has run at scale yet).
From `docs/LEADERSHIP_BRIEF.md`: Dev deployed and pipeline-verified for 2 of 5 sources
(Salesforce, MySQL RDS); Staging/Production not yet provisioned; Sage/NetSuite are code-complete
but not yet connected.

### Step 8 — Pre-empt the likely objections
Have these three ready from `docs/FAQ_FOR_MANAGEMENT.md`:
1. "Why not buy a SaaS tool?" → directional cost comparison only (not independently benchmarked),
   plus no vendor lock-in (raw data + configs are portable Parquet/JSON).
2. "What if it breaks?" → automatic retry, alerting, previous data never disappears, worst case
   is day-old data — this is the design intent, not yet proven under a real production incident.
3. "Is our data secure?" → PII masked before curated/analytics, credentials only in Secrets
   Manager. Source connections currently traverse the public internet via the dev NAT Gateway
   (not a private VPC endpoint) — see `SEC-2` in `architecture/GAP_ANALYSIS_FINDINGS.md`. No
   production track record exists yet to make any breach claim, positive or negative.

---

## 13. Quick orientation — condensed platform overview

The shortest possible orientation to the platform (what it is, the 16-stage pipeline, and the 13
AWS services touched), condensed from the rest of this file. Each point is tagged with `→ §N`
pointing at the section in *this* file that already covers it in depth — use the tag to jump
straight to the detailed answer.

### 13.1 What this codebase is

A **metadata-driven, connector-based AWS data lake / ETL platform**. It pulls data from
source systems (Salesforce, MySQL RDS, Sage Intacct/X3, NetSuite — code-complete, activation
pending), lands it in S3
in three progressively cleaner layers, cross-matches records across sources into "golden
records," and exposes the result for SQL querying via Athena/Glue. Adding a new data source
or entity is meant to be **config-only** (a DynamoDB/JSON config entry), not a code change —
that's the "metadata-driven" part.

- Three S3 layers → **see §2** (bucket names, purpose, encryption) and **§9** (rules applied
  at each layer).
- Cross-source matching into golden records → **see §5** (curated→golden→analytics flow),
  **§7** (which entity types combine multiple sources), and **§10** (how the merge actually
  works: row + column + new pipeline columns).
- "Config-only, zero code change" claim → **see §8** (full source table inventory, all driven
  by `scripts/seed_entity_config.py`) and demo talking point in **§11** (FAQ section, "What
  happens if we acquire another company with different data systems?").
- Source of truth docs named here (`Enterprise_Data_Lake_Platform_Full_Specification.md`,
  `docs/PIPELINE_FLOW.md`) — **see §11** doc-by-doc talking points for what's actually
  demo-relevant in each.

### 13.2 The pipeline, stage by stage (16 stages)

Each numbered stage below is a real AWS-backed step, run in this order, with a pointer to
where it's covered in more depth elsewhere in this file:

1. **EventBridge Scheduler** — cron rule fires on a schedule (per source/entity). → **§1**
   (full cron table, where schedules live in DynamoDB vs. Terraform).
2. **Step Functions** (`EdlExtractionPipeline`) — orchestrates every stage below as one
   workflow run. → **§6** (Lambda names wired into the state machine).
3. **Config load (DynamoDB)** — reads the `EdlEntityExtractionConfig` table to know what/how
   to extract. → **§1** and **§8** (source table inventory, config fields).
4. **Secrets Manager** — fetches source credentials at
   `edl/sources/{source_id}/credentials`. → **§3** (credential storage, access
   control, known gaps).
5. **Watermark read (DynamoDB)** — checks the last successfully extracted timestamp/id for
   incremental pulls. → **§8** (which entities are incremental vs. full) and **§9.1** (raw
   layer sequence).
6. **Extraction Lambda** — connects to the source (Salesforce API, MySQL RDS via VPC/NAT,
   Sage API) and pulls new/changed records. → **§6** (Lambda console names/handlers) and
   **§9.1**.
7. **Schema snapshot & drift check** — compares incoming schema to the last snapshot, flags
   drift. → **§9.1** (drift classification table: BREAKING / POTENTIALLY_BREAKING /
   NON_BREAKING / NO_DRIFT).
8. **S3 Raw layer** — writes extracted data as immutable Parquet. → **§2** (`edl-raw-087972550871`
   bucket details, Object Lock).
9. **Watermark update (DynamoDB)** — records progress for the next incremental run. → **§9.1**.
10. **Transformation Lambda** — applies field mappings, data-quality checks, writes S3 Curated
    layer. → **§6** (handler name) and **§9.2** (field mapping rules, quality-check gap found).
11. **Entity Resolution Lambda** — matches records across sources (Jaro-Winkler / Jaccard)
    into canonical "golden records." → **§5**, **§6**, **§9.3** (match rules + survivorship,
    including the known broken `supplier` config).
12. **Analytics Publisher Lambda** — writes partitioned Parquet to S3 Analytics layer and
    registers/updates partitions in the **Glue Data Catalog**. → **§4** (Glue's role as
    metadata-only catalog), **§6** (doc mismatch on the Lambda's actual name), **§9.4**.
13. **Athena** — analysts/BI tools query the Analytics layer via SQL. → **§7** ("why you can't
    query the Golden layer directly" — only `analytics/` is Glue-catalogued today).
14. **CloudWatch** — structured logs + metrics emitted at every stage for monitoring/alerting
    (with SNS for alert emails). → not separately covered elsewhere in this file; flag as a
    gap if a demo question goes deep on observability (see `docs/PRODUCTION_INCIDENT_RUNBOOK.md`
    instead, referenced in §11).
15. **IAM** — a distinct least-privilege execution role per Lambda/stage. → **§3** (Secrets
    Manager access scoped to the extraction Lambda role specifically).
16. **KMS** — encrypts S3 data and Secrets Manager values at rest. → **§2** (each bucket in
    `infrastructure/modules/storage/main.tf` has KMS encryption noted).

Infra for all of this is defined in **Terraform** (not CDK/SAM) under `infrastructure/`.

### 13.3 AWS services used, in the order you'll touch them

| # | Service | Role | Cross-reference in this file |
|---|---------|------|-------------------------------|
| 1 | **IAM** | Execution roles for every Lambda + Step Functions + Terraform deployer | **§3** (access control section) |
| 2 | **KMS** | Encryption keys for S3/Secrets Manager | **§2** (bucket encryption) |
| 3 | **S3** | Terraform state bucket, Raw/Curated/Analytics data layers, Lambda artifact storage | **§2** (all 6 buckets, dev names, purposes) |
| 4 | **DynamoDB** | Entity config, watermark repository, run audit log, Terraform lock table (not Terraform-managed itself — created manually) | **§1** (schedules live here), **§8** (entity config source) |
| 5 | **Secrets Manager** | Per-source connector credentials | **§3** (full section — paths, fields, known gaps) |
| 6 | **VPC / Networking / NAT Gateway** | Connectivity from `us-east-1` platform to `us-west-1` MySQL RDS source | not separately covered elsewhere in this file — note if asked live |
| 7 | **Lambda (×4)** | Extraction, Transformation, Entity Resolution, Analytics Publisher | **§6** (console names + handlers table) |
| 8 | **Step Functions** | Orchestrates the pipeline stages into one workflow | **§6** (wiring), **§9** (branching logic per stage) |
| 9 | **EventBridge Scheduler** | Cron-triggers pipeline runs | **§1** (full cron table) |
| 10 | **Glue** | Data Catalog for the Analytics layer | **§4** (Glue's role — pure metadata catalog, no ETL) |
| 11 | **Athena** | SQL querying of the Analytics layer | **§7** (why only `analytics/` is queryable, not `canonical/`) |
| 12 | **CloudWatch** | Logs and metrics | not separately covered elsewhere in this file |
| 13 | **SNS** | Failure/alert email notifications | not separately covered elsewhere in this file |
