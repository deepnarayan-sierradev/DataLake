# Documentation Map & Reading Guide

**For:** Anyone approaching the documentation for the first time  
**Purpose:** Navigate 13 comprehensive documents + 6 existing docs effectively  
**Last updated:** 2026-06-17

---

## Quick Navigation by Role

### Executive / C-Suite / Board Members
**Read these (in order):**

1. **[EXECUTIVE_SUMMARY_ONE_PAGE.md](EXECUTIVE_SUMMARY_ONE_PAGE.md)** ← START HERE
   - 2 min read
   - Problem → solution → business impact → next steps
   - Print-friendly version to share

2. **[COST_ANALYSIS_AND_ROI.md](COST_ANALYSIS_AND_ROI.md)**
   - 5 min read (exec summary) + 10 min (detailed)
   - Month 1 ROI, ongoing savings, financial comparison vs. alternatives
   - Share with CFO

3. **[FAQ_FOR_MANAGEMENT.md](FAQ_FOR_MANAGEMENT.md)** (sections: Business & Strategy, Regulatory)
   - Skim for questions you'd ask
   - Bookmark for reference during meetings

---

### Chief Technology Officer / CIO
**Read these (in order):**

1. **[EXECUTIVE_OVERVIEW.md](EXECUTIVE_OVERVIEW.md)** (Sections 1–9)
   - 15 min read
   - Business problem → platform architecture → technical walkthrough
   - Understand the full picture

2. **[PLATFORM_FLOW.md](PLATFORM_FLOW.md)** (Sections 1–3)
   - 10 min read
   - Architecture diagram + data layer definitions + end-to-end flow
   - Visual reference for architecture reviews

3. **[FAQ_FOR_MANAGEMENT.md](FAQ_FOR_MANAGEMENT.md)** (section: Technical Concerns)
   - Answer common architecture questions

4. **[GO_LIVE_READINESS_CHECKLIST.md](GO_LIVE_READINESS_CHECKLIST.md)** (Security & Compliance section)
   - Verify all security hardening is complete

---

### Chief Information Security Officer (CISO) / Security Team
**Read these (in order):**

1. **[EXECUTIVE_OVERVIEW.md](EXECUTIVE_OVERVIEW.md)** (Sections 11, 15)
   - 5 min read
   - Security architecture summary + compliance & audit readiness

2. **[PLATFORM_FLOW.md](PLATFORM_FLOW.md)** (Sections 7, 8)
   - 10 min read
   - Security controls per layer + S3/DynamoDB layout

3. **[GO_LIVE_READINESS_CHECKLIST.md](GO_LIVE_READINESS_CHECKLIST.md)** (Security & Compliance)
   - Pre-go-live verification checklist

4. **[FAQ_FOR_MANAGEMENT.md](FAQ_FOR_MANAGEMENT.md)** (section: Technical Concerns — "Are we secure?")
   - Security Q&A template

---

### Chief Compliance Officer / Compliance & Legal Team
**Read these (in order):**

1. **[EXECUTIVE_OVERVIEW.md](EXECUTIVE_OVERVIEW.md)** (Sections 10, 15)
   - 5 min read
   - Data quality & governance + compliance & audit readiness

2. **[FAQ_FOR_MANAGEMENT.md](FAQ_FOR_MANAGEMENT.md)** (section: Regulatory & Compliance)
   - 5 min read
   - GDPR, CCPA, SOC 2, HIPAA readiness

3. **[GO_LIVE_READINESS_CHECKLIST.md](GO_LIVE_READINESS_CHECKLIST.md)** (Compliance Review section)
   - Sign-off checklist

---

### Chief Data Officer / VP Data Governance
**Read these (in order):**

1. **[EXECUTIVE_OVERVIEW.md](EXECUTIVE_OVERVIEW.md)** (entire document)
   - 20 min read
   - Comprehensive walkthrough of data layers, entity resolution, governance

