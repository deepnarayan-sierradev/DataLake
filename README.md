# Enterprise Data Lake Platform

Metadata-driven, connector-based, multi-tenant-aware extraction platform built on AWS.

**Status:** Dev deployed and pipeline verified live ✅ | Staging 🔲 | Production 🔲 — Salesforce and
MySQL RDS are connected with real data flowing end-to-end in dev; Sage Intacct, Sage X3, and
NetSuite are fully implemented but not yet connected (empty credential shells). See
[docs/PLATFORM_STATUS.md](docs/PLATFORM_STATUS.md) for the current, detailed state.

If you're using Claude Code or another AI coding agent on this repo, it reads root `CLAUDE.md`
automatically — that file, plus `infrastructure/CLAUDE.md` and `connector_runtime/CLAUDE.md`, are
the agent-facing counterpart to the Developer Guide below and capture the same conventions in a
form meant to survive across sessions.

## Documentation

| Document | Audience | Description |
|---|---|---|
| [docs/DEVELOPER_GUIDE.md](docs/DEVELOPER_GUIDE.md) | Engineers | First-time setup, running tests, triggering pipelines, known gotchas |
| [docs/PLATFORM_STATUS.md](docs/PLATFORM_STATUS.md) | Everyone | Current deployment state, live data, all AWS resource names |
| [docs/PIPELINE_FLOW.md](docs/PIPELINE_FLOW.md) | Engineers, architects, on-call | Full pipeline architecture, stage-by-stage reference, canonical tenant-isolation model |
| [docs/KNOWN_GAPS_AND_ROADMAP.md](docs/KNOWN_GAPS_AND_ROADMAP.md) | Engineers, architects | What's missing, broken, or deferred — the single source for open work |
| [docs/DEPLOYMENT_GUIDE.md](docs/DEPLOYMENT_GUIDE.md) | Platform engineers | Environment deployment (staging/prod), field mapping, AWS settings |
| [docs/PRODUCTION_INCIDENT_RUNBOOK.md](docs/PRODUCTION_INCIDENT_RUNBOOK.md) | On-call engineers | Incident response, runbooks per failure scenario, including cross-tenant incidents |
| [docs/GO_LIVE_READINESS_CHECKLIST.md](docs/GO_LIVE_READINESS_CHECKLIST.md) | Platform engineers, leadership | Go-live gate checklist |
| [docs/SAGE_ERP_IMPLEMENTATION_PLAN.md](docs/SAGE_ERP_IMPLEMENTATION_PLAN.md) | Engineers | Sage Intacct/X3 connector reference — open items, operational commands, new-product recipe |
| [docs/COST_ANALYSIS_AND_ROI.md](docs/COST_ANALYSIS_AND_ROI.md) | Finance, leadership | AWS resource cost breakdown and ROI model |
| [docs/FAQ_FOR_MANAGEMENT.md](docs/FAQ_FOR_MANAGEMENT.md) | Management | Common questions, plain-language answers |
| [docs/EXECUTIVE_OVERVIEW.md](docs/EXECUTIVE_OVERVIEW.md) | Engineering & product leadership | Deep-dive functional walkthrough, compliance, security |
| [docs/GLOSSARY_AND_TERMINOLOGY.md](docs/GLOSSARY_AND_TERMINOLOGY.md) | All | Term definitions, canonical AWS-services reference table |
| [DataLake_Configuration_Module_Requirements.md](DataLake_Configuration_Module_Requirements.md) | Architects | Requirements for a planned, separate self-service configuration service (not yet built) |

## Connector Credentials (AWS Secrets Manager)

Most connector credentials are loaded from AWS Secrets Manager using this path pattern:

`edl/sources/{source_id}/credentials`

Not environment-prefixed — each environment (dev/staging/prod) is deployed to its own AWS
account, so the secret path doesn't need to disambiguate environment within a single account.

**Sage is the one exception** — it has an extra `{product_name}` segment because it has two
distinct products (Intacct and X3) with separate credentials: `edl/sources/sage/{product_name}/credentials`.

| Source | Secret ID | Status | Entities | Required JSON keys |
|---|---|---|---|---|
| Salesforce | `edl/sources/salesforce/credentials` | ✅ Connected, real data flowing | Account, Contact, Contract, Opportunity | `instance_url`, `client_id`, `client_secret` |
| MySQL RDS | `edl/sources/mysql-rds/credentials` | ✅ Connected, real data flowing | Contracts, ContractTerms | `host`, `port`, `username`, `password`, `database` |
| Sage Intacct | `edl/sources/sage/intacct/credentials` | 🟡 Code-complete, not connected | Customer, Vendor, AR Invoice, AP Bill | `token_url`, `client_id`, `client_secret`, `base_url`, `company_id` |
| Sage X3 | `edl/sources/sage/x3/credentials` | 🟡 Code-complete, not connected | Customer, Supplier | `token_url`, `client_id`, `client_secret`, `base_url`, `folder` |
| NetSuite | `edl/sources/netsuite/credentials` | 🟡 Code-complete, not connected | Customer | `account_id`, `consumer_key`, `consumer_secret`, `token_id`, `token_secret` |

All five secrets above are Terraform-managed (`infrastructure/modules/secrets/main.tf` creates
the empty secret shells with a resource policy restricting reads to the extraction runtime role).
See [docs/PLATFORM_STATUS.md](docs/PLATFORM_STATUS.md) for the current status and
[docs/DEPLOYMENT_GUIDE.md](docs/DEPLOYMENT_GUIDE.md) for `aws secretsmanager put-secret-value`
examples.

## Development Setup

Full setup, prerequisites, and local verification commands live in
[docs/DEVELOPER_GUIDE.md](docs/DEVELOPER_GUIDE.md) — this is the canonical source; don't duplicate
its steps elsewhere. Quick start:

```bash
pyenv install 3.14.6
python -m venv .venv && source .venv/bin/activate
pip install --upgrade pip hatchling && pip install -e ".[dev]"
pytest --cov --cov-fail-under=80
```
