# Developer Guide — Enterprise Data Lake Platform

**Audience:** Engineers new to the codebase, or anyone setting up a fresh workstation
**Last updated:** 2026-07-07
**Status:** Dev environment is live and fully operational

> If you're using Claude Code (or another AI coding agent) on this repo, read root `CLAUDE.md`
> first — it captures the same setup/verification conventions as this guide plus non-obvious
> traps (the broken bare `mypy .` invocation, the multi-tenancy isolation model, banned
> identifiers) in a form meant to be loaded every session. This guide is the human-onboarding
> version; keep both in sync when either changes.

---

## Table of Contents

1. [What This Platform Does](#1-what-this-platform-does)
2. [Codebase Module Map](#2-codebase-module-map)
3. [Prerequisites — Tools to Install](#3-prerequisites--tools-to-install)
4. [First-Time Setup](#4-first-time-setup)
5. [AWS Dev Profile Setup](#5-aws-dev-profile-setup)
6. [Verify Dev Environment is Healthy](#6-verify-dev-environment-is-healthy)
7. [Running Tests Locally](#7-running-tests-locally)
8. [Running Pipelines](#8-running-pipelines)
9. [Terraform Workflow](#9-terraform-workflow)
10. [Lambda Build and Deploy](#10-lambda-build-and-deploy)
11. [Seeding Configuration Data](#11-seeding-configuration-data)
12. [Understanding the Data Flow](#12-understanding-the-data-flow)
13. [Known Gotchas](#13-known-gotchas)

---

## 1. What This Platform Does

The Enterprise Data Lake Platform automatically extracts data from source systems, transforms and governs it through three S3 layers, resolves customer identity across systems, and delivers trusted analytics-ready records queryable via Athena.

```
Salesforce CRM ──┐
MySQL RDS ────── ┤
Sage ERP ────────┼──► Raw Layer (S3) ──► Curated Layer (S3) ──► Analytics Layer (S3)
NetSuite ERP ────┘         │                     │                      │
 (code-complete,      Immutable           Field-mapped           Golden records
  not yet live)       Parquet             Quality-checked        Athena-queryable
```

**Orchestration:** EventBridge → SQS FIFO → `pipeline-trigger` Lambda → Step Functions → Lambda
(extraction → transformation → entity resolution → analytics publish). Failures route to a DLQ
processed by the `dlq-processor` Lambda (audit record + SNS alert + optional auto-replay).

**Configuration-driven:** adding a new source or entity requires zero code changes — only a
DynamoDB config record (see §11).

**Multi-tenancy:** the platform is multi-tenant-aware, not strictly single-tenant. Every entity
config, watermark, schema snapshot, and entity-type lookup carries a `tenant_code` (default:
`demo`, from `contracts/identifier_policy.DEFAULT_TENANT_CODE`). Isolation is **not yet uniform**:
S3 curated/schema-snapshot keys and the `entity-type-registry` DynamoDB table are genuinely
isolated (bucket prefix / partition key); `entity-extraction-config` and `watermark-repository`
are only isolated by an application-level guard today, and the raw layer isn't tenant-prefixed at
all yet. See `docs/PRODUCTION_INCIDENT_RUNBOOK.md`'s "How tenant isolation actually works today"
section and run `tests/test_tenant_isolation.py` before touching any repository class. A new
Cognito-authenticated SaaS control-plane API (`connector_runtime/api/`) exists for self-service
tenant provisioning and pipeline triggering — it's code-complete but not yet verified against a
live AWS deployment (see §2 and `connector_runtime/CLAUDE.md`).

---

## 2. Codebase Module Map

| Module | Purpose |
|---|---|
| `connector_runtime/` | Extracts data from source APIs; writes Parquet to raw layer. Shared base classes for new connectors: `credential_client.py::SecretsManagerCredentialClient`, `raw_layer_writer.py::RawLayerWriter`, `query_builders/incremental_query_builder.py::build_incremental_select()` — see `connector_runtime/CLAUDE.md` before hand-rolling a new connector. |
| `connector_runtime/api/` | **New.** SaaS control-plane REST API (Cognito/JWT-authenticated) — tenant provisioning, entity registration, pipeline trigger, run status. Code-complete, not yet deployment-verified. |
| `connector_runtime/credential_rotation/` | **New.** Daily Lambda checking source-credential secret age; alerts via SNS if rotation is overdue. |
| `transformation/` | Applies field mapping, quality checks, PII masking; writes to curated layer (tenant-prefixed S3 keys) |
| `entity_resolution/` | Cross-source entity matching; writes golden records to analytics layer. `entity_type_registry.py` now has a DynamoDB-backed `EntityTypeRegistryClient` (tenant-scoped) alongside the original hardcoded fallback dicts. `publishing_shared.py` holds logic shared between the golden/canonical record publishers. |
| `analytics_publisher/` | Publishes partitioned analytics Parquet; registers Glue partitions; emits an end-to-end pipeline SLA metric |
| `schema_management/` | Schema snapshot capture and drift detection (tenant-prefixed S3 keys) |
| `watermark_management/` | Incremental extraction watermark read/write (tenant-checked on read) |
| `orchestration/` | Step Functions and EventBridge wiring. `pipeline_trigger/` (SQS FIFO → Step Functions, rate-limited) and `dlq_processor/` (DLQ → audit + alert + optional replay) are new dedicated Lambdas here, not just Terraform glue. |
| `governance/` | Lineage records, data classification, retention enforcement |
| `observability/` | Structured logging and CloudWatch metrics emission |
| `contracts/` | Shared Pydantic models and interfaces used across all modules — `identifier_policy.py` is the single source of truth for ID/tenant validation regexes; never re-derive them elsewhere |
| `infrastructure/` | Terraform modules and environment configs (`dev/`, `staging/`, `prod/`) — see `infrastructure/CLAUDE.md` |
| `scripts/` | Operational scripts: seeding configs, triggering runs, dry-run connectors |
| `config/` | Field mapping JSON and entity resolution config files |
| `tests/` | **New.** Cross-cutting integration tests only (currently: `test_tenant_isolation.py`) — module-specific tests belong under `<module>/tests/`, not here |

---

## 3. Prerequisites — Tools to Install

| Tool | Required version | Install |
|---|---|---|
| **pyenv** | 2.7.2+ | `brew install pyenv` |
| **Python** | 3.14.6 | `pyenv install 3.14.6` |
| **Terraform** | ≥ 1.8, < 2.0 | `brew install terraform` |
| **AWS CLI** | v2 | `brew install awscli` |
| **GNU Make** | ≥ 3.8 | Included on macOS |
| **Git** | Latest | `brew install git` |

Verify after installing:

```bash
python --version      # Python 3.14.6
terraform version     # Terraform v1.8.x
aws --version         # aws-cli/2.x.x
make --version        # GNU Make 3.8+
```

---

## 4. First-Time Setup

```bash
# 1. Clone the repository
git clone https://github.com/YOUR_ORG/enterprise-data-lake.git
cd enterprise-data-lake

# 2. Set Python version (reads .python-version if present)
pyenv local 3.14.6

# 3. Create virtual environment
python -m venv .venv
source .venv/bin/activate

# 4. Install the project and all dev dependencies
pip install --upgrade pip hatchling
pip install -e ".[dev]"

# 5. Install pre-commit hooks
pre-commit install

# 6. Run full test suite to confirm clean baseline
pytest --cov --cov-fail-under=80
```

**Subsequent sessions — just activate:**

```bash
source .venv/bin/activate
```

---

## 5. AWS Dev Profile Setup

The dev environment runs in AWS account `087972550871`, region `us-east-1`.

```bash
# Configure the dev profile (run once)
aws configure --profile dev
# Enter: Access Key ID, Secret Access Key, region=us-east-1, output=json

# Verify identity
export AWS_PROFILE=dev
aws sts get-caller-identity
# Expected: {"Account": "087972550871", "UserId": "...", "Arn": "arn:aws:iam::087972550871:user/datalake-dev-user"}
```

For SSO-based access:

```bash
aws configure sso --profile dev
```

> **Security note:** Never commit AWS credentials to git. Use `aws configure --profile dev` which stores credentials in `~/.aws/credentials` (not in the repo).

---

## 6. Verify Dev Environment is Healthy

Run these to confirm all dev resources exist before doing any work:

### S3 Buckets

```bash
export AWS_PROFILE=dev
aws s3 ls | grep dev-edl
```

Expected output:

```
dev-edl-analytics-layer
dev-edl-curated-layer
dev-edl-raw-layer
dev-edl-s3-access-logs
dev-edl-schema-snapshots
dev-edl-terraform-state
```

### DynamoDB Tables

```bash
aws dynamodb list-tables --region us-east-1 | grep dev-
```

Expected (see Known Gotcha #3 — whether these are actually Terraform-managed in this account is
currently unverified; don't assume either way):

```
dev-edl-entity-extraction-config
dev-edl-run-audit-log
dev-edl-watermark-repository
dev-edl-entity-type-registry
```

> If you see the same names *without* the `-edl-` infix (e.g. `dev-entity-extraction-config`),
> that's the older naming form referenced in some docs — prefer the `-edl-` form shown above,
> which matches the actual Terraform `name` attribute.

### Secrets Manager

```bash
aws secretsmanager list-secrets --region us-east-1 --query 'SecretList[].Name' | grep dev/sources
```

Expected:

```
dev/sources/salesforce/credentials
dev/sources/netsuite/credentials
dev/sources/mysql-rds/credentials
dev/sources/sage/intacct/credentials
dev/sources/sage/x3/credentials
```

### Lambda Functions

```bash
aws lambda list-functions --region us-east-1 --query 'Functions[?starts_with(FunctionName, `dev-`)].FunctionName'
```

Expected:

```
dev-analytics-layer-publisher
dev-entity-resolution-pipeline
dev-extraction-pipeline
dev-transformation-pipeline
dev-edl-control-plane
dev-edl-credential-expiry-notifier
dev-edl-pipeline-trigger
dev-edl-dlq-processor
```

### Step Functions

```bash
aws stepfunctions list-state-machines --region us-east-1 --query 'stateMachines[?starts_with(name, `dev-`)].name'
```

Expected — there is only one state machine (a previous version of this doc listed a second,
`dev-data-pipeline`, that doesn't exist in `infrastructure/modules/orchestration/main.tf`):

```
dev-extraction-pipeline
```

---

## 7. Running Tests Locally

All tests use `moto` to mock AWS — no real AWS credentials needed for unit tests.

### Full suite (recommended)

```bash
source .venv/bin/activate
pytest --cov --cov-fail-under=80
```

### By module

```bash
# Connector/extraction tests
pytest connector_runtime/tests/ -v --no-cov

# Transformation tests
pytest transformation/tests/ -v --no-cov

# Entity resolution tests
pytest entity_resolution/tests/ -v --no-cov

# Analytics publisher tests
pytest analytics_publisher/tests/ -v --no-cov

# Schema, watermark, observability, contracts, governance, orchestration
pytest schema_management/tests watermark_management/tests observability/tests \
       contracts/tests governance/tests orchestration/tests -v --no-cov

# Cross-cutting integration tests (tenant isolation)
pytest tests/ -v --no-cov
```

### Full CI check suite (same as GitHub Actions)

```bash
ruff check .                           # lint
ruff format --check .                  # formatting (separate CI job from lint)
mypy .                                 # type check — SEE CAVEAT BELOW, currently broken
pytest --cov --cov-fail-under=80       # tests + coverage
bandit -r . -c pyproject.toml          # SAST security scan
pip-audit                              # dependency CVE scan
make banned-names                      # rejects helper/util/common/manager identifiers
```

> **`mypy .` currently fails for reasons unrelated to your change.** It stops immediately on
> `dist/lambda-build/typing_extensions.py` shadowing the real `typing_extensions` package (present
> after running `make lambda-package`), and — once that's worked around — on a module-name
> collision between `scripts/generate_presentation.py` and `pptx/generate_presentation.py`.
> `make typecheck` has the exact same problem (it also just runs bare `mypy .`), so switching to
> the Makefile target doesn't help. **Scope mypy to the packages you actually touched** instead,
> e.g. `mypy connector_runtime schema_management watermark_management observability orchestration
> transformation governance entity_resolution analytics_publisher contracts` (this excludes
> `dist/`, `scripts/`, and `pptx/` by construction). This is tracked as a real bug, not just a
> docs caveat — fixing the root cause (excluding `dist/` from mypy's search path, and renaming one
> of the two colliding `generate_presentation.py` files) is still open.

---

## 8. Running Pipelines

> **Important:** The `dev-edl-raw-layer` S3 bucket policy only allows writes from the Lambda execution role (`dev-extraction-runtime-role`). Local scripts can run with `--dry-run` for schema/connectivity checks, but full extraction must go through Step Functions.

### Dry-run connectors (schema + connectivity check, no S3 write)

```bash
export AWS_PROFILE=dev

python scripts/run_mysql_connector_local.py \
  --entity-id mysql-rds-contracts --dry-run

python scripts/run_salesforce_connector_local.py \
  --entity-id salesforce-account --dry-run

python scripts/run_salesforce_connector_local.py \
  --entity-id salesforce-contact --dry-run

python scripts/run_sage_connector_local.py \
  --entity-id sage-intacct-customer --dry-run

python scripts/run_sage_connector_local.py \
  --entity-id sage-x3-customer --dry-run
```

### Trigger full pipeline via Step Functions

```bash
export AWS_PROFILE=dev

# MySQL RDS — Contracts (full load)
python scripts/trigger_extraction.py \
  --source-id mysql-rds \
  --entity-id mysql-rds-contracts \
  --environment dev \
  --region us-east-1 \
  --state-machine-arn arn:aws:states:us-east-1:087972550871:stateMachine:dev-extraction-pipeline \
  --param table_name=Contracts

# Salesforce — Account (full load)
python scripts/trigger_extraction.py \
  --source-id salesforce \
  --entity-id salesforce-account \
  --environment dev \
  --region us-east-1 \
  --state-machine-arn arn:aws:states:us-east-1:087972550871:stateMachine:dev-extraction-pipeline \
  --param object_name=Account

# Salesforce — Contact (incremental)
python scripts/trigger_extraction.py \
  --source-id salesforce \
  --entity-id salesforce-contact \
  --environment dev \
  --region us-east-1 \
  --state-machine-arn arn:aws:states:us-east-1:087972550871:stateMachine:dev-extraction-pipeline \
  --param object_name=Contact

# Sage Intacct — Customer (incremental)
python scripts/trigger_extraction.py \
  --source-id sage \
  --entity-id sage-intacct-customer \
  --environment dev \
  --region us-east-1 \
  --state-machine-arn arn:aws:states:us-east-1:087972550871:stateMachine:dev-extraction-pipeline \
  --param sage_product=intacct --param object_path=accounts-receivable/customer

# Sage Intacct — Vendor (incremental)
python scripts/trigger_extraction.py \
  --source-id sage \
  --entity-id sage-intacct-vendor \
  --environment dev \
  --region us-east-1 \
  --state-machine-arn arn:aws:states:us-east-1:087972550871:stateMachine:dev-extraction-pipeline \
  --param sage_product=intacct --param object_path=accounts-payable/vendor

# Sage Intacct — AR Invoice (incremental)
python scripts/trigger_extraction.py \
  --source-id sage \
  --entity-id sage-intacct-arinvoice \
  --environment dev \
  --region us-east-1 \
  --state-machine-arn arn:aws:states:us-east-1:087972550871:stateMachine:dev-extraction-pipeline \
  --param sage_product=intacct --param object_path=accounts-receivable/invoice

# Sage Intacct — AP Bill (incremental)
python scripts/trigger_extraction.py \
  --source-id sage \
  --entity-id sage-intacct-apbill \
  --environment dev \
  --region us-east-1 \
  --state-machine-arn arn:aws:states:us-east-1:087972550871:stateMachine:dev-extraction-pipeline \
  --param sage_product=intacct --param object_path=accounts-payable/bill

# Sage X3 — Customer (incremental)
python scripts/trigger_extraction.py \
  --source-id sage \
  --entity-id sage-x3-customer \
  --environment dev \
  --region us-east-1 \
  --state-machine-arn arn:aws:states:us-east-1:087972550871:stateMachine:dev-extraction-pipeline \
  --param sage_product=x3 --param object_path=BPCUSTOMER

# Sage X3 — Supplier (incremental)
python scripts/trigger_extraction.py \
  --source-id sage \
  --entity-id sage-x3-supplier \
  --environment dev \
  --region us-east-1 \
  --state-machine-arn arn:aws:states:us-east-1:087972550871:stateMachine:dev-extraction-pipeline \
  --param sage_product=x3 --param object_path=BPSUPPLIER
```

### Query analytics output via Athena

```sql
-- Latest companies
SELECT * FROM dev_edl_analytics.company WHERE analytics_date='2026-06-29';

-- Latest persons
SELECT * FROM dev_edl_analytics.person WHERE analytics_date='2026-06-29';

-- Latest contracts
SELECT COUNT(*) FROM dev_edl_analytics.contract   WHERE analytics_date='2026-06-29';

-- Latest suppliers (Sage Intacct vendors + Sage X3 suppliers merged)
SELECT COUNT(*) FROM dev_edl_analytics.supplier   WHERE analytics_date='2026-06-29';

-- Latest AR invoices
SELECT COUNT(*) FROM dev_edl_analytics.ar_invoice  WHERE analytics_date='2026-06-29';

-- Latest AP bills
SELECT COUNT(*) FROM dev_edl_analytics.ap_bill     WHERE analytics_date='2026-06-29';
```

---

## 9. Terraform Workflow

```bash
# Validate offline (no backend needed)
terraform -chdir=infrastructure/environments/dev init -backend=false
terraform -chdir=infrastructure/environments/dev validate

# Real plan/apply
cd infrastructure/environments/dev
terraform init        # only needed after adding/changing modules
terraform plan
terraform apply -target=module.<name>
```

**Apply order matters — this is the full order today, not just the original 3 Lambda modules:**

```bash
# 1. IAM first — provides role ARNs to everything else
terraform apply -target=module.iam

# 2. Metadata persistence — DynamoDB tables (entity-type-registry etc.), needed by control_plane
terraform apply -target=module.metadata_persistence

# 3. Lambdas (can apply together, need IAM done first)
terraform apply -target=module.lambda_pipeline -target=module.transformation_lambda \
  -target=module.entity_resolution_lambda -target=module.analytics_publisher_lambda

# 4. Orchestration — needs all Lambda ARNs; also provisions pipeline-trigger and dlq-processor
terraform apply -target=module.orchestration

# 5. Control plane last — depends on iam, orchestration, AND metadata_persistence
terraform apply -target=module.control_plane
```

Or skip `-target` entirely and apply everything in one pass once `terraform validate` is clean —
Terraform resolves the dependency graph itself; the staged order above is only needed when you
want to control blast radius. See `infrastructure/CLAUDE.md` for the full 14-module list and
`make iac-validate` / `make iac-scan` for the CI-equivalent local checks.

> **Critical:** Run `terraform init` after adding any new module, even if the module directory already exists. Forgetting causes "Module not installed" error.

---

## 10. Lambda Build and Deploy

The single zip `dist/extraction-pipeline.zip` serves all Lambda functions (different handlers configured in Terraform).

```bash
# Build the zip
make lambda-package

# Upload to S3 (note the SHA-256 hash printed — save it for Terraform var)
ARTIFACTS_BUCKET=dev-edl-terraform-state make lambda-upload

# After any code change, update deployed Lambdas immediately
AWS_PROFILE=dev aws lambda update-function-code \
  --function-name dev-extraction-pipeline \
  --s3-bucket dev-edl-terraform-state --s3-key lambda/extraction-pipeline.zip \
  --region us-east-1

AWS_PROFILE=dev aws lambda update-function-code \
  --function-name dev-transformation-pipeline \
  --s3-bucket dev-edl-terraform-state --s3-key lambda/extraction-pipeline.zip \
  --region us-east-1

AWS_PROFILE=dev aws lambda update-function-code \
  --function-name dev-entity-resolution-pipeline \
  --s3-bucket dev-edl-terraform-state --s3-key lambda/extraction-pipeline.zip \
  --region us-east-1

AWS_PROFILE=dev aws lambda update-function-code \
  --function-name dev-analytics-layer-publisher \
  --s3-bucket dev-edl-terraform-state --s3-key lambda/extraction-pipeline.zip \
  --region us-east-1
```

### Lambda handlers reference

| Lambda | Handler |
|---|---|
| `dev-extraction-pipeline` | `connector_runtime.extraction_pipeline_handler.lambda_handler` |
| `dev-transformation-pipeline` | `transformation.transformation_pipeline_handler.lambda_handler` |
| `dev-entity-resolution-pipeline` | `entity_resolution.entity_resolution_pipeline_handler.lambda_handler` |
| `dev-analytics-layer-publisher` | `analytics_publisher.analytics_publisher_handler.lambda_handler` |
| `dev-edl-control-plane` | `connector_runtime.api.control_plane_handler.lambda_handler` |
| `dev-edl-credential-expiry-notifier` | `connector_runtime.credential_rotation.credential_expiry_notifier_handler.lambda_handler` |
| `dev-edl-pipeline-trigger` | `orchestration.pipeline_trigger.pipeline_trigger_handler.lambda_handler` |
| `dev-edl-dlq-processor` | `orchestration.dlq_processor.dlq_processor_handler.lambda_handler` |

All eight share the same deployment zip (see §10 above) — a code change to any handler requires
rebuilding and re-uploading once, then an `update-function-code` call per affected function.

---

## 11. Seeding Configuration Data

Entity configs drive all extraction behaviour. They must exist in DynamoDB before any pipeline run.

```bash
export AWS_PROFILE=dev

# Seed all entity extraction configs to DynamoDB
python scripts/seed_entity_config.py --environment dev --region us-east-1

# Seed entity resolution configs to S3
python scripts/seed_entity_resolution_configs.py --environment dev --region us-east-1
```

Configs are defined in `config/` — edit them there and re-seed, never directly in DynamoDB.

---

## 12. Understanding the Data Flow

```
EventBridge Scheduler (cron)
    │
    ▼
SQS FIFO queue ──► dev-edl-pipeline-trigger Lambda (rate-limited) ──► Step Functions (dev-extraction-pipeline)
    │  (the control-plane API's pipeline-trigger route enqueues here too — same path, not a
    │   parallel one)
    │
    ├─ Step 1: dev-extraction-pipeline Lambda
    │       Reads DynamoDB config (tenant_code-scoped) → fetches from source API
    │       Writes Parquet to: s3://dev-edl-raw-layer/raw/{source_id}/{entity_id}/extraction_date=YYYY-MM-DD/run_id={run_id}/
    │         (raw layer is NOT tenant-prefixed yet — see §1 Multi-tenancy note)
    │       Updates watermark in DynamoDB (tenant-checked)
    │       If approaching the Lambda timeout mid-run: commits a partial watermark, emits a
    │       checkpoint audit record, and the state machine exits cleanly via the
    │       `ExtractionCheckpointed` terminal state instead of failing. Automatic resume from a
    │       checkpoint is NOT yet implemented — needs a manual re-trigger.
    │       On unrecoverable failure: message lands on the extraction-failure DLQ →
    │       dev-edl-dlq-processor Lambda (audit record + SNS alert + optional auto-replay)
    │
    ├─ Step 2: dev-transformation-pipeline Lambda
    │       Reads raw Parquet → applies field mapping JSON
    │       Quality checks → PII masking (now actually wired up — see governance module)
    │       SCD Type 1 merge: loads previous curated state, merges delta by
    │       primary_key_field → writes FULL current-state Parquet to curated
    │       (full-load entities: writes delta only, no merge)
    │       Writes to: s3://dev-edl-curated-layer/{tenant_code}/curated/{domain}/{entity_id}/
    │       Registers Glue table
    │
    ├─ Step 3: dev-entity-resolution-pipeline Lambda
    │       Loads latest curated data from ALL sources per entity type — streamed via DuckDB
    │       rather than fully materialized into memory
    │       Resolves entity type via EntityTypeRegistryClient (DynamoDB, tenant-scoped), falling
    │       back to hardcoded seed dicts if no registry record exists
    │       Runs matching (Jaro-Winkler + Jaccard)
    │       Writes golden records to: s3://dev-edl-analytics-layer/canonical/{entity_type}/
    │
    └─ Step 4: dev-analytics-layer-publisher Lambda
            Writes partitioned analytics Parquet
            Path: s3://dev-edl-analytics-layer/analytics/{entity_type}/analytics_date=YYYY-MM-DD/data.parquet
            Registers Glue partition → queryable in Athena
            Emits an end-to-end pipeline SLA metric (run start → analytics publish latency)
```

**Entity type mapping:**

| Source entity | Entity type |
|---|---|
| `salesforce-account` | `company` |
| `salesforce-contact` | `person` |
| `mysql-rds-contracts` | `contract` |
| `sage-intacct-customer` | `company` |
| `sage-x3-customer` | `company` |
| `sage-intacct-vendor` | `supplier` |
| `sage-x3-supplier` | `supplier` |
| `sage-intacct-arinvoice` | `ar_invoice` |
| `sage-intacct-apbill` | `ap_bill` |

---

## 13. Known Gotchas

1. **`terraform init` required after adding new modules** — even if the module directory exists. Forgetting causes "Module not installed" error.

2. **Terraform module apply order** — `module.iam` → `module.metadata_persistence` → (`module.lambda_pipeline` + `module.transformation_lambda` + `module.entity_resolution_lambda` + `module.analytics_publisher_lambda`) → `module.orchestration` → `module.control_plane` (needs `iam`, `orchestration`, *and* `metadata_persistence`). Orchestration and control_plane fail at plan time if any dependency's ARN is empty. See §9.

3. **DynamoDB tables — unresolved doc/infra contradiction, verify before applying.** This doc previously said these tables are "NOT Terraform-managed... Terraform uses `data \"aws_dynamodb_table\"` lookups." That's contradicted by the actual code: `infrastructure/modules/metadata_persistence/main.tf` defines real `aws_dynamodb_table` *resource* blocks (with `prevent_destroy`) for `watermark_repository`, `run_audit_log`, `entity_extraction_config`, and `entity_type_registry` — and this predates the current round of changes (confirmed via `git log` on that file), so it's a pre-existing mismatch, not something newly broken. **Before running `terraform apply` in any environment**, run `terraform state list | grep dynamodb` to check whether these are already tracked in state. If they exist in AWS but aren't in state, `apply` will fail with "already exists"; if you're setting up a fresh environment, they may need to be created via Terraform directly rather than by hand as this doc used to instruct.

4. **Raw layer bucket rejects IAM user writes** — `dev-edl-raw-layer` policy allows writes only from `dev-extraction-runtime-role` (Lambda). Local scripts must use `--dry-run`. Full runs go through Step Functions.

5. **Entity config `s3://` prefix required** — `target_raw_s3_prefix` and `schema_snapshot_s3_prefix` in entity configs must start with `s3://`. Bare paths fail Pydantic validation at runtime.

6. **Salesforce `connector_params` must include `object_name`** — e.g. `{"object_name": "Account"}`. Missing this raises `ValueError` at runtime.

7. **Field mapping `behavior` valid values** — `raise_error`, `use_default`, `drop_field`. The value `use_null` does not exist and causes a validation error.

8. **Glue domain name** — source ID `mysql-rds` becomes `mysql_rds` in Glue (dashes → underscores for catalog naming compliance).

9. **MySQL RDS is in `us-west-1`** — the platform is in `us-east-1`. Cross-region connectivity goes through NAT Gateway. NAT IP `3.208.252.220` must be whitelisted in the RDS security group.

10. **S3 Hive partition paths require `=` in prefix pattern** — paths like `extraction_date=2026-06-29` contain `=`. The `_SAFE_S3_PREFIX_PATTERN` regex must allow it.

11. **Salesforce Bulk API returns `""` for null fields** — treated as missing (becomes `None` via `use_default`). This is intentional. A genuine empty string in a Salesforce field will also be treated as missing.

12. **Sage `connector_params` must include `sage_product` and `object_path`** — e.g. `{"sage_product": "intacct", "object_path": "accounts-receivable/customer"}`. Missing either key raises `ValueError` at runtime. Valid `sage_product` values are `"intacct"` and `"x3"`.

13. **Sage X3 field names must be UPPERCASE** — e.g. `BPCNUM_0`, `MODDAT_0`. The X3 query engine validates against `^[A-Z][A-Z0-9_]{0,63}$`. Lowercase field names in `include_fields` are rejected with `X3QueryBuildError`.

14. **Sage Intacct incremental watermark field is `auditInfo.modifiedAt`** — dot-notation key returned flat in query responses. Set `watermark_field` to this exact string in the entity config.

15. **Sage uses per-product secret paths** — `{env}/sources/sage/intacct/credentials` and `{env}/sources/sage/x3/credentials` are separate Secrets Manager secrets. Intacct requires `token_url`, `client_id`, `client_secret`, `base_url`, `company_id`. X3 requires the same plus `folder` (the X3 company folder, e.g. `"SEED"`).

16. **Sage X3 OData discriminant** — the X3 query engine embeds `"_x3_odata": true` in `query_text` JSON. `SageConnector.execute_extraction()` dispatches on this key to the OData GET path. Do not set this key manually in entity configs.

17. **`primary_key_field` must be a flat canonical field name** — dotted paths like `"auditInfo.id"` are silently treated as missing because `record.get()` only does top-level dict lookup. Use the canonical field name after field mapping (e.g. `"Id"`, `"contact_id"`). Only set this field for incremental entities; `None` (default) leaves pipeline unchanged.

18. **Tombstone soft-delete is the default** — `soft_delete_field=None` means deleted records are never physically removed from the curated or analytics layers. They persist with their source deletion flag (e.g. `is_deleted=True`). Always filter `WHERE is_deleted = false` (or equivalent) in analytics queries to see only active records. To physically remove records, set `soft_delete_field` to the canonical name of the deletion flag field.
