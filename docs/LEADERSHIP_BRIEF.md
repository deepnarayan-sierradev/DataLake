# Enterprise Data Lake — Leadership Brief

**For:** CTO, CIO, VP Engineering, Product Leadership, Finance  
**Last updated:** 2026-07-09  
**Read time:** 8 minutes

---

## What We Built and Why

### The Problem

Before this platform, our company's data lived in three completely disconnected systems:

| System | What it held | Who used it |
|---|---|---|
| **Salesforce CRM** | Customer accounts, contacts | Sales, Customer Success |
| **MySQL RDS** | Transactional contracts and orders | Operations, Finance |
| **NetSuite ERP** | Financials, invoices | Finance *(pending onboarding)* |
| **Sage Intacct** | Accounts receivable invoices, AP bills, vendors | Finance |
| **Sage X3** | Supplier records, ERP data | Finance, Procurement |

Getting data out of these systems for analysis required:
- **Manual extraction scripts** — brittle, break when the source schema changes
- **24–72 hour delays** — by the time data reached analysts it was already stale
- **No audit trail** — no record of who accessed what, when, or why
- **Inconsistent customer identity** — the same customer could appear as three different records across three systems
- **Security risk** — credentials stored in scripts, shared informally, no rotation

### The Solution

An **Enterprise Data Lake Platform**: a security-first, metadata-driven data pipeline, designed
to run fully automated on a schedule, that:

- Extracts data from source systems on a scheduled cadence (currently run manually in Dev; see
  Current Status below)
- Stores versioned, immutable copies across three governed data layers
- Resolves the same customer across multiple systems into a single **golden record**
- Enforces PII masking and data classification automatically
- Provides a complete audit trail from source record to business insight
- Requires **zero code changes** to add a new data source

---

## Current Status: Dev Environment Deployed, Two of Five Sources Live

As of **2026-07-09**, only the Dev AWS environment exists, and it was rebuilt from scratch that
day (an earlier "live" claim for this account had gone stale — the account was found empty and
redeployed). Of five planned source connectors, two — Salesforce and MySQL RDS — have real
credentials in place and have run end-to-end with real data. The other three (Sage Intacct, Sage
X3, NetSuite) are code-complete but have no credentials populated, so nothing has run for them.

### Live Data

| Data | Records | Queryable via |
|---|---|---|
| Salesforce Account records (companies) | **34** | AWS Athena |
| Salesforce Contact records (persons) | Extracted; exact count not re-confirmed this pass | AWS Athena |
| Contract records (from MySQL RDS) | **36,023** | AWS Athena |

Anyone with Athena access can run standard SQL to query this data today. The figures above are
from a verified pipeline run on 2026-07-09; other entity counts (opportunities, contracts,
suppliers) will exist once their sources are seeded and run — see "What's Next" below.

### What Runs Today, and What's Still Manual

The extraction → transformation → entity resolution → analytics pipeline has been verified
end-to-end for Salesforce and MySQL RDS, triggered manually rather than on an enabled recurring
schedule — no entity-level EventBridge schedule is turned on in Dev yet. Each stage:

1. **Extraction** — Lambda reads from the source using credentials in Secrets Manager
2. **Transformation** — Field mapping, quality checks, PII masking applied
3. **Entity resolution** — Cross-source matching produces one golden record per customer
4. **Analytics delivery** — Clean, partitioned data lands in Athena-queryable tables

If any step fails: automatic retry with exponential backoff → alerting → dead-letter queue for
replay. Turning on recurring schedules, running unattended for a sustained period, and validating
SLOs under real traffic are still ahead of us — see "What's Next."

---

## Deployment Roadmap

| Environment | Status | ETA |
|---|---|---|
| **Dev** | ✅ Deployed; 2 of 5 sources connected and verified live | Done (redeployed 2026-07-09) |
| **Staging** | 🔲 Not provisioned — no AWS account/credentials yet | TBD |
| **Production** | 🔲 Not provisioned — pending staging sign-off | TBD |

---

## Business Outcomes Delivered

| Metric | Before | After |
|---|---|---|
| Time to data availability | 24–72 hours (manual) | 1–4 hours (automated) |
| Customer identity resolution | 3 disconnected views | Single golden record per customer |
| PII exposure in analytics | Uncontrolled | Masked/tokenised at pipeline level |
| Audit trail | None | Full lineage from source to serving |
| New source onboarding | 2–4 weeks (code change + deployment) | 2–3 days (configuration only) |
| Credential security | Scripts and shared .env files | AWS Secrets Manager with daily expiry-check alerting (auto-rotation planned) |
| Compliance readiness | Manual documentation | Automated lineage + retention enforcement |
| Data quality visibility | No monitoring | Quality report per entity per run |

