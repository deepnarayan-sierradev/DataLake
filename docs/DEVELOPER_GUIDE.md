# Developer Guide — Enterprise Data Lake Platform

**Audience:** Engineers new to the codebase, or anyone setting up a fresh workstation  
**Last updated:** 2026-06-29  
**Status:** Dev environment is live and fully operational

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

The Enterprise Data Lake Platform automatically extracts data from three source systems, transforms and governs it through three S3 layers, resolves customer identity across systems, and delivers trusted analytics-ready records queryable via Athena.

```
Salesforce CRM ──┐
MySQL RDS ───────┼──► Raw Layer (S3) ──► Curated Layer (S3) ──► Analytics Layer (S3)
NetSuite ERP ────┘         │                     │                      │
 (pending)            Immutable           Field-mapped           Golden records
                      Parquet             Quality-checked        Athena-queryable
```

**Orchestration:** EventBridge → Step Functions → Lambda (extraction → transformation → entity resolution → analytics publish)

**Configuration-driven:** adding a new source or entity requires zero code changes — only a DynamoDB config record.

---

## 2. Codebase Module Map

| Module | Purpose |
|---|---|
| `connector_runtime/` | Extracts data from source APIs; writes Parquet to raw layer |
| `transformation/` | Applies field mapping, quality checks, PII masking; writes to curated layer |
| `entity_resolution/` | Cross-source entity matching; writes golden records to analytics layer |
| `analytics_publisher/` | Publishes partitioned analytics Parquet; registers Glue partitions |
| `schema_management/` | Schema snapshot capture and drift detection |
| `watermark_management/` | Incremental extraction watermark read/write |
| `orchestration/` | Step Functions and EventBridge wiring |
| `governance/` | Lineage records, data classification, retention enforcement |
| `observability/` | Structured logging and CloudWatch metrics emission |
| `contracts/` | Shared Pydantic models and interfaces used across all modules |
| `infrastructure/` | Terraform modules and environment configs (`dev/`, `staging/`, `prod/`) |
| `scripts/` | Operational scripts: seeding configs, triggering runs, dry-run connectors |
| `config/` | Field mapping JSON and entity resolution config files |

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

Expected:

```
dev-entity-extraction-config
dev-run-audit-log
dev-watermark-repository
```

### Secrets Manager

```bash
aws secretsmanager list-secrets --region us-east-1 --query 'SecretList[].Name' | grep dev/sources
```

Expected:

```
dev/sources/salesforce/credentials
dev/sources/mysql-rds/credentials
```

### Lambda Functions

```bash
aws lambda list-functions --region us-east-1 --query 'Functions[?starts_with(FunctionName, `dev-`)].FunctionName'
```

Expected:

```
dev-analytics-publisher
dev-entity-resolution-pipeline
dev-extraction-pipeline
dev-transformation-pipeline
```

### Step Functions

```bash
aws stepfunctions list-state-machines --region us-east-1 --query 'stateMachines[?starts_with(name, `dev-`)].name'
```

Expected:

```
dev-data-pipeline
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

# Schema, watermark, observability, contracts, governance, orchestration
pytest schema_management/tests watermark_management/tests observability/tests \
       contracts/tests governance/tests orchestration/tests -v --no-cov
```

### Full CI check suite (same as GitHub Actions)

```bash
ruff check .                           # lint
mypy .                                 # type check
pytest --cov --cov-fail-under=80       # tests + coverage
bandit -r . -c pyproject.toml          # SAST security scan
pip-audit                              # dependency CVE scan
```

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
```

### Query analytics output via Athena

```sql
-- Latest companies
SELECT * FROM dev_edl_analytics.company WHERE analytics_date='2026-06-29';

-- Latest persons
SELECT * FROM dev_edl_analytics.person WHERE analytics_date='2026-06-29';

-- Latest contracts
SELECT COUNT(*) FROM dev_edl_analytics.contract WHERE analytics_date='2026-06-29';
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

**Apply order matters:**

```bash
# 1. IAM first — provides role ARNs to everything else
terraform apply -target=module.iam

# 2. Lambdas (can apply together, need IAM done first)
terraform apply -target=module.lambda_pipeline -target=module.transformation_lambda

# 3. Orchestration last — needs all Lambda ARNs
terraform apply -target=module.orchestration
```

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
  --function-name dev-analytics-publisher \
  --s3-bucket dev-edl-terraform-state --s3-key lambda/extraction-pipeline.zip \
  --region us-east-1
