# Deployment Guide — Enterprise Data Lake Platform

**Version:** 3.0  
**Date:** 2026-06-29  
**Audience:** Platform engineers promoting to a new environment (staging/prod)

> **Dev environment is complete.** Local and dev deployments are fully operational as of 2026-06-29. This guide is now focused on promoting to **staging** and **production**. For developer setup, see [DEVELOPER_GUIDE.md](DEVELOPER_GUIDE.md). For current resource names, see [PLATFORM_STATUS.md](PLATFORM_STATUS.md).

---

## Table of Contents

1. [Prerequisites](#1-prerequisites)
2. [AWS Prerequisites — Must Exist Before Terraform](#2-aws-prerequisites--must-exist-before-terraform)
3. [Deployment Overview — The Seven Phases](#3-deployment-overview--the-seven-phases)
4. [Phase 1 — Bootstrap (One-time Only)](#4-phase-1--bootstrap-one-time-only)
5. [Phase 2 — Infrastructure Deployment (Terraform)](#5-phase-2--infrastructure-deployment-terraform)
6. [Phase 3 — Application Deployment (Lambda)](#6-phase-3--application-deployment-lambda)
7. [Phase 4 — Automatic Pipeline Configuration (Step Functions)](#7-phase-4--automatic-pipeline-configuration-step-functions)
8. [Phase 5 — Data Configuration (DynamoDB Seeds + Secrets)](#8-phase-5--data-configuration-dynamodb-seeds--secrets)
9. [Phase 6 — Field Mapping Configuration](#9-phase-6--field-mapping-configuration)
10. [Phase 7 — Entity Resolution Config](#10-phase-7--entity-resolution-config)
11. [All AWS Settings Reference — What to Set and Where](#11-all-aws-settings-reference--what-to-set-and-where)
12. [Promoting to Staging and Production](#12-promoting-to-staging-and-production)
13. [Verification Checklist](#13-verification-checklist)
14. [Troubleshooting](#14-troubleshooting)

---

## 1. Prerequisites

### Tools Required

Install the following on your workstation before proceeding:

```bash
# Check versions after installing
terraform version    # must be >= 1.8, < 2.0
aws --version        # AWS CLI v2 — any recent version
python --version     # 3.14.x (managed by pyenv)
make --version       # GNU Make >= 3.8
zip --version        # standard zip utility
openssl version      # for SHA-256 hash of Lambda package
```

**Install links:**
- Terraform: https://developer.hashicorp.com/terraform/install
- AWS CLI v2: https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html
- pyenv + Python 3.14: see [README.md](../README.md#developer-setup)

### AWS Account Requirements

You need an AWS account with:
- An IAM user or role with `AdministratorAccess` **for the bootstrap step only** — after bootstrap you use the least-privilege CI/CD role
- AWS CLI configured: `aws configure` (sets `~/.aws/credentials` and `~/.aws/config`)
- Your account ID: `aws sts get-caller-identity --query Account --output text`

### Repository Setup

```bash
git clone https://github.com/YOUR_ORG/enterprise-data-lake.git
cd enterprise-data-lake

# Install Python dev dependencies
python -m venv .venv
source .venv/bin/activate
make install

# Confirm all tests pass (safety check before deploy)
make test
```

---

## 2. AWS Prerequisites — Must Exist Before Terraform

> **Critical:** Terraform manages almost all AWS resources in this platform, but a small set of resources **must be created manually before `terraform init` can run**. These are bootstrapping dependencies — Terraform cannot create its own remote state backend using itself.
>
> Additionally, several resources must exist **before** specific Terraform modules are applied, because those modules reference them as data sources (`data "aws_..."`).

---

### 2.1 Terraform Remote State Backend (per environment)

These three resources must exist **before** `terraform init`. They hold Terraform's own state file and prevent concurrent applies from corrupting it.

| Resource | Name pattern | How to create |
|---|---|---|
| S3 bucket (state file) | `edl-terraform-state-<ACCOUNT_ID>` — S3 bucket names must be globally unique across all of AWS, so this is discriminated by AWS account ID rather than environment name | `aws s3api create-bucket` (see Phase 1, Step 1.2) |
| DynamoDB table (state lock) | `EdlTerraformStateLock` — same literal name in every account, since the AWS account boundary (not the name) keeps dev/staging/prod state isolated | `aws dynamodb create-table` (see Phase 1, Step 1.3) |
| KMS key (state encryption) | alias `EdlTerraformState` — same literal alias in every account (a separate key/alias *instance* is still created per account) | `aws kms create-key` + `aws kms create-alias` (see Phase 1, Step 1.4) |

**Why Terraform cannot create these itself:** The S3 backend configuration in `backend.tf` is resolved _before_ any Terraform resources are applied. If the bucket doesn't exist, `terraform init` fails immediately with `NoSuchBucket`. There is no way around this — it is a fundamental constraint of how Terraform remote state works.

---

### 2.2 GitHub Actions OIDC Provider

The CI/CD deployment role (created by the `iam` Terraform module) trusts GitHub Actions via OIDC federation. The OIDC provider must be registered in your AWS account **once** before Terraform applies the IAM module.

**Why Terraform cannot create this automatically:** The OIDC provider is an account-level resource. Creating it inside the `iam` module would create a separate provider per environment (`dev`, `staging`, `prod`) which would conflict. It must be registered once at the account level.

**Check if it already exists:**

```bash
aws iam list-open-id-connect-providers --query \
  "OpenIDConnectProviderList[?contains(Arn,'token.actions.githubusercontent.com')]"
```

**If it does not exist, create it:**

```bash
aws iam create-open-id-connect-provider \
  --url https://token.actions.githubusercontent.com \
  --client-id-list sts.amazonaws.com \
  --thumbprint-list 6938fd4d98bab03faadb97b34396831e3780aea1
```

> **Note:** The thumbprint `6938fd4d98bab03faadb97b34396831e3780aea1` is the GitHub Actions OIDC thumbprint as of 2026. Verify it against [GitHub's current documentation](https://docs.github.com/en/actions/deployment/security-hardening-your-deployments/about-security-hardening-with-openid-connect) before registering.

---

### 2.3 Lambda Deployment Package in S3

The `lambda_pipeline` Terraform module references a Lambda zip file in S3 via `var.lambda_package_s3_bucket` and `var.lambda_package_s3_key`. If the zip does not exist in S3 at `terraform apply` time, the `aws_lambda_function` resource will fail.

**Correct order:**

```
make lambda-deploy           # builds the zip, uploads to S3, applies Terraform — see Phase 3, Step 3.1
                              # (do not run lambda-package/lambda-upload/terraform apply as separate
                              # commands — the build isn't byte-reproducible; see Step 3.1's warning)
```

**If you run `terraform apply` before uploading:** You will get:
```
Error: error creating Lambda Function: InvalidParameterValueException:
  Error occurred while GetObject.
  S3 Error Code: NoSuchKey
```

Fix: upload the zip first, then re-run `terraform apply`.

---

### 2.4 VPC Tagged with Environment Name

The `lambda_pipeline` module locates the VPC to place Lambda into using a tag filter:

```hcl
data "aws_vpc" "selected" {
  filter {
    name   = "tag:Environment"
    values = [var.environment]
  }
}
```

The `networking` module creates this VPC and tags it correctly. This means:

**Networking module must be applied before the lambda_pipeline module.**

The environment `main.tf` files already enforce this via `depends_on = [module.networking]`. However if you are selectively applying modules (e.g. `terraform apply -target=module.lambda_pipeline`), the networking module must have been applied first.

**To verify the VPC exists and is tagged:**

```bash
aws ec2 describe-vpcs \
  --filters "Name=tag:Environment,Values=dev" \
  --query "Vpcs[*].{VpcId:VpcId,State:State,CIDR:CidrBlock}"
```

---

### 2.5 Pipeline Lambda ARNs (for Step Functions state machine)

The orchestration module's Step Functions state machine references **five Lambda function ARN variables** — one per pipeline stage — but only **two** of them are externally-supplied `terraform.tfvars` values today. The other three are wired automatically from Terraform module outputs:

| Variable | How it's supplied |
|---|---|
| `extraction_pipeline_lambda_arn` | **Manual** — plain `variable` block, `default = ""`, in `infrastructure/environments/{env}/variables.tf`. Even though `module.lambda_pipeline` creates this Lambda, its ARN is not auto-wired into the orchestration module — you must set it in `terraform.tfvars` yourself. |
| `transformation_pipeline_lambda_arn` | **Automatic** — `module.transformation_lambda.lambda_function_arn` |
| `entity_resolution_lambda_arn` | **Automatic** — `module.entity_resolution_lambda.lambda_function_arn` |
| `analytics_publisher_lambda_arn` | **Automatic** — `module.analytics_publisher_lambda.lambda_function_arn` |
| `serving_store_loader_lambda_arn` | **Manual, but optional** — plain `variable` block, `default = ""`. No Terraform module builds this Lambda (see [Section 6 note on the serving-store stage](#6-phase-3--application-deployment-lambda)). Leaving it at the default `""` is expected — the orchestration module substitutes a `Pass` state for that stage instead of failing. |

**Only `extraction_pipeline_lambda_arn` needs a manual ARN before the first full `terraform apply`.** In practice this means:

```
1. Bootstrap the extraction Lambda zip with `make lambda-deploy` (never `lambda-package` and
   `lambda-upload` as two separate commands — see Step 3.1's warning) and set
   lambda_package_source_hash — this is what module.lambda_pipeline,
   module.transformation_lambda, module.entity_resolution_lambda, and
   module.analytics_publisher_lambda all deploy from (same zip, different handler).
2. terraform apply — creates extraction, transformation, entity-resolution, and
   analytics-publisher Lambdas in one pass (transformation/entity-resolution/
   analytics-publisher ARNs feed into the orchestration module automatically).
3. Set extraction_pipeline_lambda_arn in terraform.tfvars from the now-deployed
   extraction Lambda, and re-apply so the orchestration module picks it up.
```

**If you apply before `extraction_pipeline_lambda_arn` is set:** `terraform apply` will fail validation because the variable is required with no usable default. You will see:
```
Error: No value for required variable
  var.extraction_pipeline_lambda_arn
```

Fix: deploy the Lambda package first (Phase 3), collect the extraction Lambda's ARN, then set it in `terraform.tfvars`. See [Section 6 / Phase 3](#6-phase-3--application-deployment-lambda) for the updated, Terraform-managed flow for the other three pipeline Lambdas.

---

### 2.6 Source System Network Access

The extraction Lambda runs inside a private VPC and reaches external source systems (Salesforce, NetSuite) via NAT Gateway. Before the first extraction run:

| Requirement | What to do | Where to get the value |
|---|---|---|
| Salesforce Connected App IP allowlist | Add NAT Gateway public IPs to the Connected App's IP ranges in Salesforce Setup | `terraform output nat_gateway_public_ips` |
| NetSuite IP restrictions | Add NAT Gateway public IPs to the Integration record's IP address restriction | `terraform output nat_gateway_public_ips` |
| MySQL RDS security group | Ensure RDS security group allows inbound port 3306 from Lambda security group ID | `terraform output lambda_security_group_id` |

**Get NAT Gateway IPs after Terraform apply:**

```bash
cd infrastructure/environments/dev
terraform output nat_gateway_public_ips
# Output: ["1.2.3.4", "5.6.7.8", "9.10.11.12"]
```

> **Important:** If you ever recreate the NAT Gateways (e.g. by destroying and recreating networking), the public IPs change and you must update all source system allowlists before extractions will succeed.

---

### 2.7 SNS Email Subscription Confirmation

Terraform creates the SNS alert topic and subscribes `var.alert_email` to it. AWS sends a confirmation email to that address. **CloudWatch alarms will not deliver notifications until the subscription is confirmed.**

**After `terraform apply`:** Check the inbox for `alert_email` and click "Confirm subscription" within 72 hours. If the email expires, re-subscribe:

```bash
aws sns subscribe \
  --topic-arn "$(cd infrastructure/environments/dev && terraform output platform_alerts_topic_arn)" \
  --protocol email \
  --notification-endpoint "ops@yourcompany.com" \
  --region us-east-1
```

---

### 2.8 AWS Service Limits to Check

For production deployments, verify these service limits in your AWS account **before applying**. The defaults are sufficient for dev but may need increasing for staging/prod.

| Service | Limit to check | Default | Recommended for prod |
|---|---|---|---|
| Lambda | Concurrent executions per region | 1,000 | Request increase to 3,000+ |
| Step Functions | Express Workflow starts per second | 6,000 | Sufficient for current scale |
| Step Functions | Standard Workflow execution history | 25,000 executions | Sufficient |
| DynamoDB | Read/Write capacity (on-demand mode) | No hard limit | Monitor with CloudWatch |
| S3 | PUT requests per prefix per second | 3,500 | Sufficient for current scale |
| Secrets Manager | API calls per second | 500 | Sufficient |
| KMS | Requests per second | 10,000 per key | Sufficient |

**Check current limits:**

```bash
aws service-quotas list-service-quotas \
  --service-code lambda \
  --query "Quotas[?QuotaName=='Concurrent executions'].[Value,QuotaArn]" \
  --region us-east-1
```

---

### Summary — Complete Prerequisites Checklist

Before running `terraform init` for any environment:

- [ ] AWS account with admin access available (bootstrap only)
- [ ] Terraform state S3 bucket created (`edl-terraform-state-<ACCOUNT_ID>`)
- [ ] Terraform state DynamoDB lock table created (`EdlTerraformStateLock`)
- [ ] Bootstrap KMS key created (`alias/EdlTerraformState`)
- [ ] `backend.tf` updated to match the above names
- [ ] GitHub Actions OIDC provider registered in AWS IAM (once per account)
- [ ] Lambda deployment package built and uploaded to S3 (before `terraform apply`)
- [ ] Extraction Lambda ARN available and set in `terraform.tfvars` (before orchestration module apply) — transformation/entity-resolution/analytics-publisher ARNs wire automatically; serving-store-loader ARN is left at its default `""` (not yet built)
- [ ] NAT Gateway IPs whitelisted in Salesforce and NetSuite (after networking apply)
- [ ] MySQL RDS security group allows inbound from Lambda SG (after networking apply)
- [ ] SNS subscription confirmation email clicked (after first Terraform apply)
- [ ] AWS service limits reviewed for production

---

## 3. Deployment Overview — The Seven Phases

```
PHASE 1             PHASE 2                  PHASE 3              PHASE 4
BOOTSTRAP           INFRASTRUCTURE           APPLICATION          PIPELINE CONFIG
(one-time)          (Terraform)              (Lambda)             (Step Functions)
──────────          ──────────────────────   ─────────────────    ────────────────
Create S3           terraform init         → make lambda-deploy   Set extraction Lambda
state bucket      → terraform plan           (builds, uploads,    ARN in terraform.tfvars
Create DynamoDB   → terraform apply          applies in one pass) → terraform apply
lock table          (VPC, S3, DynamoDB,     (deploys extraction,  (creates chained state
Create KMS key      IAM, Secrets, SFN,       transformation,       machine; serving-store
Register OIDC       CloudWatch, Glue,        entity-resolution,    stage no-ops until
provider             control-plane)          analytics-publisher,  that Lambda is built)
                                             control-plane,
                                             pipeline-trigger,
                                             dlq-processor, and
                                             credential-expiry
                                             Lambdas)

PHASE 5                                    PHASE 6                        PHASE 7
DATA CONFIGURATION                         FIELD MAPPINGS                 ENTITY RESOLUTION CONFIG
───────────────────────────────────────    ─────────────────────────────  ───────────────────────────────────
aws secretsmanager put-secret-value      → python scripts/                python scripts/
python scripts/seed_entity_config.py       seed_field_mappings.py       → seed_entity_resolution_configs.py
python scripts/seed_schedules.py           (publishes JSON files          (publishes match rules +
(EventBridge schedules)                    from config/ to S3)            survivorship policy from
                                          → Verify first automated        config/entity_resolution/ to S3)
                                            end-to-end run
```

---

## 4. Phase 1 — Bootstrap (One-time Only)

The Terraform remote state backend (S3 bucket + DynamoDB lock table) must exist **before** `terraform init` can run. This is a manual one-time step.

### Step 1.1 — Set environment variables

```bash
export AWS_PROFILE=your-admin-profile    # or set AWS_ACCESS_KEY_ID + AWS_SECRET_ACCESS_KEY
export AWS_REGION=us-east-1
export ENV=dev                           # selects infrastructure/environments/<env> (i.e. which AWS account you're bootstrapping) — change to staging or prod when promoting; no longer used to build resource name strings
export ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
```

### Step 1.2 — Create the Terraform state bucket

```bash
aws s3api create-bucket \
  --bucket edl-terraform-state-${ACCOUNT_ID} \
  --region ${AWS_REGION} \
  --create-bucket-configuration LocationConstraint=${AWS_REGION}

# Enable versioning (required — protects state file)
aws s3api put-bucket-versioning \
  --bucket edl-terraform-state-${ACCOUNT_ID} \
  --versioning-configuration Status=Enabled

# Block all public access
aws s3api put-public-access-block \
  --bucket edl-terraform-state-${ACCOUNT_ID} \
  --public-access-block-configuration \
    "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true"

# Enable default SSE-S3 encryption (upgraded to KMS after bootstrap)
aws s3api put-bucket-encryption \
  --bucket edl-terraform-state-${ACCOUNT_ID} \
  --server-side-encryption-configuration \
    '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'
```

### Step 1.3 — Create the DynamoDB state lock table

```bash
aws dynamodb create-table \
  --table-name EdlTerraformStateLock \
  --attribute-definitions AttributeName=LockID,AttributeType=S \
  --key-schema AttributeName=LockID,KeyType=HASH \
  --billing-mode PAY_PER_REQUEST \
  --region ${AWS_REGION}
```

> The lock table name is a fixed literal (`EdlTerraformStateLock`) — it is no longer environment-prefixed, since each environment lives in its own AWS account and the account boundary, not the name, is what keeps them apart. You still create one *instance* of this table per account.

### Step 1.4 — Create the bootstrap KMS key for state encryption

```bash
KEY_ID=$(aws kms create-key \
  --description "${ENV} Terraform state encryption" \
  --region ${AWS_REGION} \
  --query KeyMetadata.KeyId \
  --output text)

aws kms create-alias \
  --alias-name alias/EdlTerraformState \
  --target-key-id ${KEY_ID} \
  --region ${AWS_REGION}

echo "KMS key ID: ${KEY_ID}"
```

> As with the lock table, the alias (`alias/EdlTerraformState`) is the same literal string in every account — you still create a separate key/alias instance per account.

### Step 1.5 — Update backend.tf

Open `infrastructure/environments/${ENV}/backend.tf` and confirm the values match what you just created:

```hcl
terraform {
  backend "s3" {
    bucket         = "edl-terraform-state-087972550871" # ← your bucket name (dev account ID shown; substitute your own ACCOUNT_ID)
    key            = "environments/dev/terraform.tfstate"
    region         = "us-east-1"                    # ← your region
    encrypt        = true
    kms_key_id     = "alias/EdlTerraformState"      # ← same alias name in every account
    dynamodb_table = "EdlTerraformStateLock"        # ← same table name in every account
  }
}
```

### Step 1.6 — Check for orphaned resources from a prior deployment

**Do this even if you believe the account has never been deployed to.** An account with no S3
buckets, Lambda functions, IAM roles, or DynamoDB tables can still hold leftover SQS queues,
Secrets Manager secrets, CloudWatch Logs query definitions, an X-Ray group, a Glue resource
policy, or an EventBridge Scheduler group from an earlier deployment that was torn down by
deleting the big, visible resources by hand instead of running `terraform destroy`. Any of these
blocks `terraform apply` with an `AlreadyExists`/`Conflict` error. Run this before your first
`terraform init` in any environment:

```bash
export AWS_PROFILE=your-admin-profile
export AWS_REGION=us-east-1

aws sqs list-queues --region ${AWS_REGION}
aws secretsmanager list-secrets --include-planned-deletion --region ${AWS_REGION} \
  --query "SecretList[?starts_with(Name,'edl/')]"
aws logs describe-query-definitions --region ${AWS_REGION} \
  --query "queryDefinitions[?starts_with(name,'edl/')]"
aws xray get-groups --region ${AWS_REGION}
aws glue get-resource-policy --region ${AWS_REGION}
aws scheduler list-schedule-groups --region ${AWS_REGION}
```

If any of these return results and you don't recognize them as belonging to a deployment you
intend to keep, delete them before proceeding (see `infrastructure/CLAUDE.md`'s hard-rules section
for the exact reasoning) — otherwise `terraform apply` will fail partway through with
name-collision errors that are easy to mistake for a real configuration problem.

---

## 5. Phase 2 — Infrastructure Deployment (Terraform)

### Step 2.1 — Configure terraform.tfvars

Edit `infrastructure/environments/dev/terraform.tfvars`:

```hcl
# infrastructure/environments/dev/terraform.tfvars

aws_region  = "us-east-1"               # ← Your AWS region
cost_center = "engineering"             # ← Your cost center tag
github_org  = "your-github-org"         # ← Your GitHub org name (for OIDC CI/CD role)
github_repo = "enterprise-data-lake"    # ← Your GitHub repo name
alert_email = "ops-team@yourcompany.com" # ← Ops team email for CloudWatch alarms

# Set AFTER running make lambda-deploy (Step 3.1 in Phase 3 below)
lambda_package_s3_bucket   = "edl-terraform-state-087972550871"
lambda_package_s3_key      = "lambda/extraction-pipeline.zip"
lambda_package_source_hash = ""   # Fill in from the deployed artifact's hash (see Step 3.1's warning
                                   # against running lambda-package/lambda-upload as separate commands)
```

For higher environments, start from the new templates:
- `infrastructure/environments/staging/terraform.tfvars.example`
- `infrastructure/environments/prod/terraform.tfvars.example`

> **Note:** `terraform.tfvars` is committed to source control. Never put passwords, tokens, or secrets here. Secrets go in Secrets Manager (Step 6).

### Step 2.2 — Initialize Terraform

```bash
cd infrastructure/environments/dev
terraform init
# Expected output: "Terraform has been successfully initialized!"
```

### Step 2.3 — Review the plan

```bash
terraform plan -out=tfplan
# Review the output. Confirm all expected resources are listed.
# Check that no existing resources will be destroyed unexpectedly.
```

Key resources Terraform will create:

| Resource type | Count | Notes |
|---|---|---|
| `aws_kms_key` | 4 | storage, database, secrets, logs |
| `aws_s3_bucket` | 6 | raw, curated, analytics, schema-snapshots, governance, mapping/artifacts |
| `aws_dynamodb_table` | 5 | watermark-repository, run-audit-log, entity-extraction-config, entity-type-registry, source-onboarding-registry |
| `aws_iam_role` | 13 | one runtime role per Lambda (extraction, transformation, entity-resolution, analytics-publisher, control-plane, credential-expiry-notifier) plus transformation-job, orchestration-step-functions, eventbridge-scheduler, cicd-deployment, pipeline-trigger, dlq-processor, credential-expiry-scheduler — see `infrastructure/modules/iam/main.tf` |
| `aws_vpc` + subnets | 1 VPC | private subnets, VPC endpoints |
| `aws_secretsmanager_secret` | 3 | one per source system (values set later) |
| `aws_sqs_queue` | 3 | extraction DLQ + retry queue + pipeline trigger FIFO queue |
| `aws_cloudwatch_*` | various | log groups, alarms, metric filters |
| `aws_scheduler_schedule_group` | 1 | EventBridge schedule group |
| `aws_sfn_state_machine` | 1 | extraction orchestration workflow |
| `aws_lambda_function` | 1 | extraction pipeline handler |

### Step 2.4 — Apply

```bash
terraform apply tfplan
# Type 'yes' when prompted.
# First apply takes approximately 5-10 minutes.
```

### Step 2.5 — Save outputs

```bash
terraform output -json > /tmp/dev-outputs.json
cat /tmp/dev-outputs.json
```

You will need these output values in later steps. Key outputs:

| Output name | Used for |
|---|---|
| `raw_bucket_name` | Lambda env var, seed script |
| `curated_bucket_name` | Transformation Lambda env var |
| `analytics_bucket_name` | Analytics publisher env var |
| `mapping_bucket_name` | Field mapping upload location |
| `governance_bucket_name` | Lineage and retention records |
| `entity_config_table_name` | Seed script target table |
| `watermark_table_name` | Watermark repository |
| `extraction_lambda_arn` | Manual trigger |
| `step_functions_state_machine_arn` | Invoked by Pipeline Trigger Lambda (not EventBridge directly) |
| `pipeline_trigger_queue_url` | EventBridge schedule target; also used by `seed_schedules.py` |
| `salesforce_secret_arn` | Secret to populate |
| `netsuite_secret_arn` | Secret to populate |
| `mysql_rds_secret_arn` | Secret to populate |

---

## 6. Phase 3 — Application Deployment (Lambda)

> **Prerequisite:** Phase 2 Terraform apply must be complete so the S3 artifacts bucket exists for the Lambda zip upload.

Eight Lambda functions are deployed, all from the same zip (different handler entry points) — you build and upload the zip once, then a single `terraform apply` creates/updates all of them.

| Lambda | Handler | Purpose |
|---|---|---|
| `EdlExtractionPipeline` | `connector_runtime.extraction_pipeline_handler.lambda_handler` | Stages 1–10: extract raw data from a source into the raw layer |
| `EdlTransformationPipeline` | `transformation.transformation_pipeline_handler.lambda_handler` | Stage 11: raw → curated |
| `EdlEntityResolutionPipeline` | `entity_resolution.entity_resolution_pipeline_handler.lambda_handler` | Stage 12–13: cross-source matching + golden records |
| `EdlAnalyticsLayerPublisher` | `analytics_publisher.analytics_publisher_handler.lambda_handler` | Stage 14: curated/golden → analytics layer |
| `EdlControlPlane` | `connector_runtime.api.control_plane_handler.lambda_handler` | Multi-tenant control-plane API behind API Gateway + Cognito: tenant provisioning, entity registration/listing, pipeline trigger, run status |
| `EdlPipelineTrigger` | `orchestration.pipeline_trigger.pipeline_trigger_handler.lambda_handler` | Rate-limited SQS FIFO consumer that starts Step Functions executions — both `scripts/trigger_extraction.py` and the control-plane API's pipeline-trigger route funnel through this queue |
| `EdlDlqProcessor` | `orchestration.dlq_processor.dlq_processor_handler.lambda_handler` | Drains the extraction-failure DLQ: writes an audit record, sends an SNS alert, optionally auto-replays |
| `EdlCredentialExpiryNotifier` | `connector_runtime.credential_rotation.credential_expiry_notifier_handler.lambda_handler` | Not a pipeline stage — daily EventBridge Scheduler check of source-credential secret age; SNS alert when rotation is overdue (see [Section 11.H](#h-cloudwatch-alarms--alert-thresholds)) |

> **There is no Lambda for the "serving store" stage.** A stage loading analytics → MySQL RDS was planned, and `transformation/serving_store_loader.py` exists as business logic, but no Terraform module builds or deploys it — there is no `EdlServingStoreLoader` function to package, upload, or verify. The orchestration module's `serving_store_loader_lambda_arn` variable defaults to `""`, and whenever it's empty (i.e. always, today) the Step Functions state machine substitutes a `Pass` state for that stage (see `infrastructure/modules/orchestration/main.tf`'s `load_serving_store_state` local) — the pipeline completes successfully after analytics publication. Do not add this Lambda to deploy/verify checklists until it's actually built and wired.

See [Section 11.J — Control Plane API](#j-control-plane-api-cognito--api-gateway) below for `EdlControlPlane` detail.

### Step 3.1 — Build, upload, and deploy the Lambda package

> **Do not run `make lambda-package` and `make lambda-upload` as two separate commands.**
> `pyproject.toml` pins dependency *ranges*, not exact versions, so the build is not
> byte-reproducible — two invocations with no source change can still produce different
> SHA-256 hashes. Because `lambda-upload` depends on `lambda-package` in the `Makefile`,
> running them separately (or copying a hash from a standalone `make lambda-package` run
> into `terraform.tfvars`) can upload a *different* artifact than the one whose hash you
> copied. Always use the single convenience target:

```bash
cd /path/to/enterprise-data-lake  # repo root
source .venv/bin/activate

make lambda-deploy
# Builds dist/extraction-pipeline.zip once, uploads that exact artifact to
# s3://edl-terraform-state-087972550871/lambda/extraction-pipeline.zip, computes its
# hash from the uploaded file, then runs a targeted terraform apply that updates the
# code on all eight Lambda functions (extraction, transformation, entity-resolution,
# analytics-publisher, control-plane, pipeline-trigger, dlq-processor,
# credential-expiry-notifier) in one pass.
```

If you need the hash for `terraform.tfvars` (e.g. for a full, non-targeted
`terraform apply` elsewhere in this guide), read it back from the uploaded artifact —
never from a separate, later `make lambda-package` run:

```bash
openssl dgst -sha256 -binary dist/extraction-pipeline.zip | openssl base64
```

```hcl
lambda_package_source_hash = "abc123==..."   # ← paste that hash here
```

> Note: `terraform apply` fails if `extraction_pipeline_lambda_arn` hasn't been set in
> `terraform.tfvars` yet (needed by the orchestration module) — see Step 3.4.
> `serving_store_loader_lambda_arn` does not need to be set; leave it at its default `""`.

### Step 3.4 — Collect the extraction Lambda's ARN

The transformation, entity-resolution, and analytics-publisher ARNs are wired into the orchestration module automatically (module outputs) — nothing to collect for those. The **extraction Lambda is the only one you still fetch and paste manually**, because the orchestration module takes it as a plain variable rather than a module reference:

```bash
cd infrastructure/environments/dev

# Extraction Lambda (created by lambda_pipeline module)
terraform output extraction_lambda_arn
```

### Step 3.5 — Verify all deployed Lambdas

```bash
for fn in EdlExtractionPipeline EdlTransformationPipeline EdlEntityResolutionPipeline EdlAnalyticsLayerPublisher EdlCredentialExpiryNotifier; do
  aws lambda get-function \
    --function-name "${fn}" \
    --region us-east-1 \
    --query "Configuration.[FunctionName,State,LastModified]" \
    --output table
done
```

All five should show `State: Active`. (There is no `EdlServingStoreLoader` to check — see the note above.)

---

## 7. Phase 4 — Automatic Pipeline Configuration (Step Functions)

This phase wires the pipeline Lambda functions into the Step Functions state machine that runs the full end-to-end pipeline automatically. Transformation, entity-resolution, and analytics-publisher are already wired via Terraform module outputs (Phase 3) — the only ARN this phase needs from you is extraction's.

### Step 4.1 — Add the extraction Lambda ARN to terraform.tfvars

Add the ARN collected in Step 3.4 to `infrastructure/environments/dev/terraform.tfvars`. Leave `serving_store_loader_lambda_arn` unset (default `""`) — no Terraform module builds that Lambda today, so the orchestration module substitutes a `Pass` state for that stage:

```hcl
# infrastructure/environments/dev/terraform.tfvars

# Extraction Lambda ARN — set after running Phase 3.
# transformation_pipeline_lambda_arn, entity_resolution_lambda_arn, and
# analytics_publisher_lambda_arn are NOT set here — they come from the
# corresponding Terraform module outputs automatically (see dev/main.tf).
extraction_pipeline_lambda_arn  = "arn:aws:lambda:us-east-1:123456789012:function:EdlExtractionPipeline"
# serving_store_loader_lambda_arn intentionally left unset — Lambda not yet built.
```

### Step 4.2 — Apply to create the state machine

```bash
cd infrastructure/environments/dev
terraform apply
```

Terraform creates a Standard Workflow (dev uses Express for cost savings; staging/prod use Standard for execution history and >5min timeout support) with this branching logic:

```
Extraction
  ├─ transformation_blocked=true  → STOP (breaking schema drift — alert fired)
  └─ transformation_blocked=false → Transformation
                                      ├─ is_publication_blocked=true  → STOP (quality gate — alert fired)
                                      └─ is_publication_blocked=false → EntityResolution
                                                                           → AnalyticsPublish
                                                                               → ServingStoreLoad
                                                                                   → COMPLETE
```

> `ServingStoreLoad` is a real state in the state machine, but today it is always a `Pass` state (not a Lambda invocation) because `serving_store_loader_lambda_arn` is unset — see the note in Step 3 above. It passes through immediately to `COMPLETE`.

### Step 4.3 — Verify state machine created

```bash
aws stepfunctions describe-state-machine \
  --state-machine-arn "$(cd infrastructure/environments/dev && terraform output state_machine_arn)" \
  --query "[name,status,type]" \
  --output table
```

Expected output:
```
-----------------------------------------------------------
|              DescribeStateMachine                       |
+-----------------------------------------------+---------+
|  EdlExtractionPipeline                        | ACTIVE  |
|  STANDARD                                     |         |
-----------------------------------------------------------
```

### Step 4.4 — Create extraction schedules per entity

Each entity needs an EventBridge schedule targeting the **SQS FIFO pipeline trigger queue** (not Step Functions directly). The trigger queue absorbs simultaneous schedule fires — at 80–100 entities at launch all crons could fire within the same minute — and drains them into Step Functions at a controlled rate via the Pipeline Trigger Lambda.

Schedules are **data** — managed by `ExtractionScheduleClient` at runtime, not by Terraform.

`seed_schedules.py` reads every active entity from DynamoDB that has `schedule_cron` set and `schedule_enabled=True`, then creates or updates the corresponding EventBridge Scheduler schedules in one pass. The schedule target is the SQS trigger queue ARN read from Terraform output. **This must be run after every `terraform apply` and after any `seed_entity_config.py` run.**

```bash
# Preview what will be created without making AWS API calls:
python scripts/seed_schedules.py --environment dev --dry-run

# Create / update all schedules:
python scripts/seed_schedules.py --environment dev

# Or via Makefile:
make seed-schedules
```

> **Note:** Terraform creates the schedule *group* (`EdlExtractionSchedules`) and the SQS FIFO trigger queue. The individual cron triggers inside the group are created entirely by `seed_schedules.py`. If you skip this step, the group is empty and the pipeline never runs automatically.

### Step 4.5 — Test the full pipeline with a manual trigger

Before waiting for the schedule, trigger one run manually to verify the end-to-end flow:

```bash
python scripts/trigger_extraction.py \
  --source-id salesforce \
  --entity-id salesforce-account \
  --environment dev \
  --param object_name=Account
```

Watch execution in the AWS Console or via CLI:

```bash
# Get the most recent execution ARN
MACHINE_ARN=$(cd infrastructure/environments/dev && terraform output -raw state_machine_arn)

aws stepfunctions list-executions \
  --state-machine-arn "${MACHINE_ARN}" \
  --max-results 1 \
  --query "executions[0].executionArn" \
  --output text | xargs -I{} \
  aws stepfunctions describe-execution --execution-arn {}
```

### Step 4.6 — Understand the pipeline outputs at each stage

After a successful run, verify each stage's S3 output:

```bash
RUN_ID="run-20260616-..."  # from execution output

# Stage A — Raw Parquet written (dev bucket shown; substitute the bucket for your account)
aws s3 ls s3://edl-raw-087972550871/salesforce/salesforce-account/ --recursive

# Stage B — Curated Parquet written
aws s3 ls s3://edl-curated-087972550871/curated/customer/salesforce-account/ --recursive

# Stage B — Quality report (is_publication_blocked must be false)
aws s3 cp s3://edl-curated-087972550871/quality-reports/salesforce/salesforce-account/${RUN_ID}/quality-report.json -

# Stage C/D — Golden records and analytics
aws s3 ls s3://edl-analytics-087972550871/canonical/ --recursive
aws s3 ls s3://edl-analytics-087972550871/ --recursive
```

---

## 8. Phase 5 — Data Configuration (DynamoDB Seeds + Secrets)

### Step 5.1 — Populate source credentials in Secrets Manager

This step stores actual credentials. **Do this from a secure workstation only.** Never commit credential values to git.

**Salesforce credentials:**

```bash
aws secretsmanager put-secret-value \
  --secret-id "edl/sources/salesforce/credentials" \
  --region us-east-1 \
  --secret-string '{
    "client_id":     "YOUR_SALESFORCE_CONNECTED_APP_CLIENT_ID",
    "client_secret": "YOUR_SALESFORCE_CONNECTED_APP_CLIENT_SECRET",
    "instance_url":  "https://yourcompany.my.salesforce.com"
  }'
```

**NetSuite credentials:**

```bash
aws secretsmanager put-secret-value \
  --secret-id "edl/sources/netsuite/credentials" \
  --region us-east-1 \
  --secret-string '{
    "account_id":    "YOUR_NETSUITE_ACCOUNT_ID",
    "consumer_key":  "YOUR_CONSUMER_KEY",
    "consumer_secret": "YOUR_CONSUMER_SECRET",
    "token_id":      "YOUR_TOKEN_ID",
    "token_secret":  "YOUR_TOKEN_SECRET"
  }'
```

**MySQL RDS credentials:**

```bash
aws secretsmanager put-secret-value \
  --secret-id "edl/sources/mysql-rds/credentials" \
  --region us-east-1 \
  --secret-string '{
    "host":     "your-rds-endpoint.us-east-1.rds.amazonaws.com",
    "port":     3306,
    "database": "your_database_name",
    "username": "edl_readonly",
    "password": "YOUR_READONLY_PASSWORD"
  }'
```

> **Security note:** The `EdlExtractionServiceRole` IAM role created by Terraform has `GetSecretValue` permission on these exact secret ARNs only. No other role can read these credentials.

**Sage Intacct credentials:**

```bash
aws secretsmanager put-secret-value \
  --secret-id "edl/sources/sage/intacct/credentials" \
  --region us-east-1 \
  --secret-string '{
    "token_url":   "https://api.intacct.com/ia/api/v1/auth/oauth2/token",
    "client_id":  "YOUR_INTACCT_CLIENT_ID",
    "client_secret": "YOUR_INTACCT_CLIENT_SECRET",
    "base_url":   "https://api.intacct.com/ia/api/v1",
    "company_id": "YOUR_INTACCT_COMPANY_ID"
  }'
```

**Sage X3 credentials:**

```bash
aws secretsmanager put-secret-value \
  --secret-id "edl/sources/sage/x3/credentials" \
  --region us-east-1 \
  --secret-string '{
    "token_url":   "https://YOUR_X3_SERVER/auth/realms/sage/protocol/openid-connect/token",
    "client_id":  "YOUR_X3_CLIENT_ID",
    "client_secret": "YOUR_X3_CLIENT_SECRET",
    "base_url":   "https://YOUR_X3_SERVER/api",
    "folder":     "YOUR_X3_COMPANY_FOLDER"
  }'
```

### Step 5.2 — Seed entity configuration records into DynamoDB

```bash
python scripts/seed_entity_config.py \
  --environment dev \
  --region us-east-1
```

This writes the default entity configuration records for `salesforce-account`, `salesforce-contact`,
`salesforce-opportunity`, `salesforce-contract`, `netsuite-customer` (disabled), `mysql-rds-contracts`,
`mysql-rds-contractterms`, `sage-intacct-customer`, `sage-intacct-vendor`, `sage-intacct-arinvoice`,
`sage-intacct-apbill`, `sage-x3-customer`, and `sage-x3-supplier` (schedule disabled) — 13 records
total. All records are idempotent (safe to run multiple times).

To add a new entity, edit `scripts/seed_entity_config.py` and add a record to the list returned by
`_build_records()`, then re-run the script. No Terraform changes needed.

**Entity configuration fields explained:**

```python
{
    "source_id":               "salesforce",           # stable source identifier
    "entity_id":               "salesforce-account",   # stable entity identifier
    "config_version":          "1.1.0",                # semantic version — bump when changing load_type or merge fields
    "load_type":               "incremental",           # "full" or "incremental"
    "watermark_field":         "SystemModstamp",        # source timestamp field for delta sync (required for incremental)
    "extraction_window_days":  1,                       # max days per extraction run
    "watermark_overlap_hours": 1,                       # overlap to catch late-arriving records
    "field_mode":              "all",                   # "all", "standard", "custom", "includeOnly"
    "include_fields":          [],                      # only used when field_mode = "includeOnly"
    "exclude_fields":          ["IsDeleted"],           # always excluded regardless of field_mode
    "output_format":           "parquet",               # always parquet
    # ── Incremental merge (SCD Type 1) ──────────────────────────────────────
    # Set primary_key_field to enable SCD Type 1 merge for incremental entities.
    # The curated layer will always hold the FULL current state, not just the delta.
    # This ensures entity resolution and analytics see complete data on every run.
    # Leave as None for full-load entities (no merge needed — full extract every run).
    "primary_key_field":       "account_id",           # canonical PK field name (flat, no dots); None = append-only
    # soft_delete_field controls what happens when a source record carries a deletion flag:
    #   None (default / tombstone)  → deleted records are KEPT with their flag (is_deleted=True)
    #                                 BI queries filter WHERE is_deleted = false
    #   "is_deleted"                → deleted records are physically REMOVED from the curated snapshot
    "soft_delete_field":       None,                   # canonical delete-flag field name; None = tombstone pattern
    "active":                  True                    # False = skip this entity
}
```

**Current entity extraction modes (dev), per `scripts/seed_entity_config.py`:**

| Entity | load_type | watermark_field | primary_key_field | Notes |
|---|---|---|---|---|
| `salesforce-account` | `incremental` | `SystemModstamp` | `account_id` | tombstone soft-delete |
| `salesforce-contact` | `incremental` | `SystemModstamp` | `contact_id` | tombstone soft-delete |
| `salesforce-opportunity` | `incremental` | `SystemModstamp` | `opportunity_id` | tombstone soft-delete |
| `salesforce-contract` | `incremental` | `SystemModstamp` | `sales_contract_id` | tombstone soft-delete |
| `netsuite-customer` | `incremental` | `lastModifiedDate` | not set | `active: False` — disabled, not exercised end-to-end |
| `mysql-rds-contracts` | `incremental` | `ModifiedOn` | `contract_id` | tombstone (`is_deleted=True` persists) |
| `mysql-rds-contractterms` | `incremental` | `ModifiedOn` | `contract_term_id` | tombstone soft-delete |
| `sage-intacct-customer` / `-vendor` / `-arinvoice` / `-apbill` | `incremental` | `auditInfo.modifiedAt` | not set | append-only, no SCD merge configured yet |
| `sage-x3-customer` | `incremental` | `MODDAT_0` | not set | append-only; schedule enabled |
| `sage-x3-supplier` | `incremental` | `MODDAT_0` | not set | append-only; `schedule_enabled: False` |

### Step 5.3 — Create EventBridge extraction schedules

Run `seed_schedules.py` to create all schedules in one pass (reads from DynamoDB — no per-entity CLI calls needed):

```bash
python scripts/seed_schedules.py --environment dev
# or: make seed-schedules
```

To add a schedule for a new entity: set `schedule_cron` and `schedule_enabled=True` in `seed_entity_config.py`, re-run `seed_entity_config.py`, then re-run `seed_schedules.py`.

---

## 9. Phase 6 — Field Mapping Configuration

Field mapping tells the transformation pipeline how to rename source fields to canonical business names. **If you don't provide a mapping, the pipeline uses identity mapping (field names passed through unchanged).**

### Where field mappings are stored

Field mappings are **JSON files stored in S3**:

```
s3://edl-mapping-config-<ACCOUNT_ID>/    # dev account ID shown elsewhere as 087972550871; staging/prod use their own account ID once bootstrapped
└── field-mappings/
    └── {source_id}/
        └── {entity_id}/
            ├── 1.0.0.json        ← versioned rule set
            ├── 1.1.0.json        ← updated rule set
            └── latest.json       ← pointer: {"mapping_version": "1.1.0"}
```

The platform automatically loads `latest.json` to find the current active version. Previous versions are retained for replay/rollback.

### Field mapping JSON format

Create a file called `salesforce-account-mapping.json`:

```json
{
  "source_id": "salesforce",
  "entity_id": "salesforce-account",
  "mapping_version": "1.0.0",
  "rules": [
    {
      "source_fields": ["Id"],
      "canonical_field": "account_id",
      "transformation": "rename",
      "transformation_params": {},
      "missing_field_behavior": "raise_error"
    },
    {
      "source_fields": ["Name"],
      "canonical_field": "account_name",
      "transformation": "rename",
      "transformation_params": {},
      "missing_field_behavior": "raise_error"
    },
    {
      "source_fields": ["BillingCity"],
      "canonical_field": "billing_city",
      "transformation": "rename",
      "transformation_params": {},
      "missing_field_behavior": "drop_field"
    },
    {
      "source_fields": ["BillingState"],
      "canonical_field": "billing_state",
      "transformation": "rename",
      "transformation_params": {},
      "missing_field_behavior": "drop_field"
    },
    {
      "source_fields": ["AnnualRevenue"],
      "canonical_field": "annual_revenue_usd",
      "transformation": "cast",
      "transformation_params": {"type": "decimal"},
      "missing_field_behavior": "use_default",
      "default_value": "0"
    },
    {
      "source_fields": ["CreatedDate"],
      "canonical_field": "created_date",
      "transformation": "date_format",
      "transformation_params": {
        "input_format": "%Y-%m-%dT%H:%M:%S.%f%z",
        "output_format": "%Y-%m-%d"
      },
      "missing_field_behavior": "drop_field"
    },
    {
      "source_fields": ["FirstName", "LastName"],
      "canonical_field": "full_name",
      "transformation": "concat",
      "transformation_params": {"separator": " "},
      "missing_field_behavior": "drop_field"
    }
  ]
}
```

### Transformation types reference

| `transformation` | What it does | Required `transformation_params` |
|---|---|---|
| `rename` | Copy value from one source field to canonical field | none |
| `concat` | Join multiple source fields with a separator | `separator` (default: `" "`) |
| `date_format` | Parse and reformat a date/datetime string | `input_format`, `output_format` (strftime patterns) |
| `cast` | Convert value to a different type | `type`: `string`, `integer`, `decimal`, `boolean` |
| `mask` | Mask field value (last N chars visible) | `visible_chars` (default: `4`) |

### `missing_field_behavior` reference

| Value | Effect when source field is absent or null |
|---|---|
| `drop_field` | Skip this field — canonical record produced without it |
| `raise_error` | Discard the entire record — increments `mapping_failures` counter |
| `use_default` | Use the value in `default_value` field |

### How to upload a field mapping

**Option A — Python script (recommended):**

```bash
python - <<'EOF'
import boto3, json

s3 = boto3.client("s3", region_name="us-east-1")
BUCKET = "edl-mapping-config-087972550871"   # ← from terraform output mapping_bucket_name

with open("salesforce-account-mapping.json") as f:
    rule_set = json.load(f)

# Upload versioned file
key = f"field-mappings/{rule_set['source_id']}/{rule_set['entity_id']}/{rule_set['mapping_version']}.json"
s3.put_object(Bucket=BUCKET, Key=key, Body=json.dumps(rule_set, indent=2).encode(), ContentType="application/json")

# Update latest pointer
pointer_key = f"field-mappings/{rule_set['source_id']}/{rule_set['entity_id']}/latest.json"
s3.put_object(Bucket=BUCKET, Key=pointer_key,
              Body=json.dumps({"mapping_version": rule_set["mapping_version"]}).encode(),
              ContentType="application/json")

print(f"Published: {key}")
EOF
```

**Option B — AWS CLI:**

```bash
BUCKET=edl-mapping-config-087972550871
SOURCE_ID=salesforce
ENTITY_ID=salesforce-account
VERSION=1.0.0

# Upload the rule set
aws s3 cp salesforce-account-mapping.json \
  s3://${BUCKET}/field-mappings/${SOURCE_ID}/${ENTITY_ID}/${VERSION}.json \
  --content-type application/json

# Update the latest pointer
echo '{"mapping_version": "'"${VERSION}"'"}' | \
  aws s3 cp - \
    s3://${BUCKET}/field-mappings/${SOURCE_ID}/${ENTITY_ID}/latest.json \
    --content-type application/json
```

**Option C — Python `FieldMappingRegistryClient` (programmatic):**

```python
from transformation.field_mapping.field_mapping_registry import (
    FieldMappingRegistryClient, FieldMappingRule, FieldMappingRuleSet,
    MappingTransformation, MissingFieldBehavior
)

client = FieldMappingRegistryClient(
    s3_bucket="edl-mapping-config-087972550871",
    region_name="us-east-1"
)

rule_set = FieldMappingRuleSet(
    source_id="salesforce",
    entity_id="salesforce-account",
    mapping_version="1.0.0",
    rules=(
        FieldMappingRule(
            source_fields=("Id",),
            canonical_field="account_id",
            transformation=MappingTransformation.RENAME,
            transformation_params={},
            missing_field_behavior=MissingFieldBehavior.RAISE_ERROR,
        ),
        FieldMappingRule(
            source_fields=("Name",),
            canonical_field="account_name",
            transformation=MappingTransformation.RENAME,
            transformation_params={},
            missing_field_behavior=MissingFieldBehavior.RAISE_ERROR,
        ),
    ),
)

key = client.publish_rule_set(rule_set)
print(f"Published to: {key}")
```

### Updating a field mapping

To update, create a new JSON file with an incremented `mapping_version` (e.g. `"1.1.0"`) and upload it. The `latest.json` pointer is updated automatically. The next transformation run picks up the new version. Old versions remain in S3 for replay.

---

## 10. Phase 7 — Entity Resolution Config

Entity resolution match rules and survivorship policies are stored as **versioned JSON config files in S3** — analogous to field mappings but for entity identity and canonical output schema.

### Where entity resolution configs are stored

```
s3://edl-curated-<ACCOUNT_ID>/    # dev account ID shown elsewhere as 087972550871; staging/prod use their own account ID once bootstrapped
└── entity-resolution/
    └── {entity_type}/
        ├── match_rules_v1.json     ← match rules (blocking + deterministic/probabilistic rules)
        ├── survivorship_v1.json    ← survivorship policy + output_fields schema
        └── latest.json             ← {"match_rules_version": "v1", "survivorship_version": "v1"}
```

The source files live in Git under `config/entity_resolution/`. The `ResolutionConfigRegistry` loads them from S3 at runtime. Every entity resolution Lambda invocation loads config fresh (with in-process caching for warm Lambda instances).

### How to publish entity resolution configs

```bash
# Publish all entity resolution configs from config/entity_resolution/ to dev S3
python scripts/seed_entity_resolution_configs.py --environment dev --region us-east-1

# Dry-run first (prints what would be published, no S3 writes)
python scripts/seed_entity_resolution_configs.py --environment dev --region us-east-1 --dry-run

# Publish a single entity type
python scripts/seed_entity_resolution_configs.py --environment dev --entity-type company
```

### Currently defined entity types

`entity_resolution/entity_type_registry.py`'s `ENTITY_TYPE_SOURCES` dict is the source of truth
for which (source_id, entity_id) pairs feed each entity type — the table below mirrors it as of
2026-07-09:

| Entity type | Git config path | Sources merged | Output prefix |
|---|---|---|---|
| `company` | `config/entity_resolution/company/` | Salesforce Account + NetSuite Customer + Sage Intacct Customer + Sage X3 Customer (each skipped gracefully if absent) | `canonical/company/` |
| `person` | `config/entity_resolution/person/` | Salesforce Contact | `canonical/person/` |
| `contract` | `config/entity_resolution/contract/` | MySQL RDS Contracts | `canonical/contract/` |
| `supplier` | `config/entity_resolution/supplier/` | Sage Intacct Vendor + Sage X3 Supplier | `canonical/supplier/` |
| `ar_invoice` | `config/entity_resolution/ar_invoice/` | Sage Intacct AR Invoice | `canonical/ar_invoice/` |
| `ap_bill` | `config/entity_resolution/ap_bill/` | Sage Intacct AP Bill | `canonical/ap_bill/` |
| `opportunity` | `config/entity_resolution/opportunity/` | Salesforce Opportunity | `canonical/opportunity/` |
| `sales-contract` | `config/entity_resolution/sales-contract/` | Salesforce Contract | `canonical/sales-contract/` |
| `contract-term` | `config/entity_resolution/contract-term/` | MySQL RDS ContractTerms | `canonical/contract-term/` |

### Adding a new entity type

No code change is required. Add new JSON files and publish:

```bash
# 1. Create config directory
mkdir -p config/entity_resolution/order

# 2. Create match_rules_v1.json and survivorship_v1.json in that directory
# (following the schema in docs/PIPELINE_FLOW.md §6)

# 3. Publish to S3
python scripts/seed_entity_resolution_configs.py --environment dev --entity-type order
```

### Updating an existing entity type

```bash
# 1. Edit config/entity_resolution/company/match_rules_v2.json (bump rule_set_version to "v2")
# 2. Publish new version — the script uploads every match_rules_*/survivorship_*
#    file present in config/entity_resolution/company/ and points latest.json at
#    whichever has the highest version number (see _resolve_latest_version() in
#    scripts/seed_entity_resolution_configs.py) — there is no --pin-version flag.
python scripts/seed_entity_resolution_configs.py --environment dev --entity-type company

# Rollback: the script always recomputes latest.json as the MAX version found
# locally, so re-running it will NOT pin to an older version. To roll back,
# overwrite the pointer directly in S3:
echo '{"match_rules_version": "v1", "survivorship_version": "v1"}' | \
  aws s3 cp - s3://edl-curated-087972550871/entity-resolution/company/latest.json \
  --content-type application/json
# Note: the next `seed_entity_resolution_configs.py` run for this entity type will
# recompute latest.json back to the highest local version unless the newer
# version's files are also removed from config/entity_resolution/company/.
```

### Verify entity resolution configs published

```bash
# Check all configs exist in S3 (9 entity types defined in
# entity_resolution/entity_type_registry.py's ENTITY_TYPE_SOURCES)
for entity in company person contract supplier ar_invoice ap_bill opportunity sales-contract contract-term; do
  echo "--- ${entity} ---"
  aws s3 ls "s3://edl-curated-087972550871/entity-resolution/${entity}/"
done

# Inspect the latest pointer
aws s3 cp s3://edl-curated-087972550871/entity-resolution/company/latest.json -
# Expected: {"match_rules_version": "v1", "survivorship_version": "v1"}
```

---

## 11. All AWS Settings Reference — What to Set and Where

### A. `terraform.tfvars` — Infrastructure settings

File: `infrastructure/environments/{env}/terraform.tfvars`

Template files for promotion:
- `infrastructure/environments/staging/terraform.tfvars.example`
- `infrastructure/environments/prod/terraform.tfvars.example`

| Variable | What it is | Example |
|---|---|---|
| `aws_region` | AWS region for all resources | `"us-east-1"` |
| `cost_center` | Tag applied to all resources for cost allocation | `"data-platform"` |
| `github_org` | GitHub org for CI/CD OIDC trust (GitHub Actions → AWS) | `"your-github-org"` |
| `github_repo` | GitHub repo name for OIDC trust | `"enterprise-data-lake"` |
| `alert_email` | Email for CloudWatch alarm SNS notifications | `"ops@yourcompany.com"` |
| `lambda_package_s3_bucket` | S3 bucket where Lambda zip is uploaded | `"edl-terraform-state-087972550871"` (dev; account-ID suffix differs per environment/account) |
| `lambda_package_s3_key` | S3 key of the Lambda zip | `"lambda/extraction-pipeline.zip"` |
| `lambda_package_source_hash` | Base64 SHA-256 of zip (triggers Lambda update) | written automatically by `make lambda-deploy` |

### B. `backend.tf` — Terraform remote state settings

File: `infrastructure/environments/{env}/backend.tf`

| Setting | What it is | Example |
|---|---|---|
| `bucket` | S3 bucket for Terraform state | `"edl-terraform-state-087972550871"` (dev; account-ID suffix differs per environment/account) |
| `key` | S3 object key for the state file | `"environments/dev/terraform.tfstate"` |
| `region` | State bucket region | `"us-east-1"` |
| `kms_key_id` | KMS alias for state encryption | `"alias/EdlTerraformState"` (same literal alias in every account) |
| `dynamodb_table` | DynamoDB table for state locking | `"EdlTerraformStateLock"` (same literal table name in every account) |

### C. AWS Secrets Manager — Source credentials

Secret path: `edl/sources/{source_id}/credentials` — same path in every environment/account now (the env segment was dropped)

| Source | Secret path | Fields in JSON |
|---|---|---|
| Salesforce | `edl/sources/salesforce/credentials` | `instance_url`, `client_id`, `client_secret` |
| NetSuite | `edl/sources/netsuite/credentials` | `account_id`, `consumer_key`, `consumer_secret`, `token_id`, `token_secret` |
| MySQL RDS | `edl/sources/mysql-rds/credentials` | `host`, `port`, `database`, `username`, `password` |

**How to set:** `aws secretsmanager put-secret-value` (see Step 5.1 above)  
**Who can read:** Only the `EdlExtractionServiceRole` IAM role (enforced by Secrets Manager resource policy)  
**Where configured:** `infrastructure/modules/secrets/main.tf` — the secret ARNs and resource policies are created by Terraform

### D. DynamoDB — Entity extraction configuration

Table: `EdlEntityExtractionConfig` — same name in every environment/account now

| Setting | How to set | Notes |
|---|---|---|
| Entity config records | `python scripts/seed_entity_config.py` | Idempotent; safe to re-run |
| New entity for existing source | Edit `scripts/seed_entity_config.py`, add record, re-run | No Terraform change |
| New source system | Add adapter code + seed record + register credential | Use the `/new-connector` slash command to scaffold the adapter pattern, or see `connector_runtime/CLAUDE.md` and [DEVELOPER_GUIDE.md](DEVELOPER_GUIDE.md)'s `connector_runtime/` row for the shared base classes (`SecretsManagerCredentialClient`, `RawLayerWriter`, `build_incremental_select()`) |

Key fields you will configure per entity: `load_type`, `watermark_field`, `field_mode`, `exclude_fields`, `extraction_window_days`.

### E. S3 — Field mapping configuration

Bucket: `edl-mapping-config-<ACCOUNT_ID>` (dev: `edl-mapping-config-087972550871`)  
Prefix: `field-mappings/{source_id}/{entity_id}/`

| File | Purpose | How to set |
|---|---|---|
| `{version}.json` | Versioned field mapping rule set | Upload via script or AWS CLI (see Section 7) |
| `latest.json` | Pointer to the current active version | Updated automatically when you publish a rule set |

### F. EventBridge Scheduler — Extraction schedules

Schedule group: `EdlExtractionSchedules` — same name in every environment/account now (created by Terraform)

| Setting | How to set | Example |
|---|---|---|
| Schedule expression | Set `schedule_cron` in `seed_entity_config.py`, then run `seed_schedules.py` | `"cron(0 2 * * ? *)"` |
| Time zone | Set `schedule_timezone` in entity config (default: `UTC`) | — |
| Target | Step Functions state machine ARN (wired automatically by `seed_schedules.py`) | — |
| Input payload | `{"source_id": "...", "entity_id": "...", "connector_params": {...}}` | Built from entity config |

**To update a schedule:** Edit `schedule_cron` in `seed_entity_config.py`, re-run `seed_entity_config.py`, then re-run `seed_schedules.py`.  
**To disable a schedule:** Set `schedule_enabled=False` in `seed_entity_config.py`, re-run both scripts.  
**No Terraform change needed** for schedule changes.

### G. Lambda environment variables

These are set by Terraform in `infrastructure/modules/lambda_pipeline/main.tf`. After Terraform apply, Lambda has:

The table below is the **extraction Lambda's** actual env var set, verified directly against
`infrastructure/modules/lambda_pipeline/main.tf`'s `environment { variables = { ... } }` block —
previous versions of this table used invented variable names that don't exist in code. Other
Lambdas (transformation, entity-resolution, analytics-publisher) have their own distinct env var
sets (e.g. `transformation_pipeline_handler.py` also reads `CURATED_S3_BUCKET` and
`FIELD_MAPPING_S3_BUCKET` — not `RAW_S3_BUCKET`) — don't assume every Lambda shares this exact
list; check the specific handler's `require_env(...)` calls if you need another function's set.

| Environment variable | Value (from Terraform outputs) | Purpose |
|---|---|---|
| `PLATFORM_ENVIRONMENT` | `dev` / `staging` / `prod` | Determines DynamoDB table names and S3 bucket names (not `ENVIRONMENT` — that name isn't read anywhere in code) |
| `RAW_S3_BUCKET` | `edl-raw-087972550871` | S3 raw layer bucket |
| `SCHEMA_SNAPSHOT_S3_BUCKET` | `edl-schema-snapshots-087972550871` | Schema snapshot bucket |
| `ENTITY_CONFIG_TABLE` | `EdlEntityExtractionConfig` | DynamoDB config table (note the `Edl` prefix — an older, incorrect form without it circulates in some docs/examples) |
| `WATERMARK_TABLE` | `EdlWatermarkRepository` | DynamoDB watermark table |
| `AUDIT_LOG_TABLE` | `EdlRunAuditLog` | DynamoDB audit table (not `AUDIT_TABLE` — that name isn't read anywhere in code) |

AWS Lambda provides `AWS_REGION` automatically as a reserved runtime env var — Terraform doesn't
need to (and doesn't) set it explicitly. A previous version of this table incorrectly listed
`AWS_DEFAULT_REGION`, `RAW_BUCKET`, `SCHEMA_SNAPSHOTS_BUCKET`, `MAPPING_BUCKET`,
`GOVERNANCE_BUCKET`, `DLQ_URL`, and `LOG_LEVEL` — none of these exact names are read via
`require_env(...)` anywhere in the handler code; `FIELD_MAPPING_S3_BUCKET` is the real name for
what was called `MAPPING_BUCKET`. If you need a governance/DLQ/log-level env var confirmed for a
specific Lambda, check that handler's own `require_env(...)` calls rather than trusting this table.

You do **not** need to set these manually — Terraform configures them. If you need to change a value, update the Terraform variable and re-apply.

### H. CloudWatch Alarms — Alert thresholds

Created by Terraform in `infrastructure/modules/observability/`. Key alarms:

| Alarm name | Trigger | Action |
|---|---|---|
| `EdlExtractionFailureRate` | > 5% failure rate over 5 min | SNS → alert_email |
| `EdlDlqDepth` | DLQ has > 0 messages for > 4 hours | SNS → alert_email |
| `EdlWatermarkLag` | Lag > 26 hours (daily entity) | SNS → alert_email |
| `EdlBreakingDrift` | Breaking drift event detected | SNS → alert_email |

To change alert thresholds, edit `infrastructure/modules/observability/variables.tf` and run `terraform apply`.

> **Credential expiry alerts (separate from the alarms above):** `EdlCredentialExpiryNotifier` is a Lambda (not a CloudWatch alarm) that runs daily on an EventBridge Scheduler rule (`EdlCredentialExpiryCheck`, `rate(1 day)`, created by `infrastructure/modules/secrets/main.tf`). It checks the age of every source-credential secret and publishes directly to the same platform alerts SNS topic (`ALERT_SNS_TOPIC_ARN`) when a secret is approaching or past its rotation window (`ROTATION_WARNING_DAYS` / `SECRET_ROTATION_DAYS` env vars). This is the observability half of credential rotation — no connector actually auto-rotates credentials today, so this Lambda is what tells you a secret needs manual rotation.

### I. IAM Least Privilege — What Each Role Can Access

The platform enforces a **zero-trust, need-to-know** IAM model. Every role is created by Terraform and scoped to only the exact resources and actions it requires — no `Resource: "*"` and no `Action: "*"` permissions anywhere.

| IAM role | AWS services it can access | Explicit restrictions |
|---|---|---|
| `EdlExtractionServiceRole` | S3 (`PutObject` on `raw/` prefix only) · DynamoDB (`GetItem`/`PutItem` on config, watermark, audit tables) · Secrets Manager (`GetSecretValue` on `edl/sources/*` only) · CloudWatch Logs | Cannot write to curated or analytics buckets; cannot read Secrets Manager secrets from other environments |
| `EdlTransformationServiceRole` | S3 (`GetObject` on raw prefix; `PutObject` on curated prefix) · S3 (`GetObject`/`PutObject` on mapping-config bucket) · Glue (`CreateTable`, `UpdateTable` on the platform database only) · CloudWatch Logs | Cannot access raw layer for write; cannot read Secrets Manager |
| `EdlEntityResolutionRole` | S3 (`GetObject` on curated prefix; `GetObject` on entity-resolution config prefix; `PutObject` on analytics `canonical/` prefix) · CloudWatch Logs | Cannot read raw or mapping-config buckets; cannot access Secrets Manager |
| `EdlAnalyticsPublisherRole` | S3 (`GetObject` on curated prefix; `PutObject` on analytics `curated/` prefix) · Glue · CloudWatch Logs | Cannot write to canonical (entity-resolved) prefix |
| `EdlCredentialExpiryNotifierRole` | Secrets Manager (`DescribeSecret` on `edl/sources/*` to read rotation metadata) · SNS (`Publish` on the platform alerts topic) · CloudWatch Logs | Cannot read secret values, only metadata; cannot write to any S3 bucket |
| `EdlCicdDeploymentRole` | Terraform state S3 bucket · IAM (boundary-constrained role updates) · Lambda/ECS task deployments | Cannot access data buckets, Secrets Manager values, or DynamoDB data tables |

> **There is no `EdlServingStoreRole`.** No Lambda or Terraform module exists for the serving-store stage (see [Phase 3](#6-phase-3--application-deployment-lambda)), so no role was created for it.
>
> This table shows the roles most relevant to data-plane access; the module actually defines **13 roles** in `infrastructure/modules/iam/main.tf`: `extraction_runtime`, `transformation_runtime`, `entity_resolution_runtime`, `analytics_publisher_runtime`, `transformation_job`, `orchestration_step_functions`, `eventbridge_scheduler`, `cicd_deployment`, `pipeline_trigger`, `dlq_processor`, `credential_expiry_notifier`, `credential_expiry_scheduler`, `control_plane`.

> **Verification:** After `terraform apply`, confirm no role has wildcard permissions:
> ```bash
> # Check no policy has Resource: "*" with Action: "*"
> aws iam list-policies --scope Local --query "Policies[*].PolicyName" --output text | \
>   xargs -I{} aws iam get-policy-version \
>     --policy-arn "arn:aws:iam::$(aws sts get-caller-identity --query Account --output text):policy/{}" \
>     --version-id v1 --query "PolicyVersion.Document" | \
>   grep -c '"Resource": "\*"'
> # Expected: 0
> ```

### J. Control Plane API (Cognito + API Gateway)

`infrastructure/modules/control_plane/` is wired into every environment's `main.tf` (`module "control_plane" { source = "../../modules/control_plane" ... }`) and creates a multi-tenant HTTP API in front of the platform:

| Resource | What it is |
|---|---|
| `aws_cognito_user_pool.control_plane` | Cognito User Pool — issues JWTs for API callers |
| `aws_cognito_user_pool_client.control_plane` | App client used to obtain tokens |
| `aws_apigatewayv2_api` + routes | HTTP API, JWT-authorized, fronting the control-plane Lambda |
| `aws_lambda_function.control_plane` (`EdlControlPlane`) | Single Lambda dispatching all routes, handler `connector_runtime.api.control_plane_handler.lambda_handler` |

**Routes** (method + path, all JWT-authorized except tenant creation which only requires an authenticated caller):

| Route | Purpose |
|---|---|
| `POST /tenants` | Provision a new tenant (writes a `tenant_registry#meta` record to the entity-type-registry table) |
| `GET /tenants/{tenant_code}/entities` | List configured entities for a tenant |
| `POST /tenants/{tenant_code}/entities` | Register a new entity for a tenant |
| `POST /tenants/{tenant_code}/pipelines/trigger` | Enqueue an extraction run onto the same SQS FIFO pipeline-trigger queue used by scheduled runs |
| `GET /tenants/{tenant_code}/runs/{run_id}` | Look up a single run's status |
| `GET /tenants/{tenant_code}/runs` | List runs for a tenant |

**Tenant onboarding now goes through this API, not a manual script.** Seeding the first tenant means calling `POST /tenants` with a valid Cognito-issued token (any authenticated identity — there is no admin-scoped claim yet) rather than hand-writing a DynamoDB item. The control-plane Lambda does the conditional `put_item` (`ConditionExpression="attribute_not_exists(sk)"`) so retries are safe and duplicate tenant codes are rejected with a 409.

> **Status: code-complete, not yet verified against a live AWS deployment.** The handler defensively checks both `authorizer.claims` and `authorizer.jwt.claims` shapes for the JWT authorizer payload and fails closed (401) either way, but which shape API Gateway actually populates at HTTP API payload format 1.0 has not been exercised against a real deployment. Treat this module as needing a smoke test against a real Cognito user pool + API Gateway stage before relying on it in staging/prod, not as battle-tested infrastructure.

---

## 12. Promoting to Staging and Production

The deployment process for staging and production is the same as dev — the environment directory changes, and a few additional steps apply because the orchestration module requires the extraction Lambda's ARN before Terraform can fully apply (transformation/entity-resolution/analytics-publisher ARNs wire automatically; the state machine's 5th state, `ServingStoreLoad`, runs as a `Pass` state since that Lambda isn't built).

### Step 11.1 — Complete AWS Prerequisites for the new environment

Before Terraform can run for staging or prod, repeat **Section 2** for the new environment:

- [ ] Terraform state S3 bucket (`edl-terraform-state-STAGING_ACCOUNT_ID` or `edl-terraform-state-PROD_ACCOUNT_ID` — substitute the real AWS account ID once that environment is bootstrapped)
- [ ] DynamoDB lock table (`EdlTerraformStateLock` — same literal name as dev, since each account gets its own instance of a fixed-name table)
- [ ] Bootstrap KMS key (`alias/EdlTerraformState` — same literal alias as dev)
- [ ] GitHub OIDC provider — already registered (account-level, shared across all environments)
- [ ] Orphaned-resource pre-flight check (Phase 1, Step 1.6) — run even if this account has
      "never" been deployed to; don't assume it's clean just because it's a new environment
- [ ] NAT Gateway IPs allowlisted in Salesforce, NetSuite, and MySQL RDS SG — **do after Terraform apply**

### Step 11.2 — Copy and update tfvars for the new environment

```bash
cp infrastructure/environments/staging/terraform.tfvars.example \
  infrastructure/environments/staging/terraform.tfvars

# Edit staging/terraform.tfvars and update:
#   alert_email                   = "staging-ops@yourcompany.com"
#   github_org                    = "your-github-org"
#   extraction_pipeline_lambda_arn     = "arn:aws:lambda:...:function:EdlExtractionPipeline"
#
# Do NOT set these — they are wired automatically from Terraform module outputs
# in infrastructure/environments/staging/main.tf (functions are named identically
# to dev's — EdlTransformationPipeline, EdlEntityResolutionPipeline, and
# EdlAnalyticsLayerPublisher — since the AWS account boundary, not the name,
# keeps staging separate from dev; you never paste their ARNs):
#   transformation_pipeline_lambda_arn = module.transformation_lambda.lambda_function_arn
#   entity_resolution_lambda_arn       = module.entity_resolution_lambda.lambda_function_arn
#   analytics_publisher_lambda_arn     = module.analytics_publisher_lambda.lambda_function_arn
#
# Leave unset (default ""); no Terraform module builds this Lambda yet:
#   serving_store_loader_lambda_arn
#
# For prod, copy from infrastructure/environments/prod/terraform.tfvars.example
# and use the extraction Lambda ARN from the prod account (same function name,
# EdlExtractionPipeline, different account/ARN).
```

### Step 11.3 — Bootstrap staging backend

Repeat [Phase 1](#4-phase-1--bootstrap-one-time-only) with `ENV=staging`.

### Step 11.4 — Apply staging infrastructure (excluding orchestration)

```bash
cd infrastructure/environments/staging
terraform init
terraform plan -out=tfplan
terraform apply tfplan
```

This creates VPC, S3 buckets, IAM roles, DynamoDB tables, Secrets Manager paths, CloudWatch alarms, Glue catalog, and the extraction/transformation/entity-resolution/analytics-publisher/control-plane/credential-expiry-notifier Lambda functions (once the package hash is set — see Step 11.5). The orchestration module apply in this pass fails only if `extraction_pipeline_lambda_arn` is not yet set — that is expected and handled in Step 11.5.

> **Note:** If you see `Error: No value for required variable — var.extraction_pipeline_lambda_arn`, this is expected at this stage. Proceed to Step 11.5.

### Step 11.5 — Deploy Lambdas and collect the extraction Lambda's ARN

Repeat [Phase 3](#6-phase-3--application-deployment-lambda) targeting `infrastructure/environments/staging`. The same `terraform apply` that builds transformation/entity-resolution/analytics-publisher also builds extraction — transformation/entity-resolution/analytics-publisher ARNs wire into the orchestration module automatically, so the only ARN you collect and paste is extraction's:

```bash
cd infrastructure/environments/staging

# Extraction Lambda — the only ARN that needs to be collected manually
terraform output extraction_lambda_arn
```

Add it to `infrastructure/environments/staging/terraform.tfvars`:

```hcl
extraction_pipeline_lambda_arn = "arn:aws:lambda:us-east-1:ACCOUNT_ID:function:EdlExtractionPipeline"
# transformation_pipeline_lambda_arn / entity_resolution_lambda_arn / analytics_publisher_lambda_arn
# are NOT set here — see Step 11.2.
# serving_store_loader_lambda_arn intentionally left unset — Lambda not yet built.
```

### Step 11.6 — Re-apply to create Step Functions state machine

```bash
cd infrastructure/environments/staging
terraform apply
```

This apply creates the orchestration module: Standard Workflow state machine, EventBridge schedule group, and CloudWatch alarms. Staging uses `state_machine_type = "STANDARD"` (same as prod) for execution history and timeout support.

### Step 11.7 — Allowlist NAT Gateway IPs in source systems

```bash
# Get NAT Gateway IPs assigned to the staging environment
terraform output nat_gateway_public_ips
# ["a.b.c.d", "e.f.g.h", "i.j.k.l"]
```

Log in to each source system and add these IPs:

- **Salesforce:** Setup → Connected Apps → Edit → IP Relaxation: "Enforce IP Restrictions" and add each IP to the IP Ranges list
- **NetSuite:** Setup → Integrations → Manage Integrations → Edit → Restrict IP Addresses
- **MySQL RDS:** EC2 → Security Groups → find the RDS SG → add inbound rule: TCP 3306 from each NAT IP

### Step 11.8 — Populate staging secrets

Repeat [Step 5.1](#step-51--populate-source-credentials-in-secrets-manager) with staging-specific credentials. The secret paths are the same literal paths as dev — now that each environment is its own AWS account, the path no longer needs an environment segment to avoid collisions:
```
edl/sources/salesforce/credentials
edl/sources/netsuite/credentials
edl/sources/mysql-rds/credentials
```

### Step 11.9 — Seed staging entity configs, field mappings, and entity resolution configs

```bash
# DynamoDB entity config
python scripts/seed_entity_config.py --environment staging --region us-east-1

# Field mappings to staging S3 bucket
python scripts/seed_field_mappings.py --environment staging --region us-east-1

# Entity resolution configs (match rules + survivorship) to staging S3 bucket
python scripts/seed_entity_resolution_configs.py --environment staging --region us-east-1
```

### Step 11.10 — Create extraction schedules for staging

```bash
python scripts/seed_schedules.py --environment staging
# Note: `make seed-schedules` always runs against dev — its Makefile recipe
# hardcodes `--environment dev` with no override variable — so use the direct
# python invocation above for staging/prod.
```

### Step 11.11 — Production promotion checklist

Before applying to production, confirm all of the following:

- [ ] All staging extraction runs completed without failures for at least 5 days
- [ ] All 4 deployed pipeline stages (Extraction → Transformation → EntityResolution → Analytics) succeeded at least once in staging (there is no 5th ServingStore Lambda stage today — it runs as a no-op `Pass` state; see [Phase 3](#6-phase-3--application-deployment-lambda))
- [ ] Schema drift reports reviewed — no outstanding breaking drift events
- [ ] Quality gate (`is_publication_blocked`) never fired unexpectedly in staging
- [ ] `terraform plan` on prod shows only expected changes (no destructive resource replacements)
- [ ] Extraction Lambda ARN for prod environment added to `prod/terraform.tfvars` (transformation/entity-resolution/analytics-publisher wire automatically)
- [ ] NAT Gateway IPs for prod allowlisted in Salesforce, NetSuite, and MySQL RDS SG
- [ ] SNS subscription confirmed for production alert email
- [ ] Manual approval gate in CI/CD pipeline signed off by platform lead
- [ ] Runbook for production incident response reviewed and current

---

## 13. Verification Checklist

After completing all phases, verify the full deployment is healthy.

### Infrastructure

```bash
# All DynamoDB tables exist (table names are now PascalCase, e.g. EdlWatermarkRepository)
aws dynamodb list-tables --region us-east-1 | grep Edl

# All S3 buckets exist and have encryption enabled (bucket names stay lowercase-hyphenated)
aws s3api list-buckets --query "Buckets[?contains(Name,'edl')]"

# All deployed pipeline + support Lambda functions are active
# (there is no EdlServingStoreLoader function — see Phase 3)
for fn in EdlExtractionPipeline EdlTransformationPipeline EdlEntityResolutionPipeline EdlAnalyticsLayerPublisher EdlCredentialExpiryNotifier EdlControlPlane; do
  aws lambda get-function \
    --function-name "${fn}" \
    --region us-east-1 \
    --query "Configuration.[FunctionName,State]" \
    --output text
done

# Step Functions state machine is ACTIVE
aws stepfunctions list-state-machines \
  --query "stateMachines[?contains(name,'Edl')].[name,type,creationDate]" \
  --output table \
  --region us-east-1

# EventBridge schedule group exists
aws scheduler list-schedule-groups \
  --query "ScheduleGroups[?contains(Name,'Edl')].[Name,State]" \
  --output table \
  --region us-east-1

# Secrets exist (do not verify values here)
aws secretsmanager list-secrets --region us-east-1 \
  --query "SecretList[?contains(Name,'edl/sources')].[Name]"
```

### Entity configuration

```bash
# Confirm at least one entity config record exists
aws dynamodb scan \
  --table-name EdlEntityExtractionConfig \
  --select COUNT \
  --region us-east-1
```

### Field mapping

```bash
# Confirm the latest pointer exists for all configured entities (per
# config/field_mappings/{source_id}/{entity_id}/ in Git)
for entity in salesforce/salesforce-account salesforce/salesforce-contact \
              salesforce/salesforce-opportunity salesforce/salesforce-contract \
              netsuite/netsuite-customer mysql-rds/mysql-rds-contracts \
              mysql-rds/mysql-rds-contractterms sage/sage-intacct-customer \
              sage/sage-intacct-vendor sage/sage-intacct-arinvoice sage/sage-intacct-apbill \
              sage/sage-x3-customer sage/sage-x3-supplier; do
  echo "--- ${entity} ---"
  aws s3 ls "s3://edl-mapping-config-087972550871/field-mappings/${entity}/"
done
```

### Entity resolution config

```bash
# Confirm configs exist for all entity types
for entity in company person contract supplier ar_invoice ap_bill opportunity sales-contract contract-term; do
  echo "--- ${entity} ---"
  aws s3 ls "s3://edl-curated-087972550871/entity-resolution/${entity}/"
done

# Verify latest.json pointer is populated
aws s3 cp s3://edl-curated-087972550871/entity-resolution/company/latest.json -
aws s3 cp s3://edl-curated-087972550871/entity-resolution/person/latest.json -
```

### Full pipeline end-to-end test

Trigger one complete run through all four deployed stages (the state machine also runs a `ServingStoreLoad` `Pass` state after Analytics — see the note below):

```bash
# Start via Step Functions directly (bypasses schedule, triggers immediately)
MACHINE_ARN=$(cd infrastructure/environments/dev && terraform output -raw state_machine_arn)
REGION=us-east-1

EXEC_ARN=$(aws stepfunctions start-execution \
  --state-machine-arn "${MACHINE_ARN}" \
  --input '{"source_id":"salesforce","entity_id":"salesforce-account","environment":"dev"}' \
  --query executionArn \
  --output text \
  --region "${REGION}")

echo "Execution ARN: ${EXEC_ARN}"

# Poll status (check every ~30 seconds manually, or watch in AWS Console)
aws stepfunctions describe-execution \
  --execution-arn "${EXEC_ARN}" \
  --query "[status,startDate,stopDate]" \
  --region "${REGION}"
```

Expected terminal status: `SUCCEEDED`

**Expected CloudWatch log events across all four deployed stages (in order):**

| Stage | Lambda | Expected log event |
|---|---|---|
| Extraction | `EdlExtractionPipeline` | `run_complete` with `status: success` |
| Transformation | `EdlTransformationPipeline` | `transformation_complete` with `curated_record_count > 0` |
| Entity Resolution | `EdlEntityResolutionPipeline` | `golden_record_published` with `cluster_count > 0` |
| Analytics | `EdlAnalyticsLayerPublisher` | `analytics_publish_complete` |

There is no 5th "Serving Store" stage to check — the state machine's `ServingStoreLoad` state is a `Pass` state today (no Lambda invocation, no log event); see [Phase 3](#6-phase-3--application-deployment-lambda) and [Step 4.2](#step-42--apply-to-create-the-state-machine).

**Verify S3 outputs at each stage:**

```bash
# Stage A — Raw Parquet
aws s3 ls s3://edl-raw-087972550871/salesforce/salesforce-account/ --recursive | head -5

# Stage B — Curated Parquet + quality report
aws s3 ls s3://edl-curated-087972550871/curated/customer/salesforce-account/ --recursive | head -5

# Stage C/D — Golden records
aws s3 ls s3://edl-analytics-087972550871/canonical/ --recursive | head -5

# Stage E — Analytics layer
aws s3 ls s3://edl-analytics-087972550871/ --recursive | head -5
```

### NAT Gateway IP allowlisting verification

```bash
# Verify NAT IPs are correctly exported
terraform output nat_gateway_public_ips

# Confirm extraction Lambda can reach Salesforce (expect HTTP 200 on auth endpoint)
# (This is indirectly verified by a successful extraction run above)
```

### Alarm and alerting

```bash
# Confirm no alarms are currently in ALARM state
aws cloudwatch describe-alarms \
  --alarm-name-prefix "Edl" \
  --state-value ALARM \
  --query "MetricAlarms[*].[AlarmName,StateValue,StateReason]" \
  --output table \
  --region us-east-1

# Confirm SNS subscription is confirmed (not PendingConfirmation)
aws sns list-subscriptions-by-topic \
  --topic-arn "$(cd infrastructure/environments/dev && terraform output platform_alerts_topic_arn)" \
  --query "Subscriptions[*].[Protocol,Endpoint,SubscriptionArn]" \
  --output table
```

---

## 14. Troubleshooting

| Problem | Likely cause | Fix |
|---|---|---|
| `terraform init` fails with "bucket does not exist" | Bootstrap S3 bucket not created | Complete [Phase 1](#4-phase-1--bootstrap-one-time-only) and [Section 2.1](#21-terraform-remote-state-backend-per-environment) |
| `terraform apply` fails — "No value for required variable: extraction_pipeline_lambda_arn" | Extraction Lambda ARN not yet set in tfvars (transformation/entity-resolution/analytics-publisher wire automatically and don't need this) | Deploy Lambdas first (Phase 3), collect the extraction Lambda's ARN, then re-apply (see [Section 2.5](#25-pipeline-lambda-arns-for-step-functions-state-machine)) |
| `terraform apply` fails — IAM module trust policy error | GitHub OIDC provider not registered in AWS | Run `aws iam create-open-id-connect-provider` (see [Section 2.2](#22-github-actions-oidc-provider)) |
| Lambda fails with `AccessDeniedException` on DynamoDB | IAM role lacks correct permissions | Re-run `terraform apply` — IAM policy may not have applied |
| Lambda fails with `ResourceNotFoundException` on Secrets Manager | Secret not yet populated | Run `aws secretsmanager put-secret-value` (Step 5.1) |
| Extraction runs but `record_count = 0` | Watermark is ahead of data; or field_mode excludes relevant fields | Check watermark value in DynamoDB; check `field_mode` and `exclude_fields` in entity config |
| Extraction Lambda returns `401 Unauthorized` from source system | NAT Gateway IPs not allowlisted in source system | Get IPs from `terraform output nat_gateway_public_ips` and add to source system allowlist (see [Section 2.6](#26-source-system-network-access)) |
| Transformation fails with `MappingRuleSetNotFoundError` | Field mapping not uploaded for this entity | Upload mapping JSON to S3 (Section 9); or expected if using identity mapping |
| Step Functions execution stuck in extraction | Lambda timeout too low; extraction taking >15 min | Increase Lambda timeout in terraform.tfvars; or reduce batch size in entity config |
| Step Functions execution shows `TransformationBlocked=true` | Schema drift classified as BREAKING was detected | Review drift report in S3; fix upstream schema or update field mapping; reset drift flag |
| Step Functions execution shows `PublicationBlocked=true` | Data quality gate failed | Review quality report in S3 curated bucket; check quality threshold configuration |
| `terraform plan` wants to destroy and recreate S3 buckets | `force_destroy` flag or bucket name changed | Never rename S3 buckets; review plan carefully before applying |
| EventBridge schedule not triggering | `active: false` in entity config; or schedule disabled | Set `active: true` in entity config; check schedule status in EventBridge console |
| CloudWatch alarms firing immediately after deploy | `alert_email` not confirmed in SNS | Check email inbox for SNS subscription confirmation (see [Section 2.7](#27-sns-email-subscription-confirmation)) |
| NAT Gateway IPs changed after infrastructure recreation | NAT Gateway was destroyed and recreated (new Elastic IPs assigned) | Get new IPs from `terraform output nat_gateway_public_ips`; update all source system allowlists |

---

## Technology Stack and Version Reference

Complete authoritative version reference for all tools used in deployment.

### Infrastructure and Provisioning

| Tool | Required version | Verify with |
|---|---|---|
| **Terraform** | ≥ 1.8, < 2.0 | `terraform version` |
| **AWS Terraform Provider** | ~> 5.0 | Declared in `infrastructure/modules/*/versions.tf` |
| **AWS CLI** | v2 (any recent) | `aws --version` |
| **Python** | 3.14.x | `python --version` (managed via pyenv) |
| **pyenv** | 2.7.2+ | `pyenv --version` |
| **GNU Make** | ≥ 3.8 | `make --version` |

### Python Runtime Dependencies

| Package | Minimum version | Purpose |
|---|---|---|
| **pydantic** | ≥ 2.7 | Frozen data model validation |
| **structlog** | ≥ 24.4 | Structured JSON logging + PII scrubbing |
| **boto3** | Latest | AWS SDK |
| **pyarrow** | Latest | Apache Parquet I/O |
| **pymysql** | Latest | MySQL RDS connector |
| **requests** | Latest | Salesforce + NetSuite HTTP client |

### Code Quality Gate (all must pass before deploy)

| Tool | Min version | Command | Gate enforced by |
|---|---|---|---|
| **Ruff** | ≥ 0.5 | `ruff check .` | GitHub Actions `ci.yml` |
| **mypy** | ≥ 1.10 | `mypy .` | GitHub Actions `ci.yml` |
| **pytest** | Latest | `pytest --cov --cov-fail-under=80` | GitHub Actions `ci.yml` |
| **bandit** | ≥ 1.7 | `bandit -r . -c pyproject.toml` | GitHub Actions `ci.yml` |
| **pip-audit** | ≥ 2.7 | `pip-audit` | GitHub Actions `ci.yml` |
| **checkov** | Latest | `checkov -d infrastructure/` | GitHub Actions `ci.yml` |
| **Terraform validate** | N/A | `terraform validate` | GitHub Actions `ci.yml` |

> **Ordering caveat:** `make typecheck` (`mypy .`) can fail with unrelated import errors if it's run **after** `make lambda-package` (Phase 3, Step 3.1) — the build drops a `dist/lambda-build/typing_extensions.py` that shadows the real `typing_extensions` package on mypy's search path. Either run `make typecheck` **before** packaging, or scope it to source packages only:
> ```bash
> mypy connector_runtime schema_management watermark_management observability orchestration transformation governance entity_resolution analytics_publisher contracts
> ```

### AWS Services Deployed (per environment)

| Service | Resource name pattern | Deployed by |
|---|---|---|
| **S3** | `edl-raw-<ACCOUNT_ID>`, `edl-curated-<ACCOUNT_ID>`, `edl-analytics-<ACCOUNT_ID>`, `edl-schema-snapshots-<ACCOUNT_ID>` (dev account ID: `087972550871`) | `infrastructure/modules/storage/` |
| **KMS** | alias `EdlPlatformKey` | `infrastructure/modules/kms/` |
| **VPC** | `EdlVpc`; 3 private subnets; 5 VPC Endpoints | `infrastructure/modules/networking/` |
| **IAM** | 13 roles total, including the OIDC CI/CD role (`cicd_deployment`) and one runtime role per Lambda (`extraction_runtime`, `control_plane`, `credential_expiry_notifier`, etc.) | `infrastructure/modules/iam/` |
| **DynamoDB** | `EdlEntityExtractionConfig`, `EdlWatermarkRepository`, `EdlRunAuditLog`, `EdlEntityTypeRegistry`, `EdlSourceOnboardingRegistry` | `infrastructure/modules/metadata_persistence/` |
| **Secrets Manager** | `edl/sources/salesforce/credentials`, `edl/sources/netsuite/credentials`, `edl/sources/mysql-rds/credentials` | `infrastructure/modules/secrets/` |
| **Step Functions** | `EdlExtractionOrchestrationWorkflow` | `infrastructure/modules/orchestration/` |
| **CloudWatch** | 5 log groups; namespace `EnterpriseDatalake`; 4 alarms; X-Ray group | `infrastructure/modules/observability/` |
| **SNS** | `EdlPlatformAlerts` | `infrastructure/modules/observability/` |
| **SQS (DLQ)** | `EdlExtractionDlq` | `infrastructure/modules/metadata_persistence/` |
| **Glue Data Catalog** | `edl_curated`, `edl_analytics` databases | Created at runtime by transformation pipeline |
| **EventBridge Schedules** | `{source_id}--{entity_id}` | Managed at runtime via `extraction_schedule_client.py` |
| **EventBridge Scheduler (credential check)** | `EdlCredentialExpiryCheck` (rate: 1 day) | `infrastructure/modules/secrets/` |
| **Cognito + API Gateway (control plane)** | User pool `EdlControlPlane`; HTTP API fronting `EdlControlPlane` Lambda | `infrastructure/modules/control_plane/` — see [Section 11.J](#j-control-plane-api-cognito--api-gateway) |
| **RDS MySQL** | `EdlServingStore` | `infrastructure/modules/serving_store/` |

### Data Format Specifications

| Format | Spec | Storage layer |
|---|---|---|
| **Apache Parquet** | Raw: `large_utf8` columns (no type coercion); Curated/Analytics: Snappy-compressed | All data lake layers |
| **JSON** | UTF-8; no BOM | Config files, snapshots, reports, lineage |
| **DynamoDB Item** | Native DynamoDB JSON serialisation | Config, watermark, audit, onboarding tables |
