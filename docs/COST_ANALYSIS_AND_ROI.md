# Cost Analysis & Return on Investment (ROI)

**For:** Finance, Leadership, Procurement  
**Document version:** 2.0 — revised 2026-07-09 to correct resource-count errors and remove
unverified savings claims found in v1.0  
**Last updated:** 2026-07-14 (cost figures last revised 2026-07-09; originally 2026-06-29)

> **What's actually deployed today:** Only the `dev` AWS environment exists (rebuilt from scratch
> 2026-07-09). Staging and Production have no AWS account and are not provisioned. Of five source
> connectors, only Salesforce and MySQL RDS are connected with real credentials and have run
> end-to-end (34 Salesforce accounts, 36,023 MySQL RDS rows). No pilot tenant has been onboarded,
> and nothing has run at production volume. **Every cost figure below is an estimate derived from
> the Terraform-defined dev resource shapes, scaled to an assumed steady-state usage volume — none
> of it is an observed AWS bill.** Treat this document as a planning model, not a report of actuals.

---

## Executive Summary

This document estimates that, **once fully deployed** (all five sources connected, a pilot tenant
onboarded, running at the assumed volumes below), the platform would cost roughly **$770–$800/month**
in AWS infrastructure plus staffing, versus an estimated **$8,000/month** in today's manual-labor
baseline. That comparison has **not been measured against a live production workload** — no pilot
tenant exists yet, so the "savings" figures in this document are projections to sanity-check the
business case, not results to report as achieved.

---

## Cost Breakdown

### AWS Infrastructure Monthly Costs