```

### Lambda handlers reference

| Lambda | Handler |
|---|---|
| `dev-extraction-pipeline` | `connector_runtime.extraction_pipeline_handler.lambda_handler` |
| `dev-transformation-pipeline` | `transformation.transformation_pipeline_handler.lambda_handler` |
| `dev-entity-resolution-pipeline` | `entity_resolution.entity_resolution_pipeline_handler.lambda_handler` |
| `dev-analytics-publisher` | `analytics_publisher.analytics_publisher_handler.lambda_handler` |

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
Step Functions (dev-data-pipeline)
    │
    ├─ Step 1: dev-extraction-pipeline Lambda
    │       Reads DynamoDB config → fetches from source API
    │       Writes Parquet to: s3://dev-edl-raw-layer/raw/{source_id}/{entity_id}/extraction_date=YYYY-MM-DD/
    │       Updates watermark in DynamoDB
    │
    ├─ Step 2: dev-transformation-pipeline Lambda
    │       Reads raw Parquet → applies field mapping JSON
    │       Quality checks → PII masking
    │       Writes to: s3://dev-edl-curated-layer/curated/{source_id}/{entity_id}/
    │       Registers Glue table
    │
    ├─ Step 3: dev-entity-resolution-pipeline Lambda
    │       Loads latest curated data from ALL sources per entity type
    │       Runs matching (Jaro-Winkler + Jaccard)
    │       Writes golden records to: s3://dev-edl-analytics-layer/canonical/{entity_type}/
    │
    └─ Step 4: dev-analytics-publisher Lambda
            Writes partitioned analytics Parquet
            Path: s3://dev-edl-analytics-layer/analytics/{entity_type}/analytics_date=YYYY-MM-DD/data.parquet
            Registers Glue partition → queryable in Athena
```

**Entity type mapping:**

| Source entity | Entity type |
|---|---|
| `salesforce-account` | `company` |
| `salesforce-contact` | `person` |
| `mysql-rds-contracts` | `contract` |

---

## 13. Known Gotchas

1. **`terraform init` required after adding new modules** — even if the module directory exists. Forgetting causes "Module not installed" error.

2. **Terraform module apply order** — `module.iam` → (`module.lambda_pipeline` + `module.transformation_lambda`) → `module.orchestration`. Orchestration fails at plan time if any Lambda ARN is empty.

3. **DynamoDB tables are NOT Terraform-managed** — pre-created manually with correct key schemas. Terraform uses `data "aws_dynamodb_table"` lookups. Never recreate them via Terraform.

4. **Raw layer bucket rejects IAM user writes** — `dev-edl-raw-layer` policy allows writes only from `dev-extraction-runtime-role` (Lambda). Local scripts must use `--dry-run`. Full runs go through Step Functions.

5. **Entity config `s3://` prefix required** — `target_raw_s3_prefix` and `schema_snapshot_s3_prefix` in entity configs must start with `s3://`. Bare paths fail Pydantic validation at runtime.

6. **Salesforce `connector_params` must include `object_name`** — e.g. `{"object_name": "Account"}`. Missing this raises `ValueError` at runtime.

7. **Field mapping `behavior` valid values** — `raise_error`, `use_default`, `drop_field`. The value `use_null` does not exist and causes a validation error.

8. **Glue domain name** — source ID `mysql-rds` becomes `mysql_rds` in Glue (dashes → underscores for catalog naming compliance).

9. **MySQL RDS is in `us-west-1`** — the platform is in `us-east-1`. Cross-region connectivity goes through NAT Gateway. NAT IP `3.208.252.220` must be whitelisted in the RDS security group.

10. **S3 Hive partition paths require `=` in prefix pattern** — paths like `extraction_date=2026-06-29` contain `=`. The `_SAFE_S3_PREFIX_PATTERN` regex must allow it.

11. **Salesforce Bulk API returns `""` for null fields** — treated as missing (becomes `None` via `use_default`). This is intentional. A genuine empty string in a Salesforce field will also be treated as missing.
