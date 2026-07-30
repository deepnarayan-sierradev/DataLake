# Developer Guide — Enterprise Data Lake Platform

**Audience:** Engineers new to the codebase, or anyone setting up a fresh workstation
**Last updated:** 2026-07-14
**Status:** Dev infrastructure is deployed; Salesforce and MySQL RDS have real credentials and
have run end-to-end. Sage Intacct, Sage X3, and NetSuite are code-complete but still have empty
credential shells (see `docs/PLATFORM_STATUS.md` for current detail).

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
See `docs/PIPELINE_FLOW.md`'s canonical "Multi-tenancy — the canonical isolation model" table
for exactly which layers are genuinely key/prefix-isolated vs. application-level-guard-only vs.
not isolated at all — don't assume uniformity across layers. Run `tests/test_tenant_isolation.py`
before touching any repository class. A new
Cognito-authenticated control-plane API (`connector_runtime/api/`) exists for entity registration,
pipeline triggering, and config/semantic governance — it's code-complete but not yet verified
against a live AWS deployment (see §2 and `connector_runtime/CLAUDE.md`). It deliberately has **no
tenant/user/role provisioning route**: identity belongs to the Identity API, and this repository
only consumes a verified claim.

---

## 2. Codebase Module Map

| Module | Purpose |
|---|---|
| `connector_runtime/` | Extracts data from source APIs; writes Parquet to raw layer. Shared base classes for new connectors: `credential_client.py::SecretsManagerCredentialClient`, `raw_layer_writer.py::RawLayerWriter`, `query_builders/incremental_query_builder.py::build_incremental_select()` — see `connector_runtime/CLAUDE.md` before hand-rolling a new connector. |
| `connector_runtime/api/` | Control-plane REST API (Cognito/JWT-authenticated) — entity registration, pipeline trigger, run status, plus config/semantic governance in `config_governance_routes.py`. **No tenant/user/role provisioning.** Code-complete, not yet deployment-verified. |
| `connector_runtime/adapters/rest_api/` | **New.** The shared REST/report substrate: one `RestApiConnector`, one `RestHttpSession`, and a declarative `RestSourceSpec` per source. The ten SOW sources are specs, not connector classes. |
| `tenancy/` | **New.** Source connections, scope units below `tenant_code`, the scope predicate, aggregate protection, scope attribution, and connection-aware key construction (DL-12). |
| `config_propagation/` | **New.** Run-level config version pinning, effective-config records, restatement events, declared cache-invalidation bases, and audited rollback (DL-11). |
| `data_quality/` | **New.** Quality checks and policies, the exception store, bounded backfill, source reconciliation, the brand registry, and the data dictionary (DL-02). |
| `semantic/` | Semantic model, fiscal calendar, metric lineage, result cache, model governance (maker-checker publish/approve/activate), the authored enterprise model, and the KPI validation harness (DL-03). |
| `serving_store/` | Per-engine loaders plus the view generator, row-level-security policies, merge strategies, and reader-credential delivery (DL-05 serving parts, DL-SERV-*). |
| `workflow_automation/` | **New.** Workflow definitions, the closed action registry, action handlers, and the execution engine with idempotency keys and per-destination circuit breakers (DL-07). |
| `portability/` | **New.** Tenant export, transition package, deletion workflow, and the PHI onboarding gate that fails closed on an unclassified field (DL-08/DL-09). |
| `connector_runtime/credential_rotation/` | **New.** Daily Lambda checking source-credential secret age; alerts via SNS if rotation is overdue. |
| `transformation/` | Applies field mapping, quality checks, PII masking; writes to curated layer (tenant-prefixed S3 keys) |
| `entity_resolution/` | Cross-source entity matching; writes golden records to analytics layer. `entity_type_registry.py` now has a DynamoDB-backed `EntityTypeRegistryClient` (tenant-scoped) alongside the original hardcoded fallback dicts. `publishing_shared.py` holds logic shared between the golden/canonical record publishers. |
| `analytics_publisher/` | Publishes partitioned analytics Parquet; registers Glue partitions; emits an end-to-end pipeline SLA metric |
| `schema_management/` | Schema snapshot capture and drift detection (tenant-prefixed S3 keys) |
| `watermark_management/` | Incremental extraction watermark read/write (tenant-scoped DynamoDB key via `tenant_scoped_key()`) |
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
aws s3 ls | grep datalake-
```

Expected output:

```
datalake-analytics-dev-use1
datalake-curated-dev-use1
datalake-raw-dev-use1
datalake-access-logs-dev-use1
datalake-schema-snapshots-dev-use1
datalake-terraform-state-dev-use1
```

### DynamoDB Tables

```bash
aws dynamodb list-tables --region us-east-1 | grep datalake
```

Expected (see Known Gotcha #3 — whether these are actually Terraform-managed in this account is
currently unverified; don't assume either way):

```
datalake-entity-extraction-config-dev
datalake-run-audit-log-dev
datalake-watermark-dev
datalake-entity-type-registry-dev
```

> Resource names no longer carry an environment prefix — since each of dev/staging/prod now lives
> in its own separate AWS account, the env prefix was redundant and has been dropped. The `datalake`
> workload token is now applied consistently in PascalCase (previously an inconsistent lowercase
> infix present on some resources but not others). The environment is still tracked via an
> `Environment` tag on every resource, not via the resource name.

### Secrets Manager

```bash
aws secretsmanager list-secrets --region us-east-1 --query 'SecretList[].Name' | grep datalake/
```

Expected in dev today (the legacy shared paths — the per-connection migration has not run here
yet; see `make migrate-credentials`):

```
datalake/<env>/sources/salesforce/credentials
datalake/<env>/sources/netsuite/credentials
datalake/<env>/sources/mysql-rds/credentials
datalake/<env>/sources/sage/intacct/credentials
datalake/<env>/sources/sage/x3/credentials
```

After the migration, the resolved path is
`datalake/<env>/tenants/{tenant_code}/connections/{connection_id}/credentials`, with a separate
`...-writeback` secret for the write-back path.

### Lambda Functions

```bash
aws lambda list-functions --region us-east-1 --query 'Functions[?starts_with(FunctionName, `datalake`)].FunctionName'
```

Expected:

```
datalake-analytics-devLayerPublisher
datalake-entity-resolution-dev
datalake-extraction-dev
datalake-transformation-dev
datalake-control-plane-dev
datalake-credential-expiry-notifier-dev
datalake-pipeline-trigger-dev
datalake-dlq-processor-dev
```

### Step Functions

```bash
aws stepfunctions list-state-machines --region us-east-1 --query 'stateMachines[?starts_with(name, `datalake`)].name'
```

Expected — there is only one state machine (a previous version of this doc listed a second,
`dev-data-pipeline`, that doesn't exist in `infrastructure/modules/orchestration/main.tf`):

```
datalake-extraction-dev
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