| Component | Usage | Monthly Cost | Notes |
|---|---|---|---|
| **S3 Storage** | Raw: 2.5 TB/mo; Curated: 1 TB/mo; Analytics: 0.5 TB/mo (assumed volume — dev holds a small fraction of this today) | $120 | 7-yr raw retention. Actual Terraform lifecycle rules (`infrastructure/modules/storage/main.tf`): raw transitions to STANDARD_IA at 90 days and GLACIER at 365 days; curated transitions to STANDARD_IA at 180 days. **Correction:** no S3 Intelligent-Tiering is configured anywhere in Terraform — the prior version of this document stated that incorrectly. |
| **S3 Data Transfer** | 4 TB outbound to Athena / analytics tools (assumed) | $180 | Inbound to S3 is free; outbound charges apply. Estimate only — no observed transfer volume yet. |
| **Lambda execution** | ~200 runs/month × 5–10 min avg × 512 MB memory (assumed) | $80 | Streaming architecture keeps memory flat regardless of dataset size |
| **DynamoDB** | 5 tables confirmed in Terraform (`infrastructure/modules/metadata_persistence/main.tf`: watermark repository, run audit log, entity extraction config, entity type registry, source onboarding registry); ~2 GB assumed | $150 | On-demand pricing; easily upgrades to provisioned if DLQ depth grows |
| **Secrets Manager** | 5 secrets confirmed in Terraform (Salesforce, NetSuite, MySQL RDS, Sage Intacct, Sage X3 — `infrastructure/modules/secrets/main.tf`); daily expiry-check alerting (auto-rotation planned, not yet implemented). Only 2 of 5 currently hold real credentials. | $10 | $0.40/secret/month × 5 + retrieval charges |
| **Control plane** (Cognito + API Gateway + Lambda) | SaaS control-plane API for tenant/entity self-service, plus `credential_expiry_notifier`, `pipeline_trigger`, and `dlq_processor` Lambdas | $10–$20 | Rough placeholder at current near-zero traffic — not load-tested, and the API's end-to-end request flow against the live Cognito authorizer has not yet been exercised (per `docs/PLATFORM_STATUS.md`) |
| **CloudWatch Logs** | ~50 MB/day × 30 days (structured logging, assumed) | $30 | Log retention: 30 days in hot storage, then archive to S3 |
| **CloudWatch Metrics & Alarms** | 50 custom metrics + 15 alarm instances (assumed) | $40 | Custom metrics beyond standard Lambda/S3 metrics |
| **AWS Glue Catalog** | Data catalog entries; no compute cost | $0 | Only catalog storage; query compute runs on Athena. Note: the `datalake_curated_dev` Glue database is provisioned but not wired to any Lambda in dev today, so it stays empty regardless of volume (`docs/PLATFORM_STATUS.md`, Glue Catalog section) |
| **Athena queries** | ~500 queries/month × avg 10 GB scanned (assumed) | $25 | $5/TB scanned; analytics layer partitioned to reduce scan volume |
| **VPC Endpoints** | **Corrected.** Terraform (`infrastructure/modules/networking/main.tf` + `variables.tf`) enables 5 **interface** endpoints by default in dev: Secrets Manager, CloudWatch Logs, CloudWatch Monitoring, Step Functions, KMS — each deployed across 2 AZs (`us-east-1a`/`us-east-1b`). S3 and DynamoDB use **Gateway** endpoints, which have no hourly charge. The Glue interface endpoint is disabled by default (`enable_glue_endpoint = false`). The prior version of this document listed "S3, DynamoDB, Secrets Manager, Glue, CloudWatch" as 5 endpoints at $15/month total — that list was wrong on both membership (Glue is off; S3/DynamoDB are free) and the AZ multiplier. | ~$73 | 5 interface endpoints × 2 AZs × $0.01/AZ-hour × ~730 hrs ≈ $73/month, before data-processing charges. This rate ($0.01/AZ-hour) reflects AWS's long-standing PrivateLink interface-endpoint pricing structure — **confirm the current rate on the AWS Pricing Calculator before treating this as precise**, since it was not independently re-verified this pass. |
| **KMS key** | One customer-managed CMK (deduplicated encryption key) | $1 | $1/month per CMK; reduced through key sharing |
| **Step Functions** | 200 executions/month × 16 state transitions (assumed) | $3 | $0.000025/transition |
| **EventBridge Scheduler** | 12 schedules (per entity) × 30 days × 1 invocation/day (assumed) | $1 | $0.10/month per schedule |
| **NAT Gateway** | 1 NAT Gateway, confirmed deployed in dev (`single_nat_gateway = true` in `infrastructure/environments/dev/main.tf`) | $45 | **Not optional — it is already provisioned.** It exists because Salesforce, NetSuite, and Sage are internet-reachable SaaS APIs, not AWS PrivateLink endpoints; the extraction Lambda reaches them from a private subnet via this NAT Gateway's public IP (see `docs/PLATFORM_STATUS.md`'s Networking section, which notes the NAT Gateway IP must be allowlisted in each source's security group/firewall). Figure is a base hourly-charge estimate; does not include data-processing charges, which scale with extraction volume. |
| **Total AWS** | | **≈ $770–$790/month** | Corrected from the prior version's $699 figure, primarily by fixing the VPC Endpoint line ($15 → ~$73). This is a **projection for assumed steady-state volume with all 5 sources live**, not an observed bill — actual dev spend today is far lower, since only 2 of 5 sources are connected and no pilot tenant is running load against the platform. |

### Operational Staffing Costs (Replaced)

**Before the platform:**

| Role | Activity | Time/month | Cost/month (fully loaded) |
|---|---|---|---|
| ETL developer | Write/maintain 5 custom extraction scripts | 40 hrs | $2,000 |
| Data engineer | Handle failures, reruns, schema changes | 30 hrs | $2,500 |
| Data analyst | Manual data pulls, reconciliation | 20 hrs | $1,200 |
| DBA | Monitor MySQL connections, credential rotation | 10 hrs | $800 |
| Compliance/Audit | Manual lineage documentation | 15 hrs | $1,500 |
| **Total manual labor** | | **115 hrs** | **$8,000/month** |

**After the platform:**

| Role | Activity | Time/month | Cost/month |
|---|---|---|---|
| Platform engineer (0.5 FTE) | Monitor health, address DLQ alerts, add new sources | 10 hrs | $1,000 |
| Data analyst | Use curated data (now automated; focus on insights) | 5 hrs | $300 |
| Data engineer (0.1 FTE) | Config updates, schema governance | 4 hrs | $400 |
| **Total platform ops** | | **19 hrs** | **$1,700/month** |

**Savings from automation:** **96 hrs/month** = **$6,300/month** in eliminated labor

> **Status check:** Both the "before" and "after" tables above are illustrative models for 5
> connected sources at production volume. Today, only 2 of 5 sources are connected, no pilot
> tenant has onboarded, and no team has actually shifted its working hours as a result of this
> platform yet. Treat the labor-savings figures below as a **projection to validate the business
> case**, not a result already achieved.