These reflect the pipeline's designed and implemented behavior, demonstrated so far for the two
connected sources in Dev — not yet proven across all five sources, at production scale, or with
a second tenant.

---

## Cost Summary

### Monthly AWS Infrastructure Costs (Dev → Production estimate)

Estimates, not measured production bills — Dev has run a handful of manually-triggered pipeline
runs against two sources, not sustained production-scale traffic.

| Component | Monthly Cost |
|---|---|
| S3 storage (raw, curated, analytics) | ~$120 |
| Lambda execution (200 runs/month) | ~$80 |
| DynamoDB (5 tables, on-demand) | ~$150 |
| Athena queries | ~$25 |
| Secrets Manager | ~$9 |
| CloudWatch logs and metrics | ~$70 |
| VPC endpoints | ~$15 |
| **Total monthly AWS** | **~$469/month** |

### ROI

| Comparison | Cost |
|---|---|
| This platform (AWS + engineering) | ~$469/month infrastructure |
| Commercial SaaS alternative (e.g. Fivetran) | $3,000–$5,000/month *per source* |
| Manual engineering equivalent | 40–60 hrs/month in avoided labour |

**Payback period:** Infrastructure cost is recovered within the first month relative to SaaS alternatives, and within 2–3 months relative to manual engineering costs.

---

## Security and Compliance

- **Least privilege IAM** — each Lambda has its own role scoped to exactly the resources it needs
- **No credentials in code** — all secrets stored in AWS Secrets Manager; a daily automated check alerts the platform team via SNS if rotation is overdue (automatic rotation itself is a planned follow-up, not yet implemented)
- **PII masking at pipeline level** — sensitive fields masked before they reach the analytics layer
- **Immutable raw layer** — S3 Object Lock prevents accidental or malicious deletion
- **Full audit trail** — every pipeline run writes a lineage record (who, what, when, from where, to where)
- **Encryption at rest and in transit** — S3 SSE-KMS, TLS for all API calls
- **OWASP Top 10 controls** — applied from initial implementation; Bandit SAST and pip-audit in CI pipeline

---

## What's Next

Ordered roughly as they gate one another:

| Item | Description |
|---|---|
| **Populate Sage Intacct, Sage X3, NetSuite credentials** | All three connectors are code-complete; none has real credentials yet, so none has run |
| **Seed and schedule the newer Salesforce/MySQL entities** | Opportunity and Contract (Salesforce), Contract Terms (MySQL RDS) are config-complete but not yet seeded or scheduled |
| **Verify the control-plane API end-to-end** | The Cognito-authenticated tenant/entity API is deployed in Dev; a live login/JWT round-trip against it has not yet been exercised |
| **Pilot tenant onboarding** | Onboard one real second tenant and run it alongside the default tenant for about a week with no cross-tenant incidents — not yet started |
| **Load test at target scale** | Not yet started |
| **Staging deployment** | Requires its own AWS account; `terraform validate` is already clean for staging |
| **Production deployment** | Gated on staging sign-off; no AWS account exists for production yet |
| **Self-service analytics** | Business intelligence tooling on top of Athena (e.g. QuickSight, Tableau) |
| **Data quality dashboards** | CloudWatch-based dashboards surfacing per-entity quality scores to stakeholders |

---

## Technical Architecture (High Level)

```
Source Systems                 Data Lake Layers              Analytics
──────────────                 ────────────────              ─────────
Salesforce CRM  ── live ─────► Raw Layer (S3)                Athena SQL (live)
MySQL RDS       ── live ─────► Curated Layer (S3) ─────────► QuickSight (future)
Sage Intacct    ── pending ──► Analytics Layer (S3)          BI Tools (future)
Sage X3         ── pending ──►
NetSuite ERP    ── pending ──►

"Live" = real credentials populated, pipeline run end-to-end with real data (Dev only).
"Pending" = connector code-complete, no credentials populated yet, nothing has run.

Orchestration: EventBridge → Step Functions → Lambda (4 stages); entity-level schedules
               not yet enabled — today's live runs were triggered manually.
Governance:    Glue Catalog + DynamoDB lineage records
Security:      IAM least-privilege + KMS encryption + Secrets Manager
```

---

## Key Contacts

| Role | Responsibility |
|---|---|
| Platform Engineering | Infrastructure, Lambda code, Terraform |
| Data Engineering | Field mappings, entity configs, source onboarding |
| Security / Compliance | IAM policy review, PII classification, audit |
| Finance | AWS cost monitoring, ROI tracking |
