# Deployment Guide — Enterprise Data Lake Platform

**Version:** 2.0  
**Date:** 2026-06-16  
**Audience:** Platform engineers deploying to AWS for the first time, or promoting to a new environment

---

## Table of Contents

1. [Prerequisites](#1-prerequisites)
2. [AWS Prerequisites — Must Exist Before Terraform](#2-aws-prerequisites--must-exist-before-terraform)
3. [Deployment Overview — The Six Phases](#3-deployment-overview--the-six-phases)
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
| S3 bucket (state file) | `{env}-edl-terraform-state` | `aws s3api create-bucket` (see Phase 1, Step 1.2) |
| DynamoDB table (state lock) | `{env}-edl-terraform-state-lock` | `aws dynamodb create-table` (see Phase 1, Step 1.3) |
| KMS key (state encryption) | alias `{env}-terraform-state` | `aws kms create-key` + `aws kms create-alias` (see Phase 1, Step 1.4) |

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
make lambda-package          # build the zip (Phase 3, Step 3.1)
make lambda-upload           # upload to S3  (Phase 3, Step 3.2)
terraform apply              # now safe to apply
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

### 2.5 Five Pipeline Lambda ARNs (for Step Functions state machine)

The orchestration module's Step Functions state machine references **five Lambda function ARNs** — one per pipeline stage. These are passed in as Terraform variables (`var.extraction_pipeline_lambda_arn`, etc.).

**These must be deployed Lambda functions before `terraform apply` is run for the orchestration module.**

The correct order is:

```
1. terraform apply module.lambda_pipeline    ← creates extraction Lambda
2. Deploy remaining four Lambda packages     ← transformation, entity-resolution, analytics, serving-store
3. terraform apply module.orchestration      ← wires them into Step Functions
```

In practice, the full `terraform apply` (no `-target`) handles this automatically because `module.orchestration` has `depends_on = [module.iam, module.observability]` and the Lambda ARNs are passed as variables from outside Terraform — set them in `terraform.tfvars` after Step 2.

**If you apply before Lambda ARNs exist:** `terraform apply` will fail validation because the variables are required with no default. You will see:
```
Error: No value for required variable
  var.transformation_pipeline_lambda_arn
```

Fix: deploy the Lambda functions first, then set the ARNs in `terraform.tfvars`.

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
- [ ] Terraform state S3 bucket created (`{env}-edl-terraform-state`)
- [ ] Terraform state DynamoDB lock table created (`{env}-edl-terraform-state-lock`)
- [ ] Bootstrap KMS key created (`alias/{env}-terraform-state`)
- [ ] `backend.tf` updated to match the above names
- [ ] GitHub Actions OIDC provider registered in AWS IAM (once per account)
- [ ] Lambda deployment package built and uploaded to S3 (before `terraform apply`)
- [ ] Five pipeline Lambda ARNs available (before orchestration module apply)
- [ ] NAT Gateway IPs whitelisted in Salesforce and NetSuite (after networking apply)
- [ ] MySQL RDS security group allows inbound from Lambda SG (after networking apply)
- [ ] SNS subscription confirmation email clicked (after first Terraform apply)
- [ ] AWS service limits reviewed for production

---

## 3. Deployment Overview — The Six Phases

```
PHASE 1             PHASE 2                  PHASE 3              PHASE 4
BOOTSTRAP           INFRASTRUCTURE           APPLICATION          PIPELINE CONFIG
(one-time)          (Terraform)              (Lambda)             (Step Functions)
──────────          ──────────────────────   ─────────────────    ────────────────
Create S3           terraform init         → make lambda-package  Set Lambda ARNs
state bucket      → terraform plan         → make lambda-upload   in terraform.tfvars
Create DynamoDB   → terraform apply        → terraform apply    → terraform apply
lock table          (VPC, S3, DynamoDB,      (deploys 5 Lambdas)  (creates chained
Create KMS key      IAM, Secrets, SFN,                            state machine)
Register OIDC       CloudWatch, Glue)
provider

PHASE 5                                    PHASE 6
DATA CONFIGURATION                         FIELD MAPPINGS
───────────────────────────────────────    ─────────────────────────────────────
aws secretsmanager put-secret-value      → python scripts/seed_field_mappings.py
python scripts/seed_entity_config.py       (publishes JSON files from config/ to S3)
Set EventBridge schedules               → Verify first automated end-to-end run
(ExtractionScheduleClient)
```

---

## 4. Phase 1 — Bootstrap (One-time Only)

The Terraform remote state backend (S3 bucket + DynamoDB lock table) must exist **before** `terraform init` can run. This is a manual one-time step.

### Step 1.1 — Set environment variables

```bash
export AWS_PROFILE=your-admin-profile    # or set AWS_ACCESS_KEY_ID + AWS_SECRET_ACCESS_KEY
export AWS_REGION=us-east-1
export ENV=dev                           # change to staging or prod when promoting
```

### Step 1.2 — Create the Terraform state bucket

```bash
aws s3api create-bucket \
  --bucket ${ENV}-edl-terraform-state \
  --region ${AWS_REGION} \
  --create-bucket-configuration LocationConstraint=${AWS_REGION}

# Enable versioning (required — protects state file)
aws s3api put-bucket-versioning \
  --bucket ${ENV}-edl-terraform-state \
  --versioning-configuration Status=Enabled

# Block all public access
aws s3api put-public-access-block \
  --bucket ${ENV}-edl-terraform-state \
  --public-access-block-configuration \
    "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true"

# Enable default SSE-S3 encryption (upgraded to KMS after bootstrap)
aws s3api put-bucket-encryption \
  --bucket ${ENV}-edl-terraform-state \
  --server-side-encryption-configuration \
    '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'
```

### Step 1.3 — Create the DynamoDB state lock table

```bash
aws dynamodb create-table \
  --table-name ${ENV}-edl-terraform-state-lock \
  --attribute-definitions AttributeName=LockID,AttributeType=S \
  --key-schema AttributeName=LockID,KeyType=HASH \
  --billing-mode PAY_PER_REQUEST \
  --region ${AWS_REGION}
```

### Step 1.4 — Create the bootstrap KMS key for state encryption

```bash
KEY_ID=$(aws kms create-key \
  --description "${ENV} Terraform state encryption" \
  --region ${AWS_REGION} \
  --query KeyMetadata.KeyId \
  --output text)

aws kms create-alias \
  --alias-name alias/${ENV}-terraform-state \
  --target-key-id ${KEY_ID} \
  --region ${AWS_REGION}

echo "KMS key ID: ${KEY_ID}"
```

### Step 1.5 — Update backend.tf

Open `infrastructure/environments/${ENV}/backend.tf` and confirm the values match what you just created:

```hcl
terraform {
  backend "s3" {
    bucket         = "dev-edl-terraform-state"      # ← your bucket name
    key            = "environments/dev/terraform.tfstate"
    region         = "us-east-1"                    # ← your region
    encrypt        = true
    kms_key_id     = "alias/dev-terraform-state"    # ← your KMS alias
    dynamodb_table = "dev-edl-terraform-state-lock" # ← your lock table
  }
}
```

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

# Set AFTER running make lambda-package (Step 5.1 below)
lambda_package_s3_bucket   = "dev-edl-terraform-state"
lambda_package_s3_key      = "lambda/extraction-pipeline.zip"
lambda_package_source_hash = ""   # Fill in after running make lambda-package
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
| `aws_dynamodb_table` | 4 | watermark, run-audit-log, entity-config, source-onboarding |
| `aws_iam_role` | 5+ | extraction, transformation, entity-resolution, analytics-serve, governance |
| `aws_vpc` + subnets | 1 VPC | private subnets, VPC endpoints |
| `aws_secretsmanager_secret` | 3 | one per source system (values set later) |
| `aws_sqs_queue` | 2 | extraction DLQ + retry queue |
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
| `step_functions_state_machine_arn` | Schedule target |
| `salesforce_secret_arn` | Secret to populate |
| `netsuite_secret_arn` | Secret to populate |
| `mysql_rds_secret_arn` | Secret to populate |

---

## 6. Phase 3 — Application Deployment (Lambda)

> **Prerequisite:** Phase 2 Terraform apply must be complete so the S3 artifacts bucket exists for the Lambda zip upload.

There are **five Lambda functions** in the full pipeline. They must all be deployed before Step Functions can wire them together in Phase 4.

| Lambda | Handler | Purpose |
|---|---|---|
| `{env}-extraction-pipeline` | `connector_runtime.extraction_pipeline_handler.lambda_handler` | Stages 1–10: extract raw data |
| `{env}-transformation-pipeline` | `transformation.transformation_handler.lambda_handler` | Stage 11: raw → curated |
| `{env}-entity-resolution` | `entity_resolution.resolution_handler.lambda_handler` | Stage 12–13: cross-source matching + golden records |
| `{env}-analytics-publisher` | `transformation.analytics_handler.lambda_handler` | Stage 14: curated/golden → analytics layer |
| `{env}-serving-store-loader` | `transformation.serving_handler.lambda_handler` | Stage 15: analytics → MySQL RDS |

### Step 3.1 — Build the Lambda package

```bash
cd /path/to/enterprise-data-lake  # repo root
source .venv/bin/activate

make lambda-package
# Output: dist/extraction-pipeline.zip
# Output: SHA-256 (base64): <hash-string>  ← copy this hash
```

Copy the SHA-256 hash printed at the end. Paste it into `terraform.tfvars`:

```hcl
lambda_package_source_hash = "abc123==..."   # ← paste the base64 hash here
```

### Step 3.2 — Upload to S3

```bash
export ARTIFACTS_BUCKET=dev-edl-terraform-state
export AWS_REGION=us-east-1

make lambda-upload
# Uploads dist/extraction-pipeline.zip to s3://dev-edl-terraform-state/lambda/extraction-pipeline.zip
```

### Step 3.3 — Deploy Lambda via Terraform

```bash
cd infrastructure/environments/dev
terraform apply \
  -var="lambda_package_source_hash=$(openssl dgst -sha256 -binary ../../dist/extraction-pipeline.zip | openssl base64)"
```

Or use the convenience target which does all three steps:

```bash
make lambda-deploy   # packages + uploads + applies Terraform
```

### Step 3.4 — Collect Lambda ARNs

After applying, collect all five Lambda ARNs — you need them for Phase 4:

```bash
cd infrastructure/environments/dev

# Extraction Lambda (created by lambda_pipeline module)
terraform output extraction_lambda_arn

# Remaining four Lambdas (created by their respective modules when built)
aws lambda get-function --function-name dev-transformation-pipeline \
  --query Configuration.FunctionArn --output text

aws lambda get-function --function-name dev-entity-resolution \
  --query Configuration.FunctionArn --output text

aws lambda get-function --function-name dev-analytics-publisher \
  --query Configuration.FunctionArn --output text

aws lambda get-function --function-name dev-serving-store-loader \
  --query Configuration.FunctionArn --output text
```

### Step 3.5 — Verify all Lambdas deployed

```bash
for fn in extraction-pipeline transformation-pipeline entity-resolution analytics-publisher serving-store-loader; do
  aws lambda get-function \
    --function-name "dev-${fn}" \
    --region us-east-1 \
    --query "Configuration.[FunctionName,State,LastModified]" \
    --output table
done
```

All five should show `State: Active`.

---

## 7. Phase 4 — Automatic Pipeline Configuration (Step Functions)

This phase wires all five Lambda functions into the Step Functions state machine that runs the full end-to-end pipeline automatically.

### Step 4.1 — Add Lambda ARNs to terraform.tfvars

Add the five ARNs collected in Step 3.4 to `infrastructure/environments/dev/terraform.tfvars`:

```hcl
# infrastructure/environments/dev/terraform.tfvars

# Pipeline Lambda ARNs — set after running Phase 3
extraction_pipeline_lambda_arn     = "arn:aws:lambda:us-east-1:123456789012:function:dev-extraction-pipeline"
transformation_pipeline_lambda_arn = "arn:aws:lambda:us-east-1:123456789012:function:dev-transformation-pipeline"
entity_resolution_lambda_arn       = "arn:aws:lambda:us-east-1:123456789012:function:dev-entity-resolution"
analytics_publisher_lambda_arn     = "arn:aws:lambda:us-east-1:123456789012:function:dev-analytics-publisher"
serving_store_loader_lambda_arn    = "arn:aws:lambda:us-east-1:123456789012:function:dev-serving-store-loader"
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
|  dev-extraction-pipeline                      | ACTIVE  |
|  STANDARD                                     |         |
-----------------------------------------------------------
```

### Step 4.4 — Create extraction schedules per entity

Each entity needs an EventBridge schedule pointing at the state machine. Schedules are **data** — managed by `ExtractionScheduleClient` at runtime, not by Terraform:

```bash
python scripts/trigger_extraction.py \
  --create-schedule \
  --source-id salesforce \
  --entity-id salesforce-account \
  --schedule "cron(0 2 * * ? *)" \
  --param object_name=Account \
  --environment dev \
  --region us-east-1

# Repeat for each entity
python scripts/trigger_extraction.py \
  --create-schedule \
  --source-id salesforce \
  --entity-id salesforce-contact \
  --schedule "cron(0 2 * * ? *)" \
  --param object_name=Contact \
  --environment dev \
  --region us-east-1

python scripts/trigger_extraction.py \
  --create-schedule \
  --source-id netsuite \
  --entity-id netsuite-customer \
  --schedule "cron(0 3 * * ? *)" \
  --param record_type=customer \
  --environment dev \
  --region us-east-1

python scripts/trigger_extraction.py \
  --create-schedule \
  --source-id mysql-rds \
  --entity-id mysql-rds-orders \
  --schedule "cron(0 4 * * ? *)" \
  --param table_name=orders \
  --environment dev \
  --region us-east-1
```

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
ENV=dev
RUN_ID="run-20260616-..."  # from execution output

# Stage A — Raw Parquet written
aws s3 ls s3://${ENV}-edl-raw/salesforce/salesforce-account/ --recursive

# Stage B — Curated Parquet written
aws s3 ls s3://${ENV}-edl-curated/curated/customer/salesforce-account/ --recursive

# Stage B — Quality report (is_publication_blocked must be false)
aws s3 cp s3://${ENV}-edl-curated/quality-reports/salesforce/salesforce-account/${RUN_ID}/quality-report.json -

# Stage C/D — Golden records and analytics
aws s3 ls s3://${ENV}-edl-analytics/canonical/ --recursive
aws s3 ls s3://${ENV}-edl-analytics/ --recursive
```

---

## 8. Phase 5 — Data Configuration (DynamoDB Seeds + Secrets)

### Step 6.1 — Populate source credentials in Secrets Manager

This step stores actual credentials. **Do this from a secure workstation only.** Never commit credential values to git.

**Salesforce credentials:**

```bash
aws secretsmanager put-secret-value \
  --secret-id "dev/sources/salesforce/credentials" \
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
  --secret-id "dev/sources/netsuite/credentials" \
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
  --secret-id "dev/sources/mysql-rds/credentials" \
  --region us-east-1 \
  --secret-string '{
    "host":     "your-rds-endpoint.us-east-1.rds.amazonaws.com",
    "port":     3306,
    "database": "your_database_name",
    "username": "edl_readonly",
    "password": "YOUR_READONLY_PASSWORD"
  }'
```

> **Security note:** The `extraction-service-role` IAM role created by Terraform has `GetSecretValue` permission on these exact secret ARNs only. No other role can read these credentials.

### Step 6.2 — Seed entity configuration records into DynamoDB

```bash
python scripts/seed_entity_config.py \
  --environment dev \
  --region us-east-1
```

This writes the default entity configuration records for `salesforce-account`, `salesforce-contact`, `netsuite-customer`, and `mysql-rds-orders`. All records are idempotent (safe to run multiple times).

To add a new entity, edit `scripts/seed_entity_config.py` and add a record to the `_RECORDS` list, then re-run the script. No Terraform changes needed.

**Entity configuration fields explained:**

```python
{
    "source_id":          "salesforce",           # stable source identifier
    "entity_id":          "salesforce-account",   # stable entity identifier
    "config_version":     "1.0.0",                # semantic version
    "load_type":          "incremental",           # "full" or "incremental"
    "watermark_field":    "SystemModstamp",        # source timestamp field for delta sync
    "extraction_window_days": 1,                  # max days per extraction run
    "watermark_overlap_hours": 1,                 # overlap to catch late-arriving records
    "field_mode":         "all",                  # "all", "standard", "custom", "includeOnly"
    "include_fields":     [],                     # only used when field_mode = "includeOnly"
    "exclude_fields":     ["IsDeleted"],          # always excluded regardless of field_mode
    "output_format":      "parquet",              # always parquet
    "active":             True                    # False = skip this entity
}
```

### Step 6.3 — Create EventBridge extraction schedules

```bash
python scripts/trigger_extraction.py \
  --create-schedule \
  --source-id salesforce \
  --entity-id salesforce-account \
  --schedule "cron(0 2 * * ? *)" \
  --environment dev \
  --region us-east-1
```

Repeat for each entity. To see all available options:

```bash
python scripts/trigger_extraction.py --help
```

---

## 9. Phase 6 — Field Mapping Configuration

Field mapping tells the transformation pipeline how to rename source fields to canonical business names. **If you don't provide a mapping, the pipeline uses identity mapping (field names passed through unchanged).**

### Where field mappings are stored

Field mappings are **JSON files stored in S3**:

```
s3://{env}-edl-mapping-config/
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
BUCKET = "dev-edl-mapping-config"   # ← from terraform output mapping_bucket_name

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
BUCKET=dev-edl-mapping-config
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
    s3_bucket="dev-edl-mapping-config",
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
s3://{env}-edl-curated/
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

| Entity type | Git config path | Sources merged | Output prefix |
|---|---|---|---|
| `company` | `config/entity_resolution/company/` | Salesforce Account + NetSuite Customer | `canonical/company/` |
| `person` | `config/entity_resolution/person/` | Salesforce Contact | `canonical/person/` |

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
# 2. Publish new version — also updates latest.json pointer
python scripts/seed_entity_resolution_configs.py --environment dev --entity-type company

# Rollback: pin to v1
python scripts/seed_entity_resolution_configs.py --environment dev --entity-type company --pin-version v1
```

### Verify entity resolution configs published

```bash
# Check all configs exist in S3
for entity in company person; do
  echo "--- ${entity} ---"
  aws s3 ls "s3://dev-edl-curated/entity-resolution/${entity}/"
done

# Inspect the latest pointer
aws s3 cp s3://dev-edl-curated/entity-resolution/company/latest.json -
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
| `lambda_package_s3_bucket` | S3 bucket where Lambda zip is uploaded | `"dev-edl-terraform-state"` |
| `lambda_package_s3_key` | S3 key of the Lambda zip | `"lambda/extraction-pipeline.zip"` |
| `lambda_package_source_hash` | Base64 SHA-256 of zip (triggers Lambda update) | output of `make lambda-package` |

### B. `backend.tf` — Terraform remote state settings

File: `infrastructure/environments/{env}/backend.tf`

| Setting | What it is | Example |
|---|---|---|
| `bucket` | S3 bucket for Terraform state | `"dev-edl-terraform-state"` |
| `key` | S3 object key for the state file | `"environments/dev/terraform.tfstate"` |
| `region` | State bucket region | `"us-east-1"` |
| `kms_key_id` | KMS alias for state encryption | `"alias/dev-terraform-state"` |
| `dynamodb_table` | DynamoDB table for state locking | `"dev-edl-terraform-state-lock"` |

### C. AWS Secrets Manager — Source credentials

Secret path: `{environment}/sources/{source_id}/credentials`

| Source | Secret path | Fields in JSON |
|---|---|---|
| Salesforce | `dev/sources/salesforce/credentials` | `instance_url`, `client_id`, `client_secret` |
| NetSuite | `dev/sources/netsuite/credentials` | `account_id`, `consumer_key`, `consumer_secret`, `token_id`, `token_secret` |
| MySQL RDS | `dev/sources/mysql-rds/credentials` | `host`, `port`, `database`, `username`, `password` |

**How to set:** `aws secretsmanager put-secret-value` (see Step 6.1 above)  
**Who can read:** Only the `extraction-service-role` IAM role (enforced by Secrets Manager resource policy)  
**Where configured:** `infrastructure/modules/secrets/main.tf` — the secret ARNs and resource policies are created by Terraform

### D. DynamoDB — Entity extraction configuration

Table: `{environment}-entity-extraction-config`

| Setting | How to set | Notes |
|---|---|---|
| Entity config records | `python scripts/seed_entity_config.py` | Idempotent; safe to re-run |
| New entity for existing source | Edit `scripts/seed_entity_config.py`, add record, re-run | No Terraform change |
| New source system | Add adapter code + seed record + register credential | See [docs/PLATFORM_FLOW.md — Adding a New Connector](PLATFORM_FLOW.md#10-adding-a-new-connector) |

Key fields you will configure per entity: `load_type`, `watermark_field`, `field_mode`, `exclude_fields`, `extraction_window_days`.

### E. S3 — Field mapping configuration

Bucket: `{environment}-edl-mapping-config`  
Prefix: `field-mappings/{source_id}/{entity_id}/`

| File | Purpose | How to set |
|---|---|---|
| `{version}.json` | Versioned field mapping rule set | Upload via script or AWS CLI (see Section 7) |
| `latest.json` | Pointer to the current active version | Updated automatically when you publish a rule set |

### F. EventBridge Scheduler — Extraction schedules

Schedule group: `{environment}-edl-extraction-schedules` (created by Terraform)

| Setting | How to set | Example |
|---|---|---|
| Schedule expression | `python scripts/trigger_extraction.py --create-schedule` | `"cron(0 2 * * ? *)"` |
| Time zone | UTC (all schedules are UTC) | — |
| Target | Step Functions state machine ARN (set by Terraform automatically) | — |
| Input payload | `{"source_id": "...", "entity_id": "..."}` | Set by the schedule client |

**To update a schedule:** `python scripts/trigger_extraction.py --update-schedule ...`  
**To disable a schedule:** `python scripts/trigger_extraction.py --disable-schedule ...`  
**No Terraform change needed** for schedule changes.

### G. Lambda environment variables

These are set by Terraform in `infrastructure/modules/lambda_pipeline/main.tf`. After Terraform apply, Lambda has:

| Environment variable | Value (from Terraform outputs) | Purpose |
|---|---|---|
| `ENVIRONMENT` | `dev` / `staging` / `prod` | Determines DynamoDB table names and S3 bucket names |
| `AWS_DEFAULT_REGION` | `us-east-1` | AWS SDK default region |
| `ENTITY_CONFIG_TABLE` | `dev-entity-extraction-config` | DynamoDB config table |
| `WATERMARK_TABLE` | `dev-watermark-repository` | DynamoDB watermark table |
| `AUDIT_TABLE` | `dev-run-audit-log` | DynamoDB audit table |
| `RAW_BUCKET` | `dev-edl-raw` | S3 raw layer bucket |
| `SCHEMA_SNAPSHOTS_BUCKET` | `dev-edl-schema-snapshots` | Schema snapshot bucket |
| `MAPPING_BUCKET` | `dev-edl-mapping-config` | Field mapping bucket |
| `GOVERNANCE_BUCKET` | `dev-edl-governance` | Lineage and retention bucket |
| `DLQ_URL` | SQS queue URL | Dead-letter queue for failed runs |
| `LOG_LEVEL` | `INFO` (prod) / `DEBUG` (dev) | Structured log verbosity |

You do **not** need to set these manually — Terraform configures them. If you need to change a value, update the Terraform variable and re-apply.

### H. CloudWatch Alarms — Alert thresholds

Created by Terraform in `infrastructure/modules/observability/`. Key alarms:

| Alarm name | Trigger | Action |
|---|---|---|
| `{env}-edl-extraction-failure-rate` | > 5% failure rate over 5 min | SNS → alert_email |
| `{env}-edl-dlq-depth` | DLQ has > 0 messages for > 4 hours | SNS → alert_email |
| `{env}-edl-watermark-lag` | Lag > 26 hours (daily entity) | SNS → alert_email |
| `{env}-edl-breaking-drift` | Breaking drift event detected | SNS → alert_email |

To change alert thresholds, edit `infrastructure/modules/observability/variables.tf` and run `terraform apply`.

### I. IAM Least Privilege — What Each Role Can Access

The platform enforces a **zero-trust, need-to-know** IAM model. Every role is created by Terraform and scoped to only the exact resources and actions it requires — no `Resource: "*"` and no `Action: "*"` permissions anywhere.

| IAM role | AWS services it can access | Explicit restrictions |
|---|---|---|
| `{env}-extraction-service-role` | S3 (`PutObject` on `raw/` prefix only) · DynamoDB (`GetItem`/`PutItem` on config, watermark, audit tables) · Secrets Manager (`GetSecretValue` on `{env}/sources/*` only) · CloudWatch Logs | Cannot write to curated or analytics buckets; cannot read Secrets Manager secrets from other environments |
| `{env}-transformation-service-role` | S3 (`GetObject` on raw prefix; `PutObject` on curated prefix) · S3 (`GetObject`/`PutObject` on mapping-config bucket) · Glue (`CreateTable`, `UpdateTable` on the platform database only) · CloudWatch Logs | Cannot access raw layer for write; cannot read Secrets Manager |
| `{env}-entity-resolution-role` | S3 (`GetObject` on curated prefix; `GetObject` on entity-resolution config prefix; `PutObject` on analytics `canonical/` prefix) · CloudWatch Logs | Cannot read raw or mapping-config buckets; cannot access Secrets Manager |
| `{env}-analytics-publisher-role` | S3 (`GetObject` on curated prefix; `PutObject` on analytics `curated/` prefix) · Glue · CloudWatch Logs | Cannot write to canonical (entity-resolved) prefix |
| `{env}-serving-store-role` | S3 (`GetObject` on analytics prefix) · Secrets Manager (`GetSecretValue` on serving DB secret only) · CloudWatch Logs | Cannot write to any S3 bucket |
| `ci-cd-deploy-role` | Terraform state S3 bucket · IAM (boundary-constrained role updates) · Lambda/ECS task deployments | Cannot access data buckets, Secrets Manager values, or DynamoDB data tables |

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

---

## 12. Promoting to Staging and Production

The deployment process for staging and production is the same as dev — the environment directory changes, and a few additional steps apply because the full automatic pipeline (5-stage Step Functions state machine) requires all Lambda ARNs before Terraform can apply the orchestration module.

### Step 11.1 — Complete AWS Prerequisites for the new environment

Before Terraform can run for staging or prod, repeat **Section 2** for the new environment:

- [ ] Terraform state S3 bucket (`staging-edl-terraform-state` or `prod-edl-terraform-state`)
- [ ] DynamoDB lock table (`staging-edl-terraform-state-lock` or `prod-edl-terraform-state-lock`)
- [ ] Bootstrap KMS key (`alias/staging-terraform-state` or `alias/prod-terraform-state`)
- [ ] GitHub OIDC provider — already registered (account-level, shared across all environments)
- [ ] NAT Gateway IPs allowlisted in Salesforce, NetSuite, and MySQL RDS SG — **do after Terraform apply**

### Step 11.2 — Copy and update tfvars for the new environment

```bash
cp infrastructure/environments/staging/terraform.tfvars.example \
  infrastructure/environments/staging/terraform.tfvars

# Edit staging/terraform.tfvars and update:
#   alert_email                   = "staging-ops@yourcompany.com"
#   github_org                    = "your-github-org"
#   extraction_pipeline_lambda_arn     = "arn:aws:lambda:...:staging-extraction-pipeline"
#   transformation_pipeline_lambda_arn = "arn:aws:lambda:...:staging-transformation-pipeline"
#   entity_resolution_lambda_arn       = "arn:aws:lambda:...:staging-entity-resolution-pipeline"
#   analytics_publisher_lambda_arn     = "arn:aws:lambda:...:staging-analytics-layer-publisher"
#   serving_store_loader_lambda_arn    = "arn:aws:lambda:...:staging-serving-store-loader"
#
# For prod, copy from infrastructure/environments/prod/terraform.tfvars.example
# and use prod-* Lambda function ARNs.
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

This creates VPC, S3 buckets, IAM roles, DynamoDB tables, Secrets Manager paths, CloudWatch alarms, Glue catalog, and the five Lambda function stubs. The orchestration module apply in this pass fails only if Lambda ARNs are not yet set — that is expected and handled in Step 11.5.

> **Note:** If you see `Error: No value for required variable — var.transformation_pipeline_lambda_arn`, this is expected at this stage. Proceed to Step 11.5.

### Step 11.5 — Deploy Lambdas and collect ARNs

Repeat [Phase 3](#6-phase-3--application-deployment-lambda) targeting `infrastructure/environments/staging`.

Collect the five ARNs:

```bash
cd infrastructure/environments/staging

# Extraction Lambda
terraform output extraction_lambda_arn

# Remaining four
aws lambda get-function --function-name staging-transformation-pipeline \
  --query Configuration.FunctionArn --output text

aws lambda get-function --function-name staging-entity-resolution \
  --query Configuration.FunctionArn --output text

aws lambda get-function --function-name staging-analytics-publisher \
  --query Configuration.FunctionArn --output text

aws lambda get-function --function-name staging-serving-store-loader \
  --query Configuration.FunctionArn --output text
```

Add them to `infrastructure/environments/staging/terraform.tfvars`:

```hcl
extraction_pipeline_lambda_arn     = "arn:aws:lambda:us-east-1:ACCOUNT_ID:function:staging-extraction-pipeline"
transformation_pipeline_lambda_arn = "arn:aws:lambda:us-east-1:ACCOUNT_ID:function:staging-transformation-pipeline"
entity_resolution_lambda_arn       = "arn:aws:lambda:us-east-1:ACCOUNT_ID:function:staging-entity-resolution"
analytics_publisher_lambda_arn     = "arn:aws:lambda:us-east-1:ACCOUNT_ID:function:staging-analytics-publisher"
serving_store_loader_lambda_arn    = "arn:aws:lambda:us-east-1:ACCOUNT_ID:function:staging-serving-store-loader"
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

Repeat [Step 5.1](#step-51--populate-source-credentials-in-secrets-manager) with staging-specific credentials. The secret paths are:
```
staging/sources/salesforce/credentials
staging/sources/netsuite/credentials
staging/sources/mysql-rds/credentials
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
for entity in salesforce-account salesforce-contact netsuite-customer mysql-rds-orders; do
  source_id=$(echo ${entity} | cut -d'-' -f1,2 | sed 's/-[^-]*$//')
  # Repeat trigger_extraction.py --create-schedule calls from Phase 4, Step 4.4 with --environment staging
done
```

### Step 11.11 — Production promotion checklist

Before applying to production, confirm all of the following:

- [ ] All staging extraction runs completed without failures for at least 5 days
- [ ] All 5 pipeline stages (Extraction → Transformation → EntityResolution → Analytics → ServingStore) succeeded at least once in staging
- [ ] Schema drift reports reviewed — no outstanding breaking drift events
- [ ] Quality gate (`is_publication_blocked`) never fired unexpectedly in staging
- [ ] `terraform plan` on prod shows only expected changes (no destructive resource replacements)
- [ ] Lambda ARNs for prod environment added to `prod/terraform.tfvars`
- [ ] NAT Gateway IPs for prod allowlisted in Salesforce, NetSuite, and MySQL RDS SG
- [ ] SNS subscription confirmed for production alert email
- [ ] Manual approval gate in CI/CD pipeline signed off by platform lead
- [ ] Runbook for production incident response reviewed and current

---

## 13. Verification Checklist

After completing all phases, verify the full deployment is healthy.

### Infrastructure

```bash
# All DynamoDB tables exist
aws dynamodb list-tables --region us-east-1 | grep edl

# All S3 buckets exist and have encryption enabled
aws s3api list-buckets --query "Buckets[?contains(Name,'edl')]"

# All five Lambda functions deployed and active
for fn in extraction-pipeline transformation-pipeline entity-resolution analytics-publisher serving-store-loader; do
  aws lambda get-function \
    --function-name "dev-${fn}" \
    --region us-east-1 \
    --query "Configuration.[FunctionName,State]" \
    --output text
done

# Step Functions state machine is ACTIVE
aws stepfunctions list-state-machines \
  --query "stateMachines[?contains(name,'dev')].[name,type,creationDate]" \
  --output table \
  --region us-east-1

# EventBridge schedule group exists
aws scheduler list-schedule-groups \
  --query "ScheduleGroups[?contains(Name,'dev')].[Name,State]" \
  --output table \
  --region us-east-1

# Secrets exist (do not verify values here)
aws secretsmanager list-secrets --region us-east-1 \
  --query "SecretList[?contains(Name,'dev/sources')].[Name]"
```

### Entity configuration

```bash
# Confirm at least one entity config record exists
aws dynamodb scan \
  --table-name dev-entity-extraction-config \
  --select COUNT \
  --region us-east-1
```

### Field mapping

```bash
# Confirm the latest pointer exists for all configured entities
for entity in salesforce/salesforce-account salesforce/salesforce-contact netsuite/netsuite-customer mysql-rds/mysql-rds-orders; do
  echo "--- ${entity} ---"
  aws s3 ls "s3://dev-edl-mapping-config/field-mappings/${entity}/"
done
```

### Entity resolution config

```bash
# Confirm configs exist for all entity types
for entity in company person; do
  echo "--- ${entity} ---"
  aws s3 ls "s3://dev-edl-curated/entity-resolution/${entity}/"
done

# Verify latest.json pointer is populated
aws s3 cp s3://dev-edl-curated/entity-resolution/company/latest.json -
aws s3 cp s3://dev-edl-curated/entity-resolution/person/latest.json -
```

### Full pipeline end-to-end test

Trigger one complete run through all five stages:

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

**Expected CloudWatch log events across all five stages (in order):**

| Stage | Lambda | Expected log event |
|---|---|---|
| Extraction | `dev-extraction-pipeline` | `run_complete` with `status: success` |
| Transformation | `dev-transformation-pipeline` | `transformation_complete` with `curated_record_count > 0` |
| Entity Resolution | `dev-entity-resolution` | `golden_record_published` with `cluster_count > 0` |
| Analytics | `dev-analytics-publisher` | `analytics_publish_complete` |
| Serving Store | `dev-serving-store-loader` | `serving_load_complete` |

**Verify S3 outputs at each stage:**

```bash
# Stage A — Raw Parquet
aws s3 ls s3://dev-edl-raw/salesforce/salesforce-account/ --recursive | head -5

# Stage B — Curated Parquet + quality report
aws s3 ls s3://dev-edl-curated/curated/customer/salesforce-account/ --recursive | head -5

# Stage C/D — Golden records
aws s3 ls s3://dev-edl-analytics/canonical/ --recursive | head -5

# Stage E — Analytics layer
aws s3 ls s3://dev-edl-analytics/ --recursive | head -5
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
  --alarm-name-prefix "dev-edl" \
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
| `terraform apply` fails — "No value for required variable: transformation_pipeline_lambda_arn" | Lambda ARNs not yet set in tfvars | Deploy Lambdas first (Phase 3), collect ARNs, then re-apply (see [Section 2.5](#25-five-pipeline-lambda-arns-for-step-functions-state-machine)) |
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