---

## Full Cost Analysis

### Scenario 1: First Year Deployment (projected)

**One-time costs (infrastructure setup):**
- AWS account setup, VPC provisioning, Terraform scaffolding: **$3,000**
- Data platform team training & runbook creation: **$5,000**
- Entity configuration for 5 initial sources: **$2,500**
- Security review & compliance gating: **$2,000**
- **Total one-time:** **$12,500** (estimate; dev's actual setup effort to date has not been tracked against this figure)

**Year 1 ongoing (Monthly × 12), using the corrected ~$780/month AWS estimate above:**
- AWS infrastructure: $780 × 12 = **$9,360**
- Platform operations (staffing): $1,700 × 12 = **$20,400**
- **Total Year 1 ongoing:** **$29,760**

**Year 1 total deployed cost:** **$12,500 + $29,760 = $42,260**

**Savings delivered in Year 1 (projected, once all 5 sources are live and a pilot tenant is running):**
- Eliminated manual labor: $6,300 × 12 = **$75,600**
- Reduced developer context-switching: ~$10,000 (estimated from sprint velocity improvements)
- **Total Year 1 projected savings:** **$85,600**

**Year 1 ROI (if the projected savings materialize):**
```
(Savings - Cost) / Cost = ($85,600 - $42,260) / $42,260 ≈ 103% ROI
Illustrative break-even: Month 3–4
```
This has not been validated against a live pilot — it is the model's output, not a measured result.

---

### Scenario 2: Ongoing (Year 2+, projected)

After year 1, costs stabilize:

**Monthly recurring cost:**
- AWS infrastructure: **$780** (corrected estimate)
- Platform operations (1 FTE distributed across team): **$1,700**
- **Total monthly:** **$2,480**

**Ongoing annual savings (projected):**
- Eliminated manual labor (compounding): **$75,600+**
- Avoidance of ad-hoc data warehouse projects: **~$50,000** (estimated)
- **Total ongoing annual:** **$125,600+**

**Ongoing annual ROI (if projected):** **($125,600 - $29,760) / $29,760 ≈ 322% ROI**

---

## Sensitivity Analysis

These are second-order estimates built on the already-unverified base case above — treat them as
directional, not precise.

### What if extraction volume doubles?

**Impact:** Additional S3 storage (~$120/month) + additional Lambda runs (~$50/month)  
**New total:** $780 + $170 = **$950/month**  
**Result:** Directionally, ROI likely stays strongly positive — not recalculated to a precise figure here.

### What if we add 10 more data sources?

**Impact:** 
- Additional Secrets Manager secrets: **+$4/month**
- Additional EventBridge schedules: **+$1/month**
- Additional DynamoDB capacity (on-demand): **~+$50/month**
- **New AWS cost:** **~$835/month**
- **Additional platform ops (0.25 FTE):** **+$500/month**

**New total:** ~$835 + $2,200 = **~$3,035/month** (for 15 sources instead of 5)  
**Result:** Directionally still favorable — not independently recalculated to a precise ROI percentage.

---

## Financial Comparison: Build vs. Buy vs. This Platform

### Option A: Buy a Commercial Data Lake (e.g., Fivetran, Stitch)

| Category | Cost |
|---|---|
| Licensing (per source, 5 sources) | $3,000/month |
| Connector config time | 20 hrs |
| Operational overhead | Low (vendor-managed) |
| **Annual cost** | **$36,000** |
| **Vendor lock-in risk** | High |
| **Customization flexibility** | Low (limited to vendor's roadmap) |

### Option B: Build Everything In-House (Before This Platform)

| Category | Cost |
|---|---|
| Developer salaries (2 FTE × 12 months) | $240,000 |
| Infrastructure (self-managed) | $5,000/month = $60,000 |
| On-call coverage & incidents | $30,000 |
| **Annual cost** | **$330,000** |
| **Time to first extract** | 6–9 months |
| **Customization flexibility** | Unlimited |
| **Governance/compliance built-in** | Varies (often missing) |

### Option C: This Platform (dev deployed; staging/prod not yet)

| Category | Cost |
|---|---|
| Year 1 (setup + ops + AWS), projected | $42,260 |
| Year 2+ (ops + AWS only), projected | $29,760 |
| **Break-even vs. commercial SaaS** | Illustrative Month 6 — not validated against a live pilot |
| **Break-even vs. in-house build** | Illustrative Month 4 (based on avoided 2 FTE salary) — same caveat |
| **Time to first extract** | Achieved for 2 of 5 sources in dev already (Salesforce, MySQL RDS); the other 3 are code-complete but need credentials populated |
| **Customization flexibility** | 80% (configuration) + 20% (code) — reflects the actual connector/config architecture |
| **Governance/compliance** | Automated PII masking and lineage recording exist in code and are exercised by tests; no external compliance audit (SOC 2, GDPR, HIPAA) has been performed against this platform |

---

## Hidden Costs Avoided

These figures are illustrative — no incident history exists yet for this platform (only dev has
run, briefly, with 2 of 5 sources). They are included to show the shape of the argument, not as
measured facts.

### Incidents & Downtime (Before Platform)

- Average data pipeline failure: **4 hrs to diagnose** × $2,000/hr (team billable) = **$8,000/incident**
- Frequency: **1–2 incidents/quarter** = **$16,000–$32,000/year**
- With platform: **automated alerts + replay** → incidents reduced by 80% (assumed) = **$3,200–$6,400/year saved (projected)**

### Compliance & Audit Penalties

- Data lineage audit findings: **$50,000–$250,000 per finding** (varies by regulation; industry-general figure, not specific to this organization)
- Platform's automated lineage: **assumed to eliminate 80% of potential findings** = **$40,000–$200,000 risk avoided (projected, not measured)**

### Developer Context-Switching

- Manual data extracts interrupt developers: **5–10 hrs/week × $2,000/hr = $10,000–$20,000/month** (assumed baseline)
- Platform reduces manual requests by 90% (assumed) = **$9,000–$18,000/month saved (projected)**

---

## Recommendation

**Continue toward a pilot tenant to validate this model, rather than treating it as proven.**

- **Financial case is directionally positive but unvalidated:** ~103% projected Year 1 ROI; ~320%+ projected ongoing — both depend on savings that have not yet been measured against a live pilot tenant
- **Current state:** Dev environment is deployed and verified end-to-end for 2 of 5 sources (Salesforce, MySQL RDS); staging and production are not yet provisioned (no AWS account for either); no pilot tenant has onboarded; no load test has been performed at target scale
- **Recommended next step:** onboard one pilot tenant against the dev environment (or a newly provisioned staging environment) to replace the projected labor-savings and cost figures above with measured ones before committing to a company-wide rollout timeline
- **Compliance posture:** automated PII masking and lineage recording are implemented and covered by tests; tenant-level IAM enforcement for the `entity-extraction-config` DynamoDB table and the S3 bucket-policy tenant-prefix condition is a tracked, deferred gap — not yet closed (see `docs/KNOWN_GAPS_AND_ROADMAP.md`)

**Approved signatories:**
- [ ] CFO / Finance Director
- [ ] Chief Data Officer
- [ ] Chief Information Security Officer
- [ ] VP Engineering

---

## Technology Cost Drivers — Reference

This section maps each AWS service to its cost driver and the optimisation already applied.

| Service | Cost driver | Optimisation applied | Monthly estimate |
|---|---|---|---|
| **Amazon S3** | GB stored × storage class | Lifecycle rules in `infrastructure/modules/storage/main.tf`: raw → STANDARD_IA at 90 days, GLACIER at 365 days; curated → STANDARD_IA at 180 days. Parquet compression (5–10× vs JSON). No Intelligent-Tiering is configured anywhere — corrected from the prior version of this document. | $120 |
| **S3 Data Transfer** | GB transferred out | Partitioned Athena scans minimise outbound; S3→Lambda intra-region is free | $180 |
| **AWS Lambda** | GB-seconds × invocations | Streaming architecture (constant RAM regardless of dataset size); 512 MB allocation | $80 |
| **AWS ECS Fargate** | vCPU-hours + GB-hours (large jobs only) | **Not implemented.** No ECS/Fargate resource exists anywhere in `infrastructure/` — this was planned as a large-dataset escape hatch beyond Lambda's limits but has not been built. Remove from cost planning until it exists. | $0 (does not exist) |
| **Amazon DynamoDB** | Read/write capacity units | On-demand pricing; PITR adds ~25% to storage cost; 5 tables confirmed in Terraform (watermark repository, run audit log, entity extraction config, entity type registry, source onboarding registry) | $150 |
| **AWS Step Functions** | State transitions | 16 transitions per pipeline run; Standard Workflow | $3 |
| **Amazon EventBridge Scheduler** | Invocations | 12 schedules × 1/day × 30 days (assumed; nothing is scheduled in dev today) | $1 |
| **AWS Secrets Manager** | Secrets stored + API calls | 5 secrets confirmed in Terraform (Salesforce, NetSuite, MySQL RDS, Sage Intacct, Sage X3); only Salesforce and MySQL RDS currently hold real credentials; retrieval cached per Lambda invocation; daily expiry-check alerting (auto-rotation planned, not yet implemented) | $10 |
| **Cognito + API Gateway + Lambda** (control plane) | Requests + Lambda invocations | SaaS control-plane API (tenant/entity self-service) plus `credential_expiry_notifier`, `pipeline_trigger`, `dlq_processor` Lambdas; rough placeholder, not load-tested, and its end-to-end request flow against the live Cognito authorizer has not been exercised | $10–$20 |
| **Amazon Athena** | TB scanned | Year/month/day partitioning limits scan to relevant partition (1–10 GB typical) | $25 |
| **AWS Glue Data Catalog** | Catalog entries | Catalog-only cost (no Glue ETL jobs used). `datalake_curated_dev` database is provisioned but unwired to any Lambda in dev, so it never registers tables regardless of usage. | $0 |
| **Amazon CloudWatch** | Log ingestion + storage + metrics + alarms | Structured JSON logs (compact); 30-day hot retention; 50 custom metrics (assumed) | $70 |
| **AWS KMS** | API calls + CMK storage | 1 shared CMK for all resources (annual rotation) | $1 |
| **Amazon SQS** | Messages sent | DLQ only (low volume; triggered only on failures) | < $1 |
| **Amazon VPC — Interface Endpoints** | Per-AZ hourly charge + data processed | **Corrected.** 5 interface endpoints enabled by default (Secrets Manager, CloudWatch Logs, CloudWatch Monitoring, Step Functions, KMS), each spanning 2 AZs; S3/DynamoDB use free Gateway endpoints; Glue endpoint is disabled by default. Rate assumed at AWS's standard $0.01/AZ-hour — confirm against the current AWS Pricing Calculator before treating as precise. | ~$73 |
| **Amazon VPC / NAT Gateway** | Data processed + hourly charge | Already deployed in dev (`single_nat_gateway = true`), not optional — required because Salesforce/NetSuite/Sage are internet-reachable SaaS APIs, not PrivateLink endpoints. Figure is the base hourly charge only; excludes data-processing charges. | $45 |
| **Amazon RDS MySQL** | Instance hours + storage | Two distinct things share this label. (1) The **extraction source**: the customer's own existing MySQL database that the extraction connector reads from via Secrets Manager credentials — not part of this platform's Terraform footprint; its hosting cost is not part of this platform's bill. (2) The optional **serving store** load-back: `infrastructure/modules/serving_store_database` (engine-parameterized; only its MySQL instantiation is wired in dev/staging/prod `main.tf` today — PostgreSQL/SQL Server are code-ready but not instantiated), the separate `infrastructure/modules/serving_store_redshift` module (Amazon Redshift Serverless — code-ready but not instantiated), plus `infrastructure/modules/serving_store_lambda` are now code-complete and `terraform validate`-clean, but have not been `terraform apply`'d in any environment — no RDS/Redshift instance or Lambda actually exists yet, so it's still $0 today. Treat it as a near-term cost once deployed, not a current one. Redshift Serverless bills per RPU-second of query/load time (not instance-hours), so its cost scales with load volume rather than being a fixed baseline. | Not applicable to this platform's cost today |

**Key cost insight:** The Parquet format (Apache Parquet, Snappy compression) is the single biggest cost lever — a 5–10× reduction in S3 storage compared to raw JSON directly reduces S3 storage, data transfer, and Athena scan costs simultaneously.