2. **[PLATFORM_FLOW.md](PLATFORM_FLOW.md)** (entire document)
   - 20 min read
   - Technical flow with governance integration points

3. **[PIPELINE_FLOW.md](PIPELINE_FLOW.md)** (Sections 3–6)
   - 15 min read
   - Entity resolution + field mapping systems + field mapping

4. **[GO_LIVE_READINESS_CHECKLIST.md](GO_LIVE_READINESS_CHECKLIST.md)** (Data Configuration section)
   - Pre-go-live verification for configs

---

### VP Operations / Operations Manager
**Read these (in order):**

1. **[EXECUTIVE_OVERVIEW.md](EXECUTIVE_OVERVIEW.md)** (Sections 12–13)
   - 5 min read
   - Operational resilience + scalability

2. **[GO_LIVE_READINESS_CHECKLIST.md](GO_LIVE_READINESS_CHECKLIST.md)** (entire document)
   - 30 min read
   - Pre-go-live verification + runbook testing

3. **[PRODUCTION_INCIDENT_RUNBOOK.md](PRODUCTION_INCIDENT_RUNBOOK.md)**
   - 20 min read (skim for scenarios)
   - Bookmark for on-call reference
   - Ensure all ops team members have read this

4. **[FAQ_FOR_MANAGEMENT.md](FAQ_FOR_MANAGEMENT.md)** (section: Operational & Governance)
   - Who's responsible for running this?

---

### Platform Engineering Lead / Architecture Lead
**Read these (in order):**

1. **[PLATFORM_FLOW.md](PLATFORM_FLOW.md)** (entire document)
   - 30 min read
   - Complete technical architecture + modules + failure handling

2. **[BEGINNER_GUIDE.md](BEGINNER_GUIDE.md)** (if new to platform)
   - 30 min read
   - End-to-end walkthrough of one extraction run

3. **[DEPLOYMENT_GUIDE.md](DEPLOYMENT_GUIDE.md)**
   - 45 min read
   - How to deploy to production

4. **[GO_LIVE_READINESS_CHECKLIST.md](GO_LIVE_READINESS_CHECKLIST.md)** (entire document)
   - 45 min read
   - Pre-go-live verification

5. **[PRODUCTION_INCIDENT_RUNBOOK.md](PRODUCTION_INCIDENT_RUNBOOK.md)**
   - 30 min read
   - How to respond to production incidents

6. **[GLOSSARY_AND_TERMINOLOGY.md](GLOSSARY_AND_TERMINOLOGY.md)**
   - Quick reference for terminology (bookmark)

---

### Data Engineers / Transformation Team
**Read these (in order):**

1. **[BEGINNER_GUIDE.md](BEGINNER_GUIDE.md)** (entire document)
   - 30 min read
   - How one extraction run works, step-by-step

2. **[PLATFORM_FLOW.md](PLATFORM_FLOW.md)** (Sections 1–3)
   - 15 min read
   - Architecture overview + module layout

3. **[PIPELINE_FLOW.md](PIPELINE_FLOW.md)** (Sections 4–6)
   - 15 min read
   - Field mapping system + entity resolution config system

4. **[GLOSSARY_AND_TERMINOLOGY.md](GLOSSARY_AND_TERMINOLOGY.md)**
   - Bookmark for terminology reference

---

### Business Analysts / BI Analysts
**Read these (in order):**

1. **[EXECUTIVE_OVERVIEW.md](EXECUTIVE_OVERVIEW.md)** (Sections 1–4, 9)
   - 10 min read
   - Problem → solution → data layers explained
   - Understand what data is available and how fresh it is

2. **[GLOSSARY_AND_TERMINOLOGY.md](GLOSSARY_AND_TERMINOLOGY.md)** (Data Lake Layers section)
   - 5 min read
   - Understand raw vs. curated vs. analytics layers

