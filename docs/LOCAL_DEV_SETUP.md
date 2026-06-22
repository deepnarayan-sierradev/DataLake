# Local Development Setup (VS Code)

Here's everything you need to run locally from VS Code.

## 1. Install dependencies

```bash
cd /Users/deepnarayan/DataLake
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

If `.venv` already exists, just activate it and run:

```bash
pip install -e ".[dev]"
```

## 2. Run local tests for extraction flow

Run the connector/config/handler flow tests:

```bash
.venv/bin/python -m pytest \
  connector_runtime/tests/test_extraction_pipeline_handler.py \
  connector_runtime/tests/test_configuration_repository.py \
  connector_runtime/tests/salesforce \
  connector_runtime/tests/netsuite \
  connector_runtime/tests/mysql_rds \
  -v --no-cov
```

Run orchestration workflow tests:

```bash
.venv/bin/python -m pytest orchestration/tests -v --no-cov
```

Run remaining platform tests:

```bash
.venv/bin/python -m pytest \
  transformation/tests schema_management/tests watermark_management/tests \
  observability/tests contracts/tests entity_resolution/tests governance/tests \
  -v --no-cov
```

## 3. Required runtime environment variables

Set these before invoking the extraction handler locally:

```bash
export AWS_REGION=us-east-1
export RAW_S3_BUCKET=dev-raw-layer
export SCHEMA_SNAPSHOT_S3_BUCKET=dev-schema-snapshots
```

`environment` (dev/staging/prod) is passed in the event payload, not from an env var.

## 4. Connector credentials in Secrets Manager

Credentials are not passed in event payloads or env vars. They are loaded by each connector from AWS Secrets Manager:

| Source | Secret ID (`environment=dev`) | Required JSON keys |
|---|---|---|
| Salesforce | `dev/sources/salesforce/credentials` | `instance_url`, `client_id`, `client_secret` |
| NetSuite | `dev/sources/netsuite/credentials` | `account_id`, `consumer_key`, `consumer_secret`, `token_id`, `token_secret` |
| MySQL RDS | `dev/sources/mysql-rds/credentials` | `host`, `port`, `username`, `password`, `database` |

Example check:

```bash
aws secretsmanager describe-secret --secret-id dev/sources/salesforce/credentials
aws secretsmanager describe-secret --secret-id dev/sources/netsuite/credentials
aws secretsmanager describe-secret --secret-id dev/sources/mysql-rds/credentials
```

## 5. Local Salesforce invocation payload shape

Use this event shape when invoking locally:

```json
{
  "source_id": "salesforce",
  "entity_id": "salesforce-account",
  "environment": "dev",
  "connector_params": {
    "object_name": "Account"
  },
  "is_replay": false
}
```

## 6. AWS CLI installation and dev profile setup

Install AWS CLI (if not installed):

```bash
python3 -m pip install --user awscli
export PATH="$HOME/.local/bin:$PATH"
aws --version
```

Persist PATH (zsh):

```bash
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.zshrc
```

Create a dev profile with defaults:

```bash
aws configure set region us-east-1 --profile dev
aws configure set output json --profile dev
```

Configure credentials for profile `dev`:

```bash
aws configure --profile dev
```

You must enter real credentials at this step:
- AWS Access Key ID
- AWS Secret Access Key

Verify identity:

```bash
export AWS_PROFILE=dev
aws sts get-caller-identity
```

## 7. Run Salesforce connector locally (two ways)

### Way A: True local execution (handler runs on your laptop)

This executes `lambda_handler` directly in your local Python process while using real AWS resources and your real Salesforce org.

```bash
source .venv/bin/activate

# Use either env vars or AWS profile credentials
export AWS_ACCESS_KEY_ID=YOUR_DEV_ACCESS_KEY
export AWS_SECRET_ACCESS_KEY=YOUR_DEV_SECRET_KEY
export AWS_REGION=us-east-1

# Optional if your auth requires temporary session credentials
# export AWS_SESSION_TOKEN=YOUR_SESSION_TOKEN

# Required by extraction_pipeline_handler
export RAW_S3_BUCKET=dev-raw-layer
export SCHEMA_SNAPSHOT_S3_BUCKET=dev-schema-snapshots

# Verify AWS identity
aws sts get-caller-identity

# Verify Salesforce connector secret exists
aws secretsmanager describe-secret --secret-id dev/sources/salesforce/credentials