# SOW programme modules
pytest tenancy/tests config_propagation/tests data_quality/tests \
       workflow_automation/tests portability/tests semantic/tests \
       serving_store/tests -v --no-cov

# Cross-cutting integration tests (tenant isolation, every consumption surface)
pytest tests/ -v --no-cov
```

### Full CI check suite (same as GitHub Actions)

```bash
ruff check .                            # lint
ruff format --check .                   # formatting (separate CI job from lint)
mypy -p connector_runtime -p transformation -p entity_resolution -p analytics_publisher \
     -p orchestration -p observability -p watermark_management -p schema_management \
     -p contracts -p governance -p tenancy -p config_propagation -p data_quality \
     -p workflow_automation -p portability -p semantic \
     -p serving_store                    # type check — SEE CAVEAT BELOW
pytest --cov --cov-fail-under=80        # tests + coverage
bandit -r . --exclude .venv,tests,dist -c pyproject.toml   # SAST security scan
pip-audit                               # dependency CVE scan
make banned-names                       # rejects helper/util/common/manager identifiers
```

> **Never run bare `mypy .`** — it stops immediately on `dist/lambda-build/typing_extensions.py`
> shadowing the real `typing_extensions` package (present after running `make lambda-package`),
> and — once that's worked around — on a module-name collision between
> `scripts/generate_presentation.py` and `pptx/generate_presentation.py`. `make typecheck` has the
> exact same problem (it also just runs bare `mypy .`), so switching to the Makefile target doesn't
> help. Use the scoped `-p` form shown above.
>
> **Both `mypy` (scoped) and `bandit` are green as of 2026-07-28.** The old backlog — 29 mypy errors
> across 11 files, and 20 bandit findings — was cleared as part of DL-SEC-18, whose exit gate is
> "CI fully green including typecheck". So a failure you see now is most likely yours: confirm
> against `HEAD` (`git show HEAD:<file> | mypy -`) before calling anything pre-existing. Bandit
> hard-fails on **any** finding and `[tool.bandit]` declares no skips, so the only permitted
> suppression is an inline `# nosec BXXX — <justification>`.