3. **[FAQ_FOR_MANAGEMENT.md](FAQ_FOR_MANAGEMENT.md)** (section: Technical Concerns — "What's the query latency?")
   - How to query analytics layer, expected freshness

---

### New Team Members / Onboarding
**Read these (in order):**

1. **[EXECUTIVE_SUMMARY_ONE_PAGE.md](EXECUTIVE_SUMMARY_ONE_PAGE.md)**
   - 2 min read
   - High-level overview

2. **[BEGINNER_GUIDE.md](BEGINNER_GUIDE.md)**
   - 30 min read
   - One extraction run explained step-by-step

3. **[GLOSSARY_AND_TERMINOLOGY.md](GLOSSARY_AND_TERMINOLOGY.md)**
   - 15 min read
   - All terminology defined

4. Then read based on your role (see above)

---

## Document Inventory

### NEW Documents (Created for This Review)

| Document | Audience | Read time | Purpose |
|---|---|---|---|
| [EXECUTIVE_SUMMARY_ONE_PAGE.md](EXECUTIVE_SUMMARY_ONE_PAGE.md) | C-suite, board | 2 min | One-page summary for print distribution |
| [COST_ANALYSIS_AND_ROI.md](COST_ANALYSIS_AND_ROI.md) | Finance, leadership | 15 min | Full financial case with ROI calculations |
| [FAQ_FOR_MANAGEMENT.md](FAQ_FOR_MANAGEMENT.md) | All stakeholders | 20 min | Q&A reference for common questions |
| [GO_LIVE_READINESS_CHECKLIST.md](GO_LIVE_READINESS_CHECKLIST.md) | Project mgmt, ops | 45 min | Pre-go-live verification checklist |
| [PRODUCTION_INCIDENT_RUNBOOK.md](PRODUCTION_INCIDENT_RUNBOOK.md) | Operations, on-call | 30 min | How to respond to production incidents |
| [GLOSSARY_AND_TERMINOLOGY.md](GLOSSARY_AND_TERMINOLOGY.md) | All stakeholders | 15 min | Terminology definitions + AWS services |

### EXISTING Documents (Pre-existing)

| Document | Audience | Read time | Purpose |
|---|---|---|---|
| [EXECUTIVE_OVERVIEW.md](EXECUTIVE_OVERVIEW.md) | Leadership, stakeholders | 20 min | Functional walkthrough + roadmap |
| [PLATFORM_FLOW.md](PLATFORM_FLOW.md) | Engineers, architects | 30 min | Technical flow + module responsibilities |
| [PIPELINE_FLOW.md](PIPELINE_FLOW.md) | Engineers, architects | 30 min | Stage-by-stage pipeline reference |
| [BEGINNER_GUIDE.md](BEGINNER_GUIDE.md) | New developers, QA | 30 min | End-to-end walkthrough of one run |
| [DEPLOYMENT_GUIDE.md](DEPLOYMENT_GUIDE.md) | Platform engineers | 45 min | How to deploy to production |
| [Enterprise_Data_Lake_Platform_Full_Specification.md](../Enterprise_Data_Lake_Platform_Full_Specification.md) | All | Reference | Full platform specification (source of truth) |
| [Implementation_Plan_Phase_Wise.md](../Implementation_Plan_Phase_Wise.md) | Engineering | Reference | Phase-by-phase delivery plan |
| [README.md](../README.md) | Everyone | 2 min | Quick start, links to other docs |

---

## Reading Paths by Scenario

### Scenario 1: "I have 1 hour, summarize everything"

1. [EXECUTIVE_SUMMARY_ONE_PAGE.md](EXECUTIVE_SUMMARY_ONE_PAGE.md) (2 min)
2. [COST_ANALYSIS_AND_ROI.md](COST_ANALYSIS_AND_ROI.md) — Executive Summary only (3 min)
3. [EXECUTIVE_OVERVIEW.md](EXECUTIVE_OVERVIEW.md) — Sections 1–3 (10 min)
4. [PLATFORM_FLOW.md](PLATFORM_FLOW.md) — Sections 1–2 (5 min)
5. [FAQ_FOR_MANAGEMENT.md](FAQ_FOR_MANAGEMENT.md) — Skim for your role (20 min)
6. Reserve 20 min for Q&A