# Invoke the extraction handler directly
python -c "from connector_runtime.extraction_pipeline_handler import lambda_handler; event={'source_id':'salesforce','entity_id':'salesforce-account','environment':'dev','connector_params':{'object_name':'Account'},'is_replay':False}; print(lambda_handler(event, None))"
```

### Way B: AWS-backed execution (trigger Step Functions from local)

This starts a real Step Functions execution. The connector runs in deployed Lambda (not on your laptop).

```bash
source .venv/bin/activate

# If using profile-based auth:
export AWS_PROFILE=dev
export AWS_REGION=us-east-1

# Verify AWS identity
aws sts get-caller-identity

# Trigger a run
python scripts/trigger_extraction.py \
  --source-id salesforce \
  --entity-id salesforce-account \
  --environment dev \
  --region us-east-1 \
  --param object_name=Account
```

If `state_machine_arn` is not available from Terraform outputs in your local checkout, pass it explicitly:

```bash
python scripts/trigger_extraction.py \
  --source-id salesforce \
  --entity-id salesforce-account \
  --environment dev \
  --region us-east-1 \
  --state-machine-arn arn:aws:states:us-east-1:123456789012:stateMachine:dev-extraction-pipeline \
  --param object_name=Account
```

## 8. Security notes

- Do not commit credentials or secrets.
- Use least-privilege IAM permissions for dev profile.
- If your organization uses SSO, prefer:

```bash
aws configure sso --profile dev
```

---

## 9. Technology Stack Reference

Complete list of tools required or used in local development.

### Required Local Tools

| Tool | Version | Install |
|---|---|---|
| **pyenv** | 2.7.2+ | `brew install pyenv` |
| **Python** | 3.14.x | `pyenv install 3.14.6` |
| **Terraform** | ≥ 1.8, < 2.0 | `brew install terraform` |
| **AWS CLI** | v2 | `brew install awscli` |
| **GNU Make** | ≥ 3.8 | Included on macOS; `brew install make` on Linux |
| **Git** | Latest | `brew install git` |
| **pre-commit** | Latest | `pip install pre-commit` |

### Python Dev Dependencies (installed via `pip install -e ".[dev]"`)

| Package | Version | Purpose |
|---|---|---|
| **pydantic** | ≥ 2.7 | Data model validation; frozen models |
| **structlog** | ≥ 24.4 | Structured JSON logging |
| **boto3** | Latest | AWS SDK for all service calls |
| **pyarrow** | Latest | Parquet read/write |
| **pymysql** | Latest | MySQL RDS connector |
| **requests** | Latest | Salesforce / NetSuite HTTP client |
| **ruff** | ≥ 0.5 | Linter (run: `ruff check .`) |
| **mypy** | ≥ 1.10 | Type checker strict mode (run: `mypy .`) |
| **bandit** | ≥ 1.7 | SAST scanner (run: `bandit -r . -c pyproject.toml`) |
| **pip-audit** | ≥ 2.7 | CVE scan (run: `pip-audit`) |
| **pytest** | Latest | Test runner (run: `pytest --cov --cov-fail-under=80`) |
| **moto** | ≥ 5.0 | AWS service mocking (no real AWS needed for unit tests) |
| **hatchling** | Latest | Build backend (`pyproject.toml`-only) |

### Key AWS Services Used (in Dev Environment)

| Service | Dev environment resource name |
|---|---|
| S3 raw layer | `dev-raw-layer` |
| S3 curated layer | `dev-curated-layer` |
| S3 analytics layer | `dev-analytics-layer` |
| S3 schema snapshots | `dev-schema-snapshots` |
| DynamoDB config table | `dev-entity-extraction-config` |
| DynamoDB watermark | `dev-watermark-repository` |
| DynamoDB audit log | `dev-run-audit-log` |
| Secrets Manager | `dev/sources/{source}/credentials` |
| Step Functions | `dev-extraction-orchestration-workflow` |
| CloudWatch namespace | `EnterpriseDatalake` |

### Run All Local Checks

```bash
source .venv/bin/activate

# Full CI check suite (same as GitHub Actions)
.venv/bin/ruff check .
.venv/bin/mypy .
.venv/bin/pytest --cov --cov-fail-under=80
.venv/bin/bandit -r . -c pyproject.toml
.venv/bin/pip-audit

# Terraform checks
cd infrastructure/environments/dev
terraform validate
```
