# FAQ: Questions Management Will Ask (And Answers)

**For:** All stakeholder levels  
**Format:** Q&A reference  
**Last updated:** 2026-07-09 (revised — see status note below; originally 2026-06-29)

> **Current status, plainly stated:** Only the `dev` AWS environment is deployed (rebuilt from
> scratch on 2026-07-09). Staging and Production have no AWS account and are not deployed. Of five
> planned source connectors, two — Salesforce and MySQL RDS — are connected with real credentials
> and have run end-to-end (34 Salesforce accounts, 36,023 MySQL RDS rows, both queryable today).
> Sage Intacct, Sage X3, and NetSuite are code-complete but have no credentials populated, so
> nothing has run for them. No pilot tenant has been onboarded, and the platform has not been
> tested at production scale. Several answers below were revised from an earlier, more optimistic
> version of this document to reflect that reality.

---

## Business & Strategy

### Q: "How is this different from just buying a SaaS data integration tool?"

**A:** The architectural trade-offs are real, but treat the numbers below as directional
estimates, not settled facts — none of this has been benchmarked against a live SaaS-tool
alternative for our specific sources.

| Aspect | SaaS Tools (Fivetran, etc.) | This Platform |
|---|---|---|
| **Flexibility** | Fixed connectors; can't customize | Full source code; customize everything |
| **Cost scaling** | ~$3K–$5K per source (industry-general figure, not a quote we've obtained) | ~$770–$800/month AWS (corrected estimate for 5 sources at assumed production volume — see `COST_ANALYSIS_AND_ROI.md`; not an observed bill) + staff |
| **Compliance customization** | Limited to vendor's roadmap | Our PII rules, retention, and audit trail — implemented in code, not yet independently audited |
| **Lock-in risk** | High (vendor-dependent) | Low (open infrastructure, version-controlled code) |
| **Onboarding time** | 3–5 days (per source), industry-general estimate | New source connector: code-complete for Salesforce/NetSuite/MySQL RDS/Sage Intacct/Sage X3 today. Turning on a new *instance* of an existing connector is configuration-only. Onboarding is currently a manual, config-file-driven process — the automated self-service control-plane API exists in code but its end-to-end request flow hasn't been exercised against a live deployment yet |
| **Suitable for** | High-confidence, low-customization | High-compliance, complex transformations, rapid scaling — once validated by a pilot tenant |

**Bottom line:** The cost/flexibility trade-off favors this platform on paper for our use case
(Salesforce, NetSuite, MySQL RDS, Sage Intacct, Sage X3, with compliance requirements) — but we
have not yet run a pilot tenant to confirm the projected savings hold up in practice. Avoid citing
a specific multiplier (e.g. "10x cheaper") until that's measured.

---

### Q: "Can we still switch to a SaaS tool later if we want?"

**A:** Yes, by design:

- **Raw data** in S3 is Parquet (an open, standard columnar format; no vendor lock-in)
- **Transformation rules** are version-controlled JSON files (portable)
- **Entity resolution configs** are declarative JSON (portable)
- The non-portable layer is the Lambda code (Python) itself

**Switching cost** if you wanted to migrate away: not yet estimated with any real data point — the
platform hasn't been in use long enough, or at enough scale, to size a realistic migration effort.
Treat any dollar figure here as a guess until we've run it once.

---

### Q: "What happens if we acquire another company with different data systems?"

**A:** The architecture is designed to make this easier, with real caveats:

- **New instance of an existing connector** (another Salesforce org, another MySQL database) —
  configuration-only, no code change, once the pattern above is validated by a pilot tenant
- **A genuinely new source system** (e.g., Dynamics 365, HubSpot) — **no connector exists today.**
  Only Salesforce, NetSuite, MySQL RDS, Sage Intacct, and Sage X3 connectors are built
  (`connector_runtime/adapters/`). Adding a new source system means building a new connector
  following the existing pattern (the `/new-connector` scaffold in this repo) — real engineering
  work, not a config change.
- **Governance rules?** Update the PII classification policy and entity resolution config
- **Data volume?** Infrastructure is designed to scale, but this has not been load-tested at
  target production scale yet

We do not have a committed connector for Dynamics 365, HubSpot, or PostgreSQL today — that would
need to be scoped and built if prioritized.

---

## Technical Concerns

### Q: "What happens if the extraction breaks? How long until data is stale?"

**A:** The failure-handling design has several real layers, though none of it has been exercised
under a real production failure yet (dev has run cleanly so far):

1. **Alerts** → An SNS-based alert fires on failure (credential-expiry checks run daily today; a
   broader real-time failure alert path exists in the architecture but its response-time SLA has
   not been measured against a real incident)
2. **Automatic retry** → Exponential-backoff retry logic exists in the Step Functions workflow
3. **Previous data not deleted** → Raw data is written once per run and never overwritten, so a
   failed run doesn't destroy the prior day's clean data
4. **Dead-Letter Queue** → Failed runs land in `EdlExtractionFailureDlq`; `EdlDlqProcessor` writes
   an audit record and sends an SNS alert, with optional auto-replay (off by default)
5. **SLO target** → 99.5% run-completion is a target we've set, not a measured historical rate —
   dev hasn't run long enough or at enough volume to report a real number

**Worst-case scenario, by design:** stale (not lost) data — last successful run's output remains
available. This has not been tested under a simulated real failure yet.

---

### Q: "Are we secure? What about PII exposure?"

**A:** The security controls below are implemented in code and exercised by automated tests, but
**this platform has no production track record** — only the `dev` environment exists, it was
rebuilt from scratch on 2026-07-09, and no external security or compliance audit has been
performed against it.

| Layer | Control | Status |
|---|---|---|
| **Raw data** | Private S3 with KMS encryption; bucket policies block public access | Implemented, in Terraform |
| **Credentials** | AWS Secrets Manager; daily automated expiry check alerts via SNS if rotation is overdue (automatic rotation is a planned follow-up, not yet implemented); never logged | Implemented |
| **Transformation** | PII identified & masked before curated data is created | Implemented, covered by tests |
| **Analytics** | Masked version only reaches the analytics layer | Implemented |
| **Network** | AWS-internal service calls (S3, DynamoDB, etc.) go through private VPC endpoints; **however**, Salesforce/NetSuite/Sage are internet-hosted SaaS APIs reached via a NAT Gateway's public IP, not a private tunnel — see the connectivity question below | Implemented, but not "no internet exposure" as previously stated |
| **Audit** | Every pipeline run logged to an immutable DynamoDB audit table | Implemented |
| **Tenant isolation** | S3 prefixes and most DynamoDB tables are tenant-scoped; two tables (`entity-extraction-config`) still rely on an application-level guard rather than IAM enforcement, and the S3 bucket-policy tenant-prefix condition isn't turned on yet — tracked as `SEC-2`/`ARCH-12` in `architecture/GAP_ANALYSIS_FINDINGS.md` | Partially implemented |
| **Compliance** | Automated lineage recording exists and is designed to support GDPR/SOC 2/HIPAA audits | Implemented in code; **no external compliance audit has actually been performed** |

**Track record:** None yet to report — the platform has not run in production. Avoid citing an
uptime or breach-free track record until one actually exists.

---

### Q: "What if there's a schema change in the source (e.g., Salesforce adds a new field)?"

**A:** The platform's drift-detection design distinguishes breaking from non-breaking changes:

1. **New optional field added** → Raw data captures it; transformation continues (non-breaking)
2. **Field length reduced** → Flagged for review (potentially breaking)
3. **Field removed** → Drift detected; transformation blocks; alert escalated (breaking)

This logic exists in code and is covered by tests, but has not yet been triggered by a real schema
change from a live source in dev.

**Comparison:** With hand-written scripts, schema changes are typically discovered when the script
crashes.

---

### Q: "What's the query latency? Can we use it for real-time dashboards?"

**A:** Depends on the use case:

| Use case | Latency | Solution |
|---|---|---|
| **Nightly / periodic reports** | Depends on schedule cadence (design supports hourly-or-slower refresh) | Analytics layer |
| **BI dashboards** | Same as above | Athena on analytics layer (no separate query engine to license) |
| **App operational data** | Sub-second, in principle | A "serving store" load-back concept exists in the design, but **no Lambda implements it in any environment today** — the relevant Step Functions state is a no-op placeholder until that's built |
| **Real-time operational events** | Not supported | This platform is not designed for sub-second event streaming (would need Kafka/Kinesis-style tooling) |

No latency figures above have been measured against a real reporting workload yet — dev has only
run a handful of extraction jobs so far.

---

### Q: "How much data can it handle? Does it scale?"

**A:** The architecture is designed with headroom, but scale has not been tested — dev's real data
volume so far is 34 Salesforce accounts and 36,023 MySQL RDS rows, far below any of the figures
below.

| Scale | Design intent |
|---|---|
| Smaller entities | Standard Lambda extraction (15-minute execution limit) |
| Larger entities | Salesforce connector implements the Bulk API for async, batched extraction (`connector_runtime/adapters/salesforce/salesforce_bulk_query_job_controller.py`) |
| Very large entities | **No ECS Fargate (or equivalent) task exists in this codebase today** — earlier documentation described this as an already-built escape hatch for datasets that exceed Lambda's limits; that component has not actually been built. Extraction beyond Lambda's timeout would need this to be built first. |

**Memory:** The transformation/entity-resolution pipeline uses a streaming (DuckDB-backed) design
intended to keep memory flat regardless of dataset size — a real, implemented optimization, but
not yet stress-tested at high volume.

**Realistically:** the design should scale well beyond today's data volumes, but "10-100x" is an
untested claim — remove it until a load test backs it up.

---

### Q: "What if our internet connection goes down while extraction is running?"

**A:** Partially handled automatically, but the earlier claim that source connectivity is "already
tunneled, not public internet" was incorrect for two of our five sources:

- **Salesforce, NetSuite, and Sage** are internet-hosted SaaS APIs. The extraction Lambda runs in
  a private subnet and reaches them through a NAT Gateway's public IP (`docs/PLATFORM_STATUS.md`
  confirms this IP must be allowlisted in each source's firewall/security-group settings) — this
  traffic does traverse the public internet, it just isn't a publicly *reachable* Lambda.
- **MySQL RDS** connectivity depends on how the customer's database network is configured; the
  extraction Lambda reaches it the same way, via the private subnet + NAT Gateway path, unless a
  private network path (VPC peering, PrivateLink) is separately set up.
- **S3 upload** completes or rolls back atomically (no partial writes)
- **If connection drops mid-run** → Watermark not advanced → next run replays same window (idempotent)
- **If Lambda timeout reached** → Step Functions retries automatically

**No data loss scenario exists** in the architecture.

---

## Operational & Governance

### Q: "Who's responsible for keeping this running? Do we need a new team?"

**A:** Estimated staffing needs, once the platform is running a real workload (not yet validated
against actual operational load, since only dev exists today):

- **Platform ownership** → Existing data engineering team (~0.5 FTE, estimated)
- **Alert response** → Integrate into existing on-call rotation
- **Config management** → Data governance / data quality team (~0.1 FTE, estimated)
- **Infrastructure** → Cloud platform team (periodic AWS account reviews)

**Comparison:** the "manual scripts require 2 FTE" baseline in `COST_ANALYSIS_AND_ROI.md` is a
modeled estimate for illustration, not a measurement of our current team's actual time spent on
manual extraction — treat any specific savings number as directional until measured.

---

### Q: "How do we govern who can access what data?"

**A:** Fine-grained, role-based access:

```
Raw data (PII)
  ├─ Data engineers (read-only)
  └─ Compliance/audit (read-only, governance role)

Curated data (PII-masked)
  ├─ Transformation team (read-only)
  ├─ Entity resolution team (read-only)
  └─ Data quality team (read-only)

Analytics layer (PII-masked, curated)
  ├─ BI analysts (read; prefix-scoped to approved datasets)
  ├─ ML engineers (read; feature store prefix only)
  ├─ Finance (read; company entity records only)
  └─ Marketing (read; customer entity records only)

Serving database (optional, for apps — not yet built; see the latency question above)
  ├─ API services (read-only via app IAM role)
  └─ BI tools (read-only connection string, no write)
```

This access model reflects the intended design. IAM enforces most of it today, with two known,
tracked gaps: the `entity-extraction-config` DynamoDB table and the S3 bucket-policy tenant-prefix
condition are not yet IAM-enforced at the key/prefix level (application-level guards only —
`SEC-2`/`ARCH-12` in `architecture/GAP_ANALYSIS_FINDINGS.md`). Neither is a data leak today (both
fail closed), but don't describe this as a hard security boundary until that's closed.

---

### Q: "What's the approval process for adding a new data source?"

**A:** A six-gate onboarding checklist exists as a certification concept
(`connector_runtime/certification/connector_certification_checklist.py`), covering registration,
credential setup, entity mapping, a dry-run extraction profile, security/governance review, and
acceptance validation. **This is a documented checklist, not yet an enforced automated workflow**
— there's a separate `EdlSourceOnboardingRegistry` DynamoDB table intended to track gate state,
but it isn't currently called from the control-plane API, so no route today reads or writes it.
In practice, bringing on a new source in dev today is a manual process: populate credentials in
Secrets Manager, seed entity config via a script, and enable scheduling — not a self-service,
gated workflow yet. Timeline estimates for onboarding a genuinely new source system aren't
meaningful until that automation exists; treat "weeks" as the honest ballpark rather than a
precise figure.

---

## Implementation & Risk

### Q: "Is this production-ready, or is it still experimental?"

**A:** Somewhere in between — **dev is real and working, production is not yet built.**

What's actually true today:
- ✅ Modules have 80%+ automated test coverage (enforced in CI)
- ✅ Core security/compliance mechanisms (PII masking, encryption, audit logging) are implemented in code and covered by tests
- 🟡 Most tenant-isolation mechanisms are code-complete; two gaps remain deliberately deferred and tracked (`SEC-2`, `ARCH-12`) — not yet IAM-enforced
- ❌ No chaos-engineering exercises or incident drills have been run against this platform
- ❌ No external security architecture review has been signed off
- ❌ No external compliance audit (SOC 2, GDPR, HIPAA) has been performed
- ❌ Only `dev` is deployed; staging and production have no AWS account and are not provisioned
- ❌ No pilot tenant has onboarded; nothing has run at production scale

**What's actually needed before "production-ready" is a fair label:**
- Provision a staging AWS account and deploy there
- Onboard a pilot tenant and validate the platform under a real (even if small) workload
- Close the two tracked tenant-isolation gaps (`SEC-2`, `ARCH-12`) if IAM-level enforcement is a launch requirement
- Get an external security/compliance review, if that's required for our regulatory posture

There is no meaningful "timeline to go-live" figure to quote until the steps above are scoped.

---

### Q: "What's the worst-case scenario? What could go wrong?"

**A:** Known risks & mitigations — none of these have been tested against a real incident yet
(dev has run cleanly so far, at low volume):

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Source API rate limit hit | Medium | Extraction delay | Backoff strategy built-in; alert fired |
| DynamoDB hot partition | Low | Watermark update slow | On-demand capacity; circuit breaker logic exists in code |
| S3 capacity exceeded | Very low | Writes throttled | Standard AWS regional quotas apply; would need a quota-increase request |
| Schema corruption | Very low | Data quality alert | Breaking drift detected & blocks transformation (implemented, tested) |
| Credentials rotated unexpectedly | Very low | Extraction fails | Retry logic; manual credential update path exists; automatic rotation is not yet implemented |
| **Data loss** | Low, not zero | Data unavailable or lost | Raw data is versioned and never overwritten in place, with S3's standard multi-AZ durability. **Correction:** there is no cross-region replication or separate backup bucket configured in Terraform today — the earlier claim that data loss is "impossible" overstated this. |

**Residual risk:** Meaningfully reduced by the design, but not zero, and not yet tested against a
real failure.

---

### Q: "How do we know the data quality is good?"

**A:** Three layers of validation:

**Layer 1: Raw extraction**
- Record count check (compared to previous run)
- Field presence check (all expected fields present?)
- Type validation (integers are integers, dates are dates)

**Layer 2: Transformation**
- Null checks (required fields have values)
- Pattern checks (emails match email regex, phone is valid)
- Enum checks (status ∈ {active, inactive, pending})
- Range checks (order amount > 0 and < max)

**Layer 3: Post-publication**
- Quality dashboard (% records passing per entity per run)
- Row count validation (curated ≈ raw, allowing for filtering)
- Lineage validation (can trace every analytics record back to raw source)

**If quality fails:** Alarm fires, analytics not published, previous dataset remains available.

---

### Q: "How do we train the team to use this?"

**A:** Three-level learning path:

| Level | Audience | Content | Time |
|---|---|---|---|
| **Executive** | C-suite, Board | This FAQ + one-pager | 15 min |
| **Business user** | BI analysts, marketers, finance | How to query analytics layer via Athena or BI tool | 2 hours |
| **Administrator** | Data engineers, platform ops | Adding sources, monitoring, incident response | 1 day |
| **Developer** | Adding new connectors (future) | Connector interface, query builders, testing | 3 days |

We'll deliver pre-recorded videos + live Q&A for each level.

---

## Regulatory & Compliance

### Q: "Does this meet our compliance requirements? (GDPR, CCPA, SOC 2, HIPAA?)"

**A:** The technical building blocks below are implemented in code and infrastructure, but **no
external body has audited this platform against any of these standards** — treat "implementation
exists" and "compliant" as different claims.

| Standard | Requirement | What's implemented |
|---|---|---|
| **GDPR** | Data lineage, right to erasure, consent audit trail | Automated lineage records exist for most write paths (two lower-severity gaps — unscoped lineage/quality-report prefixes — are tracked as `ARCH-13`/`ARCH-14`, deliberately deferred, no cross-tenant data corruption risk) |
| **CCPA** | Data inventory, access logs, deletion capability | Glue catalog inventory (analytics layer only; curated layer isn't wired to Glue in dev today); S3 access logs; S3 Object Lock is configured |
| **SOC 2** | Audit trail, change control, incident response | DynamoDB audit log; Git version control; CloudWatch alarms |
| **HIPAA** | Encryption, access control, audit logs | KMS encryption; IAM roles; audit trail — note the tenant-isolation gaps flagged elsewhere in this FAQ (`SEC-2`, `ARCH-12`) if HIPAA-grade tenant separation is a requirement |
| **GDPR Right to Erasure** | Delete PII within 30 days of request | S3 Object Lock governance-bypass mechanics exist; this has not been exercised end-to-end against a real erasure request |

**Not audit-ready today** in the sense of "an auditor has signed off" — no SOC 2, GDPR, or HIPAA
audit has been performed against this platform. The building blocks that would support passing
such an audit are largely in place, but "designed to support compliance" and "compliant" are not
the same claim, and only the first is true right now.

---

### Q: "What about data residency? Can we keep data in a specific region?"

**A:** Full regional control:

- All S3 buckets: **Single region** (you choose: us-east-1, eu-west-1, etc.)
- DynamoDB: **Single region** (same region as S3)
- Lambda: **Same region**
- No cross-region replication unless explicitly enabled (GDPR-compliant)
- All source connectivity: **Private VPC** (never leaves your region)

**Terraform variable:** `aws_region = "eu-west-1"` (for example). Changes region for entire deployment.

---

## Final Q&A

### Q: "Who do I call if I have questions or concerns?"

**A:** Escalation path:

| Question type | Owner | Contact |
|---|---|---|
| Technical / architecture | Platform Engineering Lead | (see org directory) |
| Security / compliance | CISO / Data Security Officer | (see org directory) |
| Cost / business case | CFO or Project Manager | (see org directory) |
| Operations / runbooks | VP Operations | (see org directory) |
| Go-live timeline | Project Manager | (see org directory) |

**Schedule follow-up meeting:** Yes, absolutely. We'll walk through any part of this in detail.

---

## Technology Stack — Quick Q&A

### Q: "What cloud does this run on?"

**A:** AWS (Amazon Web Services) exclusively. All services are in a single AWS region (default: `us-east-1`; fully configurable). No multi-cloud dependencies.

---

### Q: "What are the main AWS services?"

**A:** The platform uses these AWS services:

| Service | Role |
|---|---|
| **EventBridge Scheduler** | Fires the pipeline on a cron schedule for each entity (per-entity schedules exist in code; none are currently enabled in dev) |
| **Step Functions** | Manages pipeline stages with retry, branching, and failure routing |
| **Lambda** | Runs the Python extraction, transformation, entity resolution, and analytics code (8 functions deployed in dev) — **no ECS Fargate task exists in this codebase**; earlier documentation described Fargate as a large-dataset escape hatch that hasn't actually been built |
| **S3** | Stores all data (raw, curated, analytics layers; lifecycle rules transition older data to cheaper storage classes over time) |
| **DynamoDB** | 5 tables confirmed in Terraform: entity extraction config, watermark repository, run audit log, entity type registry, source onboarding registry |
| **Secrets Manager** | Stores Salesforce / NetSuite / MySQL RDS / Sage Intacct / Sage X3 credentials; only Salesforce and MySQL RDS currently hold real values |
| **Glue Data Catalog + Athena** | Makes curated/analytics data queryable via SQL; in dev today only the analytics layer is actually registered in Glue — the curated-layer registration code path exists but isn't wired up yet |
| **CloudWatch + X-Ray + SNS** | Logs, metrics, alarms, alerts, tracing |
| **KMS + IAM + VPC** | Encryption, access control, network isolation |

---

### Q: "What programming language is it written in?"

**A:** Python 3.14, using:
- **Pydantic v2** — data model validation
- **structlog** — structured JSON logging (auto-scrubs PII/credentials)
- **pyarrow** — reads/writes Apache Parquet files
- **boto3** — AWS SDK
- **pymysql** — MySQL connector

---

### Q: "How is infrastructure managed?"

**A:** **Terraform** (≥ 1.8). Every AWS resource — S3 buckets, DynamoDB tables, IAM roles, VPC, encryption keys — is declared as code in the `infrastructure/` directory. Changes go through the same code review and CI/CD process as application code.

---

### Q: "What's the data file format?"

**A:** **Apache Parquet** (Snappy-compressed). It is:
- 5–10× smaller than JSON
- Columnar — fast for analytics queries
- Supported natively by Athena, Spark, Pandas, and all major BI tools

---

### Q: "How do BI tools connect?"

**A:** One option exists today, one is designed but not built:
1. **Amazon Athena** — connect Tableau / Power BI / Looker via ODBC or JDBC driver; queries run directly against S3 Parquet; no separate database server. This is the only connection path that actually works today.
2. **RDS MySQL "serving store"** — intended for dashboards needing sub-second response times; **no Lambda implements this load-back today** in any environment (the relevant Step Functions state is a no-op placeholder). Treat this as a future option, not something to demo.

---

### Q: "What CI/CD and quality tools are used?"

**A:** GitHub Actions runs an 8-stage gate on every code change:
1. **Ruff** — code style and security linting
2. **mypy** — static type checking (strict mode)
3. **pytest** — automated tests (≥ 80% coverage required)
4. **bandit** — Python SAST scan (OWASP Top 10)
5. **pip-audit** — dependency CVE scan
6. **checkov** — Terraform security scan
7. **Terraform validate** — infrastructure syntax and logic validation
8. **detect-secrets** — secret-scan for accidentally committed credentials

Deploys only proceed after all 8 gates pass.