---

### Scenario 2: "I need to present this to my board next week"

1. [EXECUTIVE_SUMMARY_ONE_PAGE.md](EXECUTIVE_SUMMARY_ONE_PAGE.md) (2 min) — Print for handout
2. [COST_ANALYSIS_AND_ROI.md](COST_ANALYSIS_AND_ROI.md) (15 min) — Have CFO read this first
3. [EXECUTIVE_OVERVIEW.md](EXECUTIVE_OVERVIEW.md) (20 min) — Full business case and tech walkthrough
4. Practice presentation with [PLATFORM_FLOW.md](PLATFORM_FLOW.md) architecture diagram (10 min)
5. Print [FAQ_FOR_MANAGEMENT.md](FAQ_FOR_MANAGEMENT.md) as backup for Q&A

---

### Scenario 3: "We're going live in 2 weeks, what do I need to verify?"

1. [GO_LIVE_READINESS_CHECKLIST.md](GO_LIVE_READINESS_CHECKLIST.md) (45 min) — Work through entire checklist
2. [PRODUCTION_INCIDENT_RUNBOOK.md](PRODUCTION_INCIDENT_RUNBOOK.md) (30 min) — Ensure ops team trained
3. [DEPLOYMENT_GUIDE.md](../docs/DEPLOYMENT_GUIDE.md) (45 min) — Verify all deployment steps completed
4. [PLATFORM_FLOW.md](PLATFORM_FLOW.md) (30 min) — Architecture review with security team

---

### Scenario 4: "Something broke in production, what do I do?"

1. [PRODUCTION_INCIDENT_RUNBOOK.md](PRODUCTION_INCIDENT_RUNBOOK.md) — Find your scenario (2 min)
2. Follow the runbook step-by-step (5–30 min depending on severity)
3. Document root cause in ticket system
4. Reference [GLOSSARY_AND_TERMINOLOGY.md](GLOSSARY_AND_TERMINOLOGY.md) if you need terminology

---

### Scenario 5: "I'm new to the team, how do I learn this?"

**Day 1:**
1. [EXECUTIVE_SUMMARY_ONE_PAGE.md](EXECUTIVE_SUMMARY_ONE_PAGE.md) (2 min)
2. [GLOSSARY_AND_TERMINOLOGY.md](GLOSSARY_AND_TERMINOLOGY.md) (15 min)

**Day 2:**
1. [BEGINNER_GUIDE.md](BEGINNER_GUIDE.md) (30 min) — Step-by-step walkthrough
2. [EXECUTIVE_OVERVIEW.md](EXECUTIVE_OVERVIEW.md) (20 min) — Sections 1–9

**Day 3:**
1. [PLATFORM_FLOW.md](PLATFORM_FLOW.md) (30 min) — Your role-specific sections

**Ongoing:**
- Bookmark [FAQ_FOR_MANAGEMENT.md](FAQ_FOR_MANAGEMENT.md) as reference
- Keep [GLOSSARY_AND_TERMINOLOGY.md](GLOSSARY_AND_TERMINOLOGY.md) handy

---

## How to Use This Map

### For Presenters
1. Use the **"Quick Navigation by Role"** section
2. Share the appropriate docs with each attendee **before** your presentation
3. Use [EXECUTIVE_SUMMARY_ONE_PAGE.md](EXECUTIVE_SUMMARY_ONE_PAGE.md) as speaker reference and print for distribution

### For Project Managers
1. Use **"Scenario 3"** (Go-Live Readiness) as your checkpoint tracker
2. Ensure each team has read their role-specific docs before meetings
3. Share [EXECUTIVE_SUMMARY_ONE_PAGE.md](EXECUTIVE_SUMMARY_ONE_PAGE.md) with stakeholders who ask "what is this?"

