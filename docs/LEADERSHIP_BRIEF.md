# Enterprise Data Lake — Leadership Brief

**For:** CTO, CIO, VP Engineering, Product Leadership, Finance  
**Last updated:** 2026-06-29  
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

An **Enterprise Data Lake Platform**: a fully automated, security-first, metadata-driven data pipeline that:

- Extracts data from source systems on a nightly schedule
- Stores versioned, immutable copies across three governed data layers
- Resolves the same customer across multiple systems into a single **golden record**
- Enforces PII masking and data classification automatically
- Provides a complete audit trail from source record to business insight
- Requires **zero code changes** to add a new data source

---

## Current Status: Dev Environment Live ✅

As of **2026-06-29**, the full pipeline is deployed and operational in the dev environment. Real business data is flowing end-to-end.

### Live Data

| Data | Records | Queryable via |
|---|---|---|
| Company golden records (from Salesforce) | **34** | AWS Athena |
| Person golden records (from Salesforce) | **49** | AWS Athena |
| Contract records (from MySQL RDS) | **35,971** | AWS Athena |

Anyone with Athena access can run standard SQL to query this data today — no exports, no scripts, no waiting.

### What "Fully Operational" Means

The following runs automatically, end-to-end, with no manual intervention:

1. **Scheduled trigger** — EventBridge fires a nightly cron job per entity
2. **Extraction** — Lambda reads from Salesforce/MySQL/Sage using secure credentials
3. **Transformation** — Field mapping, quality checks, PII masking applied
4. **Entity resolution** — Cross-source customer matching produces one golden record per customer
5. **Analytics delivery** — Clean, partitioned data lands in Athena-queryable tables

If any step fails: automatic retry with exponential backoff → alerting → dead-letter queue for replay.

---

## Deployment Roadmap

| Environment | Status | ETA |
|---|---|---|
| **Dev** | ✅ Complete | Done |
| **Staging** | 🔲 Next | TBD — requires infrastructure provisioning |
| **Production** | 🔲 Pending staging sign-off | TBD |

---

## Business Outcomes Delivered

| Metric | Before | After |
|---|---|---|
| Time to data availability | 24–72 hours (manual) | 1–4 hours (automated) |
| Customer identity resolution | 3 disconnected views | Single golden record per customer |
| PII exposure in analytics | Uncontrolled | Masked/tokenised at pipeline level |
| Audit trail | None | Full lineage from source to serving |
| New source onboarding | 2–4 weeks (code change + deployment) | 2–3 days (configuration only) |
| Credential security | Scripts and shared .env files | AWS Secrets Manager with auto-rotation |
| Compliance readiness | Manual documentation | Automated lineage + retention enforcement |
| Data quality visibility | No monitoring | Quality report per entity per run |

---

## Cost Summary

### Monthly AWS Infrastructure Costs (Dev → Production estimate)

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
- **No credentials in code** — all secrets stored in AWS Secrets Manager with 90-day auto-rotation
- **PII masking at pipeline level** — sensitive fields masked before they reach the analytics layer
- **Immutable raw layer** — S3 Object Lock prevents accidental or malicious deletion
- **Full audit trail** — every pipeline run writes a lineage record (who, what, when, from where, to where)
- **Encryption at rest and in transit** — S3 SSE-KMS, TLS for all API calls
- **OWASP Top 10 controls** — applied from initial implementation; Bandit SAST and pip-audit in CI pipeline

---

## What's Next

| Item | Description |
|---|---|
| **Staging deployment** | Mirror of dev — validates that the platform promotes cleanly to a production-like environment |
| **Production deployment** | Live production workloads after staging sign-off |
| **NetSuite onboarding** | No code changes needed — configuration-only; adds financial data to the lake |
| **Sage Intacct + Sage X3 onboarding** | Connectors fully implemented; configuration-only activation; adds AR invoices, AP bills, vendor and supplier golden records |
| **Additional Salesforce entities** | Opportunities, Cases — configuration-only additions |
| **Self-service analytics** | Business intelligence tooling on top of Athena (e.g. QuickSight, Tableau) |
| **Data quality dashboards** | CloudWatch-based dashboards surfacing per-entity quality scores to stakeholders |

---

## Technical Architecture (High Level)

```
Source Systems                 Data Lake Layers              Analytics
──────────────                 ────────────────              ─────────
Salesforce CRM ──────────────► Raw Layer (S3)                Athena SQL
MySQL RDS       ── nightly ──► Curated Layer (S3) ─────────► QuickSight (future)
NetSuite ERP    ── pipeline ►  Analytics Layer (S3)          BI Tools (future)Sage Intacct    ─────────┉
Sage X3         ─────────┉(pending)

Orchestration: EventBridge → Step Functions → Lambda (4 stages)
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
