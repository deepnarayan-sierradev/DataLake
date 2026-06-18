# Enterprise Data Lake Platform

Metadata-driven, connector-based extraction platform built on AWS.

## Documentation

| Document | Audience | Description |
|---|---|---|
| [docs/BEGINNER_GUIDE.md](docs/BEGINNER_GUIDE.md) | New developers, anyone new to the codebase | End-to-end walkthrough of every step, module map, design principles |
| [docs/EXECUTIVE_OVERVIEW.md](docs/EXECUTIVE_OVERVIEW.md) | Leadership, stakeholders | Functional walkthrough, schedules, access model, compliance, roadmap |
| [docs/PLATFORM_FLOW.md](docs/PLATFORM_FLOW.md) | Engineers, architects, on-call | Step-by-step technical flow, module responsibilities, runbook reference |
| [docs/DEPLOYMENT_GUIDE.md](docs/DEPLOYMENT_GUIDE.md) | Platform engineers | Step-by-step deployment, field mapping setup, AWS settings reference |
| [Enterprise_Data_Lake_Platform_Full_Specification.md](Enterprise_Data_Lake_Platform_Full_Specification.md) | All | Full platform specification (source of truth) |
| [Implementation_Plan_Phase_Wise.md](Implementation_Plan_Phase_Wise.md) | Engineering | Phase-by-phase delivery plan with acceptance criteria |

## Connector Credentials (AWS Secrets Manager)

All connector credentials are loaded from AWS Secrets Manager using this path pattern:

`{environment}/sources/{source_id}/credentials`

| Source | Secret ID example (`environment=dev`) | Required JSON keys |
|---|---|---|
| Salesforce | `dev/sources/salesforce/credentials` | `instance_url`, `client_id`, `client_secret` |
| NetSuite | `dev/sources/netsuite/credentials` | `account_id`, `consumer_key`, `consumer_secret`, `token_id`, `token_secret` |
| MySQL RDS | `dev/sources/mysql-rds/credentials` | `host`, `port`, `username`, `password`, `database` |

See [docs/DEPLOYMENT_GUIDE.md](docs/DEPLOYMENT_GUIDE.md#step-61--populate-source-credentials-in-secrets-manager) for `aws secretsmanager put-secret-value` examples.

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