### For Ops Teams
1. Print [PRODUCTION_INCIDENT_RUNBOOK.md](PRODUCTION_INCIDENT_RUNBOOK.md) and laminate
2. Keep it at your desk for on-call reference
3. Practice 1–2 scenarios during team sync weekly

### For Everyone
1. Bookmark [GLOSSARY_AND_TERMINOLOGY.md](GLOSSARY_AND_TERMINOLOGY.md) for terminology lookup
2. Share [FAQ_FOR_MANAGEMENT.md](FAQ_FOR_MANAGEMENT.md) when someone asks a question you've seen before
3. Reference [COST_ANALYSIS_AND_ROI.md](COST_ANALYSIS_AND_ROI.md) when discussing budget approvals

---

## Document Maintenance

### When to Update

| Document | Update frequency | Trigger |
|---|---|---|
| [EXECUTIVE_OVERVIEW.md](EXECUTIVE_OVERVIEW.md) | Quarterly | Major feature addition; roadmap change |
| [PRODUCTION_INCIDENT_RUNBOOK.md](PRODUCTION_INCIDENT_RUNBOOK.md) | After each incident | New failure scenario discovered |
| [GO_LIVE_READINESS_CHECKLIST.md](GO_LIVE_READINESS_CHECKLIST.md) | Quarterly | Process changes |
| [GLOSSARY_AND_TERMINOLOGY.md](GLOSSARY_AND_TERMINOLOGY.md) | Quarterly | New terms introduced |
| [FAQ_FOR_MANAGEMENT.md](FAQ_FOR_MANAGEMENT.md) | After each presentation | New Q&A collected |

### Owner

- **Platform Engineering Lead:** PLATFORM_FLOW, DEPLOYMENT_GUIDE, PRODUCTION_INCIDENT_RUNBOOK, GLOSSARY
- **Chief Data Officer:** EXECUTIVE_OVERVIEW, PIPELINE_FLOW, FAQ (Data Governance)
- **Project Manager:** GO_LIVE_READINESS_CHECKLIST
- **Finance / CFO:** COST_ANALYSIS_AND_ROI, EXECUTIVE_SUMMARY_ONE_PAGE

---

## Technology Stack Index

Use this table to quickly find where each technology is documented in detail.

