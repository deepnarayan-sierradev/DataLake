# Enterprise Data Lake Platform

Metadata-driven, connector-based extraction platform built on AWS.

**Status:** Dev environment live ✅ | Staging 🔲 | Production 🔲

## Documentation

| Document | Audience | Description |
|---|---|---|
| [docs/DEVELOPER_GUIDE.md](docs/DEVELOPER_GUIDE.md) | Engineers | First-time setup, running tests, triggering pipelines, known gotchas |
| [docs/PLATFORM_STATUS.md](docs/PLATFORM_STATUS.md) | Everyone | Current deployment state, live data, all AWS resource names |
| [docs/PIPELINE_FLOW.md](docs/PIPELINE_FLOW.md) | Engineers, architects, on-call | Full pipeline architecture, stage-by-stage reference |
| [docs/DEPLOYMENT_GUIDE.md](docs/DEPLOYMENT_GUIDE.md) | Platform engineers | Environment deployment (staging/prod), field mapping, AWS settings |
| [docs/PRODUCTION_INCIDENT_RUNBOOK.md](docs/PRODUCTION_INCIDENT_RUNBOOK.md) | On-call engineers | Incident response, runbooks per failure scenario |
| [docs/LEADERSHIP_BRIEF.md](docs/LEADERSHIP_BRIEF.md) | CTO, CIO, VP, Finance | What was built, current status, ROI, roadmap |
| [docs/EXECUTIVE_OVERVIEW.md](docs/EXECUTIVE_OVERVIEW.md) | Engineering & product leadership | Deep-dive functional walkthrough, compliance, security |
| [docs/GLOSSARY_AND_TERMINOLOGY.md](docs/GLOSSARY_AND_TERMINOLOGY.md) | All | Term definitions |
| [Enterprise_Data_Lake_Platform_Full_Specification.md](Enterprise_Data_Lake_Platform_Full_Specification.md) | All | Full platform specification (source of truth) |

## Connector Credentials (AWS Secrets Manager)

All connector credentials are loaded from AWS Secrets Manager using this path pattern:

`{environment}/sources/{source_id}/credentials`

| Source | Secret ID example (`environment=dev`) | Status | Required JSON keys |
|---|---|---|---|
| Salesforce | `dev/sources/salesforce/credentials` | ✅ Connected | `instance_url`, `client_id`, `client_secret` |
| MySQL RDS | `dev/sources/mysql-rds/credentials` | ✅ Connected | `host`, `port`, `username`, `password`, `database` |
| NetSuite | `dev/sources/netsuite/credentials` | 🔲 Pending | `account_id`, `consumer_key`, `consumer_secret`, `token_id`, `token_secret` |

See [docs/DEPLOYMENT_GUIDE.md](docs/DEPLOYMENT_GUIDE.md) for `aws secretsmanager put-secret-value` examples.

## Development Setup

Requires Python 3.14.6. See [Developer Setup](#developer-setup) below.

### Prerequisites

- macOS (Apple Silicon or Intel) / Linux
- [pyenv](https://github.com/pyenv/pyenv) — manages the Python version
- Xcode Command Line Tools: `xcode-select --install`

### Developer Setup

```bash
# 1. Install Python 3.14.6 via pyenv
pyenv install 3.14.6

# 2. Create and activate the virtual environment
python -m venv .venv
source .venv/bin/activate

# 3. Install the project and all dev dependencies
pip install --upgrade pip hatchling
pip install -e ".[dev]"

# 4. Run the full test suite
pytest --cov --cov-fail-under=80

# 5. Run linting and type checks
ruff check .
mypy .
```

### Running CI checks locally

```bash
# Lint
ruff check . --output-format=github

# Type check
mypy .

# Tests with coverage
pytest --cov --cov-report=term-missing --cov-fail-under=80

# Security scan
bandit -r . -c pyproject.toml

# Dependency CVE scan
pip-audit
```
