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
| [docs/REMEDIATION_PASS_HANDOFF.md](docs/REMEDIATION_PASS_HANDOFF.md) | Engineers picking up this work | What the 2026-07-29 re-assessment found, the two new gates, and what is still open |
| [docs/SCALE_AND_DLQ_THRESHOLDS.md](docs/SCALE_AND_DLQ_THRESHOLDS.md) | Engineers, on-call, architects | The 12-month scale target (10–20 tenants × 5–12 sources × 100+ entities) and every DLQ alarm threshold derived from it — read before changing an alarm number |
| [docs/DEPLOYMENT_GUIDE.md](docs/DEPLOYMENT_GUIDE.md) | Platform engineers | Environment deployment (staging/prod), field mapping, AWS settings |
| [docs/PRODUCTION_INCIDENT_RUNBOOK.md](docs/PRODUCTION_INCIDENT_RUNBOOK.md) | On-call engineers | Incident response, runbooks per failure scenario, including cross-tenant incidents |
| [docs/GO_LIVE_READINESS_CHECKLIST.md](docs/GO_LIVE_READINESS_CHECKLIST.md) | Platform engineers, leadership | Go-live gate checklist |
| [docs/SAGE_ERP_IMPLEMENTATION_PLAN.md](docs/SAGE_ERP_IMPLEMENTATION_PLAN.md) | Engineers | Sage Intacct/X3 connector reference — open items, operational commands, new-product recipe |
| [docs/SOURCE_API_FIDELITY_AUDIT.md](docs/SOURCE_API_FIDELITY_AUDIT.md) | Engineers | Every REST source checked against its vendor documentation (2026-07-29) — what each API actually is, its real rate limits, and what is still inferred rather than documented. Read before writing or editing a source spec |
| [docs/COST_ANALYSIS_AND_ROI.md](docs/COST_ANALYSIS_AND_ROI.md) | Finance, leadership | AWS resource cost breakdown and ROI model |
| [docs/FAQ_FOR_MANAGEMENT.md](docs/FAQ_FOR_MANAGEMENT.md) | Management | Common questions, plain-language answers |
| [docs/EXECUTIVE_OVERVIEW.md](docs/EXECUTIVE_OVERVIEW.md) | Engineering & product leadership | Deep-dive functional walkthrough, compliance, security |
| [docs/GLOSSARY_AND_TERMINOLOGY.md](docs/GLOSSARY_AND_TERMINOLOGY.md) | All | Term definitions, canonical AWS-services reference table |
| [DataLake_Configuration_Module_Requirements.md](DataLake_Configuration_Module_Requirements.md) | Architects | Requirements for a planned, separate self-service configuration service (not yet built) |
| [docs/WIRING_PASS_HANDOFF.md](docs/WIRING_PASS_HANDOFF.md) | Engineers | Session handoff for the 2026-07-28 wiring pass — the six CI gates, what was wired, corrected findings, ordering hazards, and what awaits approval |
| [requirements/WAIVERS.md](requirements/WAIVERS.md) | Engineers | Every deliberate exception to the wiring gates, with its reason. The gates fail on a stale entry, so this file cannot drift |
| [requirements/README.md](requirements/README.md) | Engineers, architects | **SOW requirements programme (DL-01…DL-12)** — one document per phase, the implementation plan, and the cross-repo interface contract. Built as of 2026-07-28 except the deferred DL-04 (AI agent runtime) and DL-05 (ML platform); nothing is applied to AWS yet |

## System boundary

This repository is a **standalone, configuration-driven data-lake processing system**. It does not
own tenants, users, roles, or permissions — those belong to the Identity API that
`enterprise-platform` is built on. DataLake reads the configuration the enterprise-platform
publishes (entity settings, field mappings, entity-resolution and survivorship rules, entity-type
registry, semantic definitions, schedules, connections, scope model) and acts on it. There is
deliberately no tenant-provisioning endpoint here, and a test asserts its absence.

## Connector Credentials (AWS Secrets Manager)

Credentials are **per connection**:

`edl/tenants/{tenant_code}/connections/{connection_id}/credentials`

resolved through `connector_runtime/connection_credential_resolver.py`. Write-back uses a separate
`...-writeback` secret so a read-only deployment cannot mutate a source.

For a single-connection source, `connection_id == source_id`. The legacy shared path below is still
read as a **fallback with a warning** until each environment has run `make migrate-credentials`
(dry-run by default) and then `--delete-legacy`.

Not environment-prefixed — each environment (dev/staging/prod) is deployed to its own AWS
account, so the secret path doesn't need to disambiguate environment within a single account.

### Legacy shared paths (still in place in dev until the migration runs)

**Sage is the one exception** — it has an extra `{product_name}` segment because it has two
distinct products (Intacct and X3) with separate credentials: `edl/sources/sage/{product_name}/credentials`.

| Source | Secret ID | Status | Entities | Required JSON keys |
|---|---|---|---|---|
| Salesforce | `edl/sources/salesforce/credentials` | ✅ Connected, real data flowing | Account, Contact, Contract, Opportunity | `instance_url`, `client_id`, `client_secret` |
| MySQL RDS | `edl/sources/mysql-rds/credentials` | ✅ Connected, real data flowing | Contracts, ContractTerms | `host`, `port`, `username`, `password`, `database` |
| Sage Intacct | `edl/sources/sage/intacct/credentials` | 🟡 Code-complete, not connected | Customer, Vendor, AR Invoice, AP Bill | `token_url`, `client_id`, `client_secret`, `base_url`, `company_id` |
| Sage X3 | `edl/sources/sage/x3/credentials` | 🟡 Code-complete, not connected | Customer, Supplier | `token_url`, `client_id`, `client_secret`, `base_url`, `folder` |
| NetSuite | `edl/sources/netsuite/credentials` | 🟡 Code-complete, not connected | Customer | `account_id`, `consumer_key`, `consumer_secret`, `token_id`, `token_secret` |

The ten SOW sources (HubSpot, MaidCentral, ServMan Pro, WellSky, Housecall Pro, Dialpad,
SeniorPlace, Google Ads, Google Analytics, Meta Ads) are implemented as declarative REST specs and
read their credentials from the per-connection path above. None is connected yet.

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