| Technology / Tool | Primary documentation | Brief description |
|---|---|---|
| **AWS Lambda** | [PLATFORM_FLOW.md](PLATFORM_FLOW.md#1-platform-architecture-overview) | Compute for all pipeline stages (< 5 M records) |
| **AWS ECS Fargate** | [PLATFORM_FLOW.md](PLATFORM_FLOW.md#1-platform-architecture-overview) | Compute for large-volume extraction (> 5 M records/day) |
| **AWS Step Functions** | [PIPELINE_FLOW.md](PIPELINE_FLOW.md#stage-2--step-functions-orchestration) | 5-stage pipeline orchestrator with retry/branching |
| **Amazon EventBridge Scheduler** | [PIPELINE_FLOW.md](PIPELINE_FLOW.md#stage-1--event-scheduling) | Cron trigger per entity |
| **Amazon S3** | [PIPELINE_FLOW.md](PIPELINE_FLOW.md#2-data-layer-definitions) | Raw, curated, analytics, snapshots, configs |
| **S3 Object Lock (GOVERNANCE)** | [EXECUTIVE_OVERVIEW.md](EXECUTIVE_OVERVIEW.md#9-data-layers-explained) | Immutable raw layer; 7-year retention |
| **Amazon DynamoDB** | [PLATFORM_FLOW.md](PLATFORM_FLOW.md#9-dynamodb-table-layout) | Config, watermark, audit log, onboarding tables |
| **AWS Secrets Manager** | [PIPELINE_FLOW.md](PIPELINE_FLOW.md#stage-4--credential-retrieval) | Source credential storage; auto-rotation |
| **AWS Glue Data Catalog** | [PLATFORM_FLOW.md](PLATFORM_FLOW.md#stage-12--curated-layer-write) | Curated and analytics table registry |
| **Amazon Athena** | [EXECUTIVE_OVERVIEW.md](EXECUTIVE_OVERVIEW.md#9-data-layers-explained) | Serverless SQL on S3 Parquet |
| **Amazon RDS MySQL** | [PIPELINE_FLOW.md](PIPELINE_FLOW.md#stage-16--serving-store-load) | Operational serving store |
| **Amazon SQS (DLQ)** | [PIPELINE_FLOW.md](PIPELINE_FLOW.md#7-failure-handling-and-replay) | Dead-Letter Queue; 14-day retention |
| **Amazon CloudWatch** | [EXECUTIVE_OVERVIEW.md](EXECUTIVE_OVERVIEW.md#17-key-metrics-and-slos) | Logs, metrics, alarms |
| **AWS X-Ray** | [PLATFORM_FLOW.md](PLATFORM_FLOW.md#5-observability-logs-metrics-and-traces) | Distributed tracing |
| **AWS KMS** | [EXECUTIVE_OVERVIEW.md](EXECUTIVE_OVERVIEW.md#12-technology-stack-and-tools) | SSE-KMS encryption for all data at rest |
| **AWS IAM** | [EXECUTIVE_OVERVIEW.md](EXECUTIVE_OVERVIEW.md#8-least-privilege-access-model--who-can-read-what) | Least-privilege service roles |
| **Amazon VPC** | [EXECUTIVE_OVERVIEW.md](EXECUTIVE_OVERVIEW.md#12-technology-stack-and-tools) | Private network; VPC Endpoints for AWS services |
| **Terraform** | [DEPLOYMENT_GUIDE.md](DEPLOYMENT_GUIDE.md) | Infrastructure as Code (≥ 1.8; AWS Provider ~> 5.0) |
| **Python 3.14** | [LOCAL_DEV_SETUP.md](LOCAL_DEV_SETUP.md) | Runtime language for all platform code |
| **Pydantic v2** | [PLATFORM_FLOW.md](PLATFORM_FLOW.md#2-repository-layout-and-module-responsibilities) | Data model validation; frozen models |
| **structlog** | [PLATFORM_FLOW.md](PLATFORM_FLOW.md#5-observability-logs-metrics-and-traces) | Structured JSON logging; PII-scrubbing |
| **Apache Parquet** | [PIPELINE_FLOW.md](PIPELINE_FLOW.md#2-data-layer-definitions) | Columnar data format; Snappy compressed |
| **Salesforce Bulk API 2.0** | [PIPELINE_FLOW.md](PIPELINE_FLOW.md#stage-7--extraction) | High-volume async Salesforce extraction |
| **NetSuite SuiteQL** | [PIPELINE_FLOW.md](PIPELINE_FLOW.md#stage-7--extraction) | REST API with SQL-like query language |
| **GitHub Actions** | [DEPLOYMENT_GUIDE.md](DEPLOYMENT_GUIDE.md) | CI/CD; 7-stage gate (lint→typecheck→test→SAST→CVE→IaC→tf-validate) |
| **Ruff / mypy / bandit / checkov** | [DEPLOYMENT_GUIDE.md](DEPLOYMENT_GUIDE.md) | Code quality and security toolchain |
| **Jaro-Winkler / Jaccard** | [PIPELINE_FLOW.md](PIPELINE_FLOW.md#stage-13--entity-resolution) | Entity resolution similarity algorithms |
| **HMAC-SHA256** | [PLATFORM_FLOW.md](PLATFORM_FLOW.md#stage-12--curated-layer-write) | PII tokenisation (deterministic pseudonym) |

---

**Last updated:** 2026-06-17  
**Maintained by:** Documentation Owner  
**Review cycle:** Monthly