---

## 8. Running Pipelines

> **Prerequisite:** none of this works yet in dev as of this writing — no source secret is
> populated (see [Connector Credentials](../README.md#connector-credentials-aws-secrets-manager)
> in the README) and no entity config is seeded (§11 below). The commands below are the procedure
> once both are done, not evidence that they've already been run.

> **Important:** The `datalake-raw-dev-use1` S3 bucket policy only allows writes from the Lambda execution role (`datalake-extraction-dev-exec`). Local scripts can run with `--dry-run` for schema/connectivity checks, but full extraction must go through Step Functions.

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
  --state-machine-arn arn:aws:states:us-east-1:087972550871:stateMachine:datalake-extraction-dev \
  --param table_name=Contracts

# Salesforce — Account (full load)
python scripts/trigger_extraction.py \
  --source-id salesforce \
  --entity-id salesforce-account \
  --environment dev \
  --region us-east-1 \
  --state-machine-arn arn:aws:states:us-east-1:087972550871:stateMachine:datalake-extraction-dev \
  --param object_name=Account

# Salesforce — Contact (incremental)
python scripts/trigger_extraction.py \
  --source-id salesforce \
  --entity-id salesforce-contact \
  --environment dev \
  --region us-east-1 \
  --state-machine-arn arn:aws:states:us-east-1:087972550871:stateMachine:datalake-extraction-dev \
  --param object_name=Contact

# Sage Intacct — Customer (incremental)
python scripts/trigger_extraction.py \
  --source-id sage \
  --entity-id sage-intacct-customer \
  --environment dev \
  --region us-east-1 \
  --state-machine-arn arn:aws:states:us-east-1:087972550871:stateMachine:datalake-extraction-dev \
  --param sage_product=intacct --param object_path=accounts-receivable/customer

# Sage Intacct — Vendor (incremental)
python scripts/trigger_extraction.py \
  --source-id sage \
  --entity-id sage-intacct-vendor \
  --environment dev \
  --region us-east-1 \
  --state-machine-arn arn:aws:states:us-east-1:087972550871:stateMachine:datalake-extraction-dev \
  --param sage_product=intacct --param object_path=accounts-payable/vendor

# Sage Intacct — AR Invoice (incremental)
python scripts/trigger_extraction.py \
  --source-id sage \
  --entity-id sage-intacct-arinvoice \
  --environment dev \
  --region us-east-1 \
  --state-machine-arn arn:aws:states:us-east-1:087972550871:stateMachine:datalake-extraction-dev \
  --param sage_product=intacct --param object_path=accounts-receivable/invoice

# Sage Intacct — AP Bill (incremental)
python scripts/trigger_extraction.py \
  --source-id sage \
  --entity-id sage-intacct-apbill \
  --environment dev \
  --region us-east-1 \
  --state-machine-arn arn:aws:states:us-east-1:087972550871:stateMachine:datalake-extraction-dev \
  --param sage_product=intacct --param object_path=accounts-payable/bill

# Sage X3 — Customer (incremental)
python scripts/trigger_extraction.py \
  --source-id sage \
  --entity-id sage-x3-customer \
  --environment dev \
  --region us-east-1 \
  --state-machine-arn arn:aws:states:us-east-1:087972550871:stateMachine:datalake-extraction-dev \
  --param sage_product=x3 --param object_path=BPCUSTOMER

# Sage X3 — Supplier (incremental)
python scripts/trigger_extraction.py \
  --source-id sage \
  --entity-id sage-x3-supplier \
  --environment dev \
  --region us-east-1 \
  --state-machine-arn arn:aws:states:us-east-1:087972550871:stateMachine:datalake-extraction-dev \
  --param sage_product=x3 --param object_path=BPSUPPLIER
```

### Query analytics output via Athena

Real data exists in dev for `company` (Salesforce accounts) and `mysql-rds-contracts` — the
pipeline has run end-to-end for these (see `docs/PLATFORM_STATUS.md`). Other entities (persons,
opportunities, contracts, Sage/NetSuite sources) aren't seeded/connected yet, so treat those rows
as illustrative only. Replace `analytics_date` with a real run's date:

```sql
-- Latest companies
SELECT * FROM datalake_analytics_dev.company WHERE analytics_date='YYYY-MM-DD';

-- Latest persons
SELECT * FROM datalake_analytics_dev.person WHERE analytics_date='YYYY-MM-DD';

-- Latest contracts
SELECT COUNT(*) FROM datalake_analytics_dev.contract   WHERE analytics_date='YYYY-MM-DD';

-- Latest suppliers (Sage Intacct vendors + Sage X3 suppliers merged)
SELECT COUNT(*) FROM datalake_analytics_dev.supplier   WHERE analytics_date='YYYY-MM-DD';

-- Latest AR invoices
SELECT COUNT(*) FROM datalake_analytics_dev.ar_invoice  WHERE analytics_date='YYYY-MM-DD';

-- Latest AP bills
SELECT COUNT(*) FROM datalake_analytics_dev.ap_bill     WHERE analytics_date='YYYY-MM-DD';
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
want to control blast radius. See `infrastructure/CLAUDE.md` for the current full module list and
`make iac-validate` / `make iac-scan` for the CI-equivalent local checks.

> **Critical:** Run `terraform init` after adding any new module, even if the module directory already exists. Forgetting causes "Module not installed" error.

---

## 10. Lambda Build and Deploy

The single zip `dist/extraction-pipeline.zip` serves all Lambda functions (different handlers configured in Terraform).

```bash
# Build the zip
make lambda-package

# Upload to S3 (note the SHA-256 hash printed — save it for Terraform var)
ARTIFACTS_BUCKET=datalake-terraform-state-dev-use1 make lambda-upload

# After any code change, update deployed Lambdas immediately
AWS_PROFILE=dev aws lambda update-function-code \
  --function-name datalake-extraction-dev \
  --s3-bucket datalake-terraform-state-dev-use1 --s3-key lambda/extraction-pipeline.zip \
  --region us-east-1

AWS_PROFILE=dev aws lambda update-function-code \
  --function-name datalake-transformation-dev \
  --s3-bucket datalake-terraform-state-dev-use1 --s3-key lambda/extraction-pipeline.zip \
  --region us-east-1

AWS_PROFILE=dev aws lambda update-function-code \
  --function-name datalake-entity-resolution-dev \
  --s3-bucket datalake-terraform-state-dev-use1 --s3-key lambda/extraction-pipeline.zip \
  --region us-east-1

AWS_PROFILE=dev aws lambda update-function-code \
  --function-name datalake-analytics-devLayerPublisher \
  --s3-bucket datalake-terraform-state-dev-use1 --s3-key lambda/extraction-pipeline.zip \
  --region us-east-1
```

### Lambda handlers reference

| Lambda | Handler |
|---|---|
| `datalake-extraction-dev` | `connector_runtime.extraction_pipeline_handler.lambda_handler` |
| `datalake-transformation-dev` | `transformation.transformation_pipeline_handler.lambda_handler` |
| `datalake-entity-resolution-dev` | `entity_resolution.entity_resolution_pipeline_handler.lambda_handler` |
| `datalake-analytics-devLayerPublisher` | `analytics_publisher.analytics_publisher_handler.lambda_handler` |
| `datalake-control-plane-dev` | `connector_runtime.api.control_plane_handler.lambda_handler` |
| `datalake-credential-expiry-notifier-dev` | `connector_runtime.credential_rotation.credential_expiry_notifier_handler.lambda_handler` |
| `datalake-pipeline-trigger-dev` | `orchestration.pipeline_trigger.pipeline_trigger_handler.lambda_handler` |
| `datalake-dlq-processor-dev` | `orchestration.dlq_processor.dlq_processor_handler.lambda_handler` |

Two further handlers exist in code with **no deployed function yet** (no Terraform `aws_lambda_function`
and no dev deployment):

| Handler | Purpose |
|---|---|
| `connector_runtime.webhook_receiver_handler.lambda_handler` | Provider webhooks: verifies the signature (mandatory, fails closed), dedups on the provider event id, enqueues to the FIFO queue grouped per tenant/connection/entity. Never processes inline. |
| `connector_runtime.writeback_handler.lambda_handler` | Bi-directional write-back, gated on the entity's own `writeback_enabled` flag and using a separate write-back secret. |

All eight share the same deployment zip (see §10 above) — a code change to any handler requires
rebuilding and re-uploading once, then an `update-function-code` call per affected function.

---

## 11. Seeding Configuration Data

> **Migrations first, then the code.** Two migrations must run per environment **before** the
> connection-aware code is deployed there — deploying first takes existing configs dark. Both are
> dry-run by default and reversible:
>
> ```bash
> make migrate-connections   # registers the default connection per source (connection_id == source_id)
> make migrate-credentials   # copies shared per-source secrets to per-connection paths (additive)
> ```
>
> `make seed-semantic-model` publishes the authored enterprise semantic model as a **draft**.
> Activating it needs a named owner's signature per KPI (`--sign owner=entity.metric`), then
> `--approve` by a *different* actor, then `--activate` — maker-checker is enforced by
> `semantic/model_governance.py`, not by the script.


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
SQS FIFO queue ──► datalake-pipeline-trigger-dev Lambda (rate-limited) ──► Step Functions (datalake-extraction-dev)
    │  (the control-plane API's pipeline-trigger route enqueues here too — same path, not a
    │   parallel one)
    │
    ├─ Step 1: datalake-extraction-dev Lambda
    │       Reads DynamoDB config (tenant_code-scoped) → fetches from source API
    │       Writes Parquet to: s3://datalake-raw-dev-use1/{tenant_code}/{source}/{entity_id}/extraction_date=YYYY-MM-DD/run_id={run_id}/
    │         ({source} is one hyphenated segment: salesforce, netsuite, mysql-rds, sage-intacct, sage-x3)
    │       Updates watermark in DynamoDB (tenant-scoped key)
    │       If approaching the Lambda timeout mid-run: commits a partial watermark, emits a
    │       checkpoint audit record, and the state machine exits cleanly via the
    │       `ExtractionCheckpointed` terminal state instead of failing. Automatic resume from a
    │       checkpoint is NOT yet implemented — needs a manual re-trigger.
    │       On unrecoverable failure: message lands on the extraction-failure DLQ →
    │       datalake-dlq-processor-dev Lambda (audit record + SNS alert + optional auto-replay)
    │
    ├─ Step 2: datalake-transformation-dev Lambda
    │       Reads raw Parquet → applies field mapping JSON
    │       Quality checks → PII masking (now actually wired up — see governance module)
    │       SCD Type 1 merge: loads previous curated state, merges delta by
    │       primary_key_field → writes FULL current-state Parquet to curated
    │       (full-load entities: writes delta only, no merge)
    │       Writes to: s3://datalake-curated-dev-use1/{tenant_code}/curated/{domain}/{entity_id}/
    │       Registers Glue table (tenant-scoped table name `{tenant_code}_{entity_id}_{domain}_curated`)
    │         and the run's `curated_date` partition
    │
    ├─ Step 3: datalake-entity-resolution-dev Lambda
    │       Loads latest curated data from ALL sources per entity type — streamed via DuckDB
    │       rather than fully materialized into memory
    │       Resolves entity type via EntityTypeRegistryClient (DynamoDB, tenant-scoped), falling
    │       back to hardcoded seed dicts if no registry record exists
    │       Runs matching (Jaro-Winkler + Jaccard)
    │       Writes golden records to: s3://datalake-analytics-dev-use1/{tenant_code}/canonical/{entity_type}/
    │
    └─ Step 4: datalake-analytics-devLayerPublisher Lambda
            Writes partitioned analytics Parquet
            Path: s3://datalake-analytics-dev-use1/{tenant_code}/analytics/{entity_type}/analytics_date=YYYY-MM-DD/data.parquet
            Registers Glue partition → queryable in Athena
            Emits an end-to-end pipeline SLA metric (run start → analytics publish latency)
```

**Entity type mapping** (`entity_resolution/entity_type_registry.py::ENTITY_ID_TO_TYPE`):

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
| `salesforce-opportunity` | `opportunity` |
| `salesforce-contract` | `sales-contract` |
| `mysql-rds-contractterms` | `contract-term` |

The last three rows are newly added (field mapping under `config/field_mappings/`, entity
resolution config under `config/entity_resolution/`, and registry/seed-script wiring are all in
place) but not yet seeded to any environment's DynamoDB — run `scripts/seed_entity_config.py`
after reviewing the new records. `sales-contract` and `contract-term` are deliberately separate
entity types rather than merged into `contract`: Salesforce Contract and MySQL RDS ContractTerms
share no common key, so merging them would require a fuzzy match that risks combining unrelated
records.

---

## 13. Known Gotchas

1. **`terraform init` required after adding new modules** — even if the module directory exists. Forgetting causes "Module not installed" error.

2. **Terraform module apply order** — `module.iam` → `module.metadata_persistence` → (`module.lambda_pipeline` + `module.transformation_lambda` + `module.entity_resolution_lambda` + `module.analytics_publisher_lambda`) → `module.orchestration` → `module.control_plane` (needs `iam`, `orchestration`, *and* `metadata_persistence`). Orchestration and control_plane fail at plan time if any dependency's ARN is empty. See §9.

3. **DynamoDB tables are all Terraform-managed** — `module.metadata_persistence` defines real `aws_dynamodb_table` resources for all five (`entity_extraction_config`, `entity_type_registry`, `run_audit_log`, `source_onboarding_registry`, `watermark_repository`). Don't create any of them by hand.

4. **Raw layer bucket rejects IAM user writes** — `datalake-raw-dev-use1` policy allows writes only from `datalake-extraction-dev-exec` (Lambda). Local scripts must use `--dry-run`. Full runs go through Step Functions.

5. **Entity config `s3://` prefix required** — `target_raw_s3_prefix` and `schema_snapshot_s3_prefix` in entity configs must start with `s3://`. Bare paths fail Pydantic validation at runtime.

6. **Lambda `environment.variables` can never set `AWS_REGION`** (or any other AWS-reserved key) — `CreateFunction`/`UpdateFunctionConfiguration` reject the whole request. Lambda injects it automatically — see `infrastructure/CLAUDE.md` for detail.

7. **A queue's `visibility_timeout_seconds` must be ≥ the timeout of any Lambda consuming it via an event source mapping**, or `CreateEventSourceMapping` fails outright — see `infrastructure/CLAUDE.md`.

8. **A one-attribute fix in one module can force spurious security-group/Lambda-permission replacement in every module that consumes its outputs** (Terraform defers `data.aws_region`/`data.aws_caller_identity`/`data.aws_vpc` reads to apply-time whenever their containing module "depends on a module with changes pending"). Land small unrelated fixes via `-target` first, then re-plan the full environment. See `infrastructure/CLAUDE.md` for the full mechanism.

9. **Never assume an environment's AWS account is orphan-free just because nothing was "officially" deployed there.** A prior deployment torn down by deleting only the big, visible resources (not via `terraform destroy`) can leave SQS queues, Secrets Manager secrets, CloudWatch Logs query definitions, an X-Ray group, a Glue resource policy, or an EventBridge Scheduler group behind, blocking the next `apply` with `AlreadyExists` errors. Run an inventory sweep before assuming a clean slate — see `infrastructure/CLAUDE.md` for the exact commands.

10. **`make lambda-package` is not byte-reproducible** (unpinned dependency ranges in `pyproject.toml`) — running it twice, or running `lambda-package` then `lambda-upload` as separate commands, can upload a different artifact than the one whose hash you copied. Always run `make lambda-deploy` as a single command, which updates all eight Lambda functions in one pass — see `infrastructure/CLAUDE.md`.

11. **Salesforce `connector_params` must include `object_name`** — e.g. `{"object_name": "Account"}`. Missing this raises `ValueError` at runtime.

12. **Field mapping `behavior` valid values** — `raise_error`, `use_default`, `drop_field`. The value `use_null` does not exist and causes a validation error.

13. **Glue domain name** — source ID `mysql-rds` becomes `mysql_rds` in Glue (dashes → underscores for catalog naming compliance).

14. **MySQL RDS is in `us-west-1`** — the platform is in `us-east-1`. Cross-region connectivity goes through NAT Gateway. NAT IP `3.208.252.220` must be whitelisted in the RDS security group.

15. **S3 Hive partition paths require `=` in prefix pattern** — paths like `extraction_date=2026-06-29` contain `=`. The `_SAFE_S3_PREFIX_PATTERN` regex must allow it.

16. **Salesforce Bulk API returns `""` for null fields** — treated as missing (becomes `None` via `use_default`). This is intentional. A genuine empty string in a Salesforce field will also be treated as missing.

17. **Sage `connector_params` must include `sage_product` and `object_path`** — e.g. `{"sage_product": "intacct", "object_path": "accounts-receivable/customer"}`. Missing either key raises `ValueError` at runtime. Valid `sage_product` values are `"intacct"` and `"x3"`.

18. **Sage X3 field names must be UPPERCASE** — e.g. `BPCNUM_0`, `MODDAT_0`. The X3 query engine validates against `^[A-Z][A-Z0-9_]{0,63}$`. Lowercase field names in `include_fields` are rejected with `X3QueryBuildError`.

19. **Sage Intacct incremental watermark field is `auditInfo.modifiedAt`** — dot-notation key returned flat in query responses. Set `watermark_field` to this exact string in the entity config.

20. **Sage uses per-product secret paths** — `datalake/<env>/sources/sage/intacct/credentials` and `datalake/<env>/sources/sage/x3/credentials` are separate Secrets Manager secrets. Intacct requires `token_url`, `client_id`, `client_secret`, `base_url`, `company_id`. X3 requires the same plus `folder` (the X3 company folder, e.g. `"SEED"`).

21. **Sage X3 OData discriminant** — the X3 query engine embeds `"_x3_odata": true` in `query_text` JSON. `SageConnector.execute_extraction()` dispatches on this key to the OData GET path. Do not set this key manually in entity configs.

22. **`primary_key_field` must be a flat canonical field name** — dotted paths like `"auditInfo.id"` are silently treated as missing because `record.get()` only does top-level dict lookup. Use the canonical field name after field mapping (e.g. `"Id"`, `"contact_id"`). Only set this field for incremental entities; `None` (default) leaves pipeline unchanged.

23. **Tombstone soft-delete is the default** — `soft_delete_field=None` means deleted records are never physically removed from the curated or analytics layers. They persist with their source deletion flag (e.g. `is_deleted=True`). Always filter `WHERE is_deleted = false` (or equivalent) in analytics queries to see only active records. To physically remove records, set `soft_delete_field` to the canonical name of the deletion flag field.
