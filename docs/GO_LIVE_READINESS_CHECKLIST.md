# Go-Live Readiness Checklist

**For:** Project manager, platform engineering lead, operations  
**Purpose:** Verify all systems are ready before production activation  
**Date:** 2026-06-17

---

## Infrastructure Readiness

### AWS Account Setup
- [ ] AWS account created & billing configured
- [ ] VPC created (prod environment, tagged correctly)
- [ ] S3 buckets provisioned: raw-layer, curated-layer, analytics-layer, schema-snapshots
- [ ] DynamoDB tables created: config, watermark, audit-log, onboarding-registry
- [ ] IAM roles created for extraction, transformation, entity-resolution, serving
- [ ] Secrets Manager namespaces created for Salesforce, NetSuite, MySQL credentials
- [ ] CloudWatch log groups created; retention set to 30 days (hot storage)
- [ ] KMS key created for S3 encryption
- [ ] VPC endpoints configured for S3, DynamoDB, Secrets Manager, CloudWatch, Glue

**Owner:** Platform Engineering  
**Timeline:** 1 day  
**Sign-off:** [ ] Infrastructure Lead

---

### Lambda & Compute
- [ ] Extraction Lambda deployed (prod environment)
- [ ] Transformation Lambda deployed (prod environment)
- [ ] Entity resolution Lambda deployed (prod environment)
- [ ] Analytics publish Lambda deployed (prod environment)
- [ ] Serving store load Lambda deployed (prod environment)
- [ ] All Lambda functions have correct IAM role attached
- [ ] Environment variables set correctly (S3 bucket names, DynamoDB table names, region)
- [ ] Lambda timeout values set per function (extraction: 15 min, transformation: 10 min, etc.)
- [ ] Dead-Letter Queue Lambda created (for replay handling)
- [ ] VPC networking configured for Lambdas (private subnets, security groups)

**Owner:** Platform Engineering  
**Timeline:** 1 day  
**Sign-off:** [ ] Platform Engineering Lead

---

### Step Functions & Orchestration
- [ ] Step Functions state machine deployed (prod environment)
- [ ] State machine execution role has correct permissions
- [ ] Retry policies configured (exponential backoff, 3 attempts, DLQ routing on failure)
- [ ] Branching logic tested: extraction → transformation → entity resolution → serving
- [ ] Schema drift blocking logic tested (breaks transformation if breaking drift detected)
- [ ] Quality policy blocking logic tested (breaks entity resolution if quality fails)
- [ ] Dead-Letter Queue integration tested (failed messages appear in DLQ topic)
- [ ] CloudWatch alarms configured for Step Functions failures

**Owner:** Platform Engineering  
**Timeline:** 1 day  
**Sign-off:** [ ] Platform Engineering Lead

---

### Orchestration Schedules (EventBridge)
- [ ] EventBridge Scheduler configured with initial 5 entities (Salesforce Account, Contact, Opportunity, NetSuite Customer, MySQL Orders)
- [ ] Each entity has exactly one schedule (no duplicates)
- [ ] Schedule names follow `{source_id}--{entity_id}` convention
- [ ] Schedule times staggered to avoid concurrent source API load (e.g., SF Account 02:00, SF Contact 02:15, NS Customer 03:00)
- [ ] EventBridge execution role has permission to invoke Step Functions
- [ ] Schedules tested manually (manually trigger one step function execution, verify end-to-end)

**Owner:** Platform Engineering  
**Timeline:** 0.5 day  
**Sign-off:** [ ] Operations Lead

---

## Data Configuration

### Source Credentials (Secrets Manager)
- [ ] Salesforce credentials stored: `prod/sources/salesforce/credentials`
  - [ ] `instance_url` verified (correct org)
  - [ ] `client_id` and `client_secret` valid
  - [ ] OAuth token scope includes Bulk API 2.0
- [ ] NetSuite credentials stored: `prod/sources/netsuite/credentials`
  - [ ] `account_id` correct (not sandbox)
  - [ ] OAuth tokens valid
  - [ ] Timestamp format validated (ISO-8601)
- [ ] MySQL RDS credentials stored: `prod/sources/mysql-rds/credentials`
  - [ ] `host` is prod database endpoint
  - [ ] `username` is read-only user
  - [ ] Network connectivity verified (MySQL from Lambda in VPC)
- [ ] All credentials rotated within 90 days (prior to go-live)
- [ ] Secrets Manager rotation schedule configured (auto-rotate every 90 days)
- [ ] KMS key policy grants extraction service role `kms:Decrypt` permission

**Owner:** Data Engineering + Security  
**Timeline:** 0.5 day  
**Sign-off:** [ ] Security Officer, [ ] Data Engineering Lead

---

### Entity Configuration (DynamoDB)
- [ ] Configuration table populated with 5 initial entities
  - [ ] `salesforce-account` (incremental, SystemModstamp watermark, 1 day window)
  - [ ] `salesforce-contact` (incremental, SystemModstamp watermark, 1 day window)
  - [ ] `salesforce-opportunity` (incremental, CloseDate watermark, 1 day window)
  - [ ] `netsuite-customer` (incremental, dateCreated watermark, 1 day window)
  - [ ] `mysql-orders` (incremental, updated_at watermark, 4 hour window)
- [ ] Each entity record includes: `source_id`, `entity_id`, `load_type`, `watermark_field`, `extraction_window_days`, `field_mode`, `exclude_fields`
- [ ] All entities marked `active: True` (ready to extract)
- [ ] All entities have `created_at` timestamp (audit trail)

**Owner:** Data Engineering  
**Timeline:** 0.5 day  
**Sign-off:** [ ] Data Governance Lead

---

### Field Mapping Configuration (S3)
- [ ] Field mapping JSON files created for each source/entity pair (5 files total)
  - [ ] Salesforce Account mapping: source fields → canonical names (e.g., `Account_Name__c` → `account_name`)
  - [ ] Salesforce Contact mapping: source fields → canonical names
  - [ ] Salesforce Opportunity mapping: source fields → canonical names
  - [ ] NetSuite Customer mapping: source fields → canonical names
  - [ ] MySQL Orders mapping: source fields → canonical names
- [ ] All mapping files stored in S3: `s3://prod-schema-snapshots/field_mappings/`
- [ ] Each mapping file versioned (v1.json)
- [ ] All transformation Lambda has read permission to mapping bucket

**Owner:** Data Engineering  
**Timeline:** 1 day  
**Sign-off:** [ ] Data Quality Lead

---

### Data Classification & PII Policy (S3)
- [ ] PII classification policy file created: `s3://prod-schema-snapshots/data_classification/classification_policy.json`
- [ ] Every field in every entity classified as: `PII`, `SENSITIVE`, `PUBLIC`, or `INTERNAL`
- [ ] Masking strategy defined per field:
  - [ ] Email fields: MASK_EMAIL (mask domain, keep first char)
  - [ ] Phone fields: REDACT (replace with XXXX)
  - [ ] SSN/taxpayer ID: TOKENIZE (HMAC-SHA256)
  - [ ] Customer ID: HASH (irreversible)
- [ ] Transformation Lambda has read permission to classification policy file
- [ ] Policy reviewed by Data Governance + Compliance teams

**Owner:** Data Governance + Compliance  
**Timeline:** 1 day  
**Sign-off:** [ ] Chief Data Officer, [ ] Compliance Officer

---

### Entity Resolution Configuration (S3)
- [ ] Resolution config files created for planned entity types:
  - [ ] `config/entity_resolution/company/company_resolution_v1.json` (matches Salesforce Account + NetSuite Customer)
  - [ ] `config/entity_resolution/person/person_resolution_v1.json` (normalizes Salesforce Contact)
- [ ] Each config includes:
  - [ ] Matching rules (deterministic: exact field match; probabilistic: Levenshtein distance > 0.95)
  - [ ] Survivorship policy (which source wins for which field)
  - [ ] Output schema (`output_fields` list of 14 canonical fields)
  - [ ] System fields automatically appended (golden_id, contributing_records, survivorship_version, match_run_id, field_provenance)
- [ ] Resolution rules reviewed & approved by Data Governance

**Owner:** Entity Resolution Team  
**Timeline:** 1 day  
**Sign-off:** [ ] Data Governance Lead

---

## Quality & Governance

### Quality Policy (S3)
- [ ] Quality policy file created for each entity: `s3://prod-schema-snapshots/quality_policies/`
- [ ] Salesforce Account: enforce not-null on `account_id`, name regex, account_type enum
- [ ] Salesforce Contact: enforce not-null on `contact_id`, email regex, valid phone
- [ ] Salesforce Opportunity: enforce not-null on `opp_id`, positive amount range
- [ ] NetSuite Customer: enforce not-null on `customer_id`, email regex
- [ ] MySQL Orders: enforce not-null on `order_id`, positive order_amount, valid status
- [ ] All BLOCKING violations log to CloudWatch and trigger SNS alert
- [ ] All WARNING violations logged but don't block publication

**Owner:** Data Quality  
**Timeline:** 1 day  
**Sign-off:** [ ] Data Quality Lead

---

### Lineage & Audit Trail Setup
- [ ] DynamoDB audit-log table configured
- [ ] CloudWatch log group created: `/aws/lambda/extraction`, `/aws/lambda/transformation`, etc.
- [ ] CloudWatch custom metrics configured:
  - [ ] RecordsExtracted (per entity per run)
  - [ ] RecordsFailed (per entity per run)
  - [ ] WatermarkLagSeconds (per entity)
  - [ ] SchemaDriftCount (per entity)
  - [ ] RetryCount (per run)
- [ ] X-Ray tracing enabled on all Lambda functions
- [ ] S3 access logging enabled (all data buckets log to separate logging bucket)

**Owner:** Observability / DevOps  
**Timeline:** 0.5 day  
**Sign-off:** [ ] Observability Lead

---

### Alerting & Monitoring
- [ ] CloudWatch alarm created: "Extraction Failure" (SNS → Ops team email/Slack)
- [ ] CloudWatch alarm created: "Quality Blocking Violation" (SNS → Data team)
- [ ] CloudWatch alarm created: "Schema Breaking Drift" (SNS → Data Governance team)
- [ ] CloudWatch alarm created: "Watermark Lag > 26 hours" (SNS → Ops team)
- [ ] CloudWatch alarm created: "DLQ Message Age > 4 hours" (SNS → Ops team)
- [ ] Opsgenie/PagerDuty integration configured (alerts → on-call engineer)
- [ ] Dashboard created: "Data Lake Health" (shows key metrics + status per entity)
- [ ] Test alert mechanism (manually trigger one alarm, verify notification received)

**Owner:** Observability / DevOps  
**Timeline:** 1 day  
**Sign-off:** [ ] Ops Manager

---

## Security & Compliance

### Security Hardening
- [ ] All S3 buckets: public access blocked (Block all public access enabled)
- [ ] All S3 buckets: versioning enabled (recovery capability)
- [ ] All S3 buckets: encryption enabled (SSE-KMS with prod KMS key)
- [ ] All S3 buckets: access logging enabled (logs written to logging bucket)
- [ ] Raw layer S3 bucket: Object Lock enabled (GOVERNANCE mode, 7-year retention)
- [ ] Curated layer S3 bucket: Object Lock NOT enabled (append-only via partition structure)
- [ ] VPC: no internet gateway attached (all services via VPC endpoints)
- [ ] VPC: security groups restrict Lambda outbound to only Secrets Manager, S3, DynamoDB, CloudWatch
- [ ] IAM roles: no wildcard resource or action permissions (`Resource: "*"` or `Action: "*"` forbidden)
- [ ] All cross-account access: requires explicit resource-based policy (if applicable)
- [ ] KMS key policy: reviewed by Security team (grants minimal permissions)

**Owner:** Security / Platform Engineering  
**Timeline:** 0.5 day  
**Sign-off:** [ ] CISO

---

### Compliance Review
- [ ] GDPR readiness: lineage tracking + legal hold capability verified
- [ ] CCPA readiness: data inventory (Glue catalog) + access log capability verified
- [ ] SOC 2 readiness: audit trail (DynamoDB) + change control (Git) verified
- [ ] HIPAA readiness (if applicable): encryption + access control verified
- [ ] Data residency: all resources confirmed single region (no cross-region replication)
- [ ] Incident response plan reviewed: DLQ handling, schema drift escalation, quality failure escalation
- [ ] Compliance sign-off obtained before production extraction begins

**Owner:** Compliance + Legal  
**Timeline:** 0.5 day  
**Sign-off:** [ ] Chief Compliance Officer

---

## Testing & Validation

### Dry-run Extraction (Dev Environment)
- [ ] Extract Salesforce Account (first 100 records manually triggered) → verify raw Parquet in S3
- [ ] Extract NetSuite Customer → verify raw Parquet in S3
- [ ] Extract MySQL Orders → verify raw Parquet in S3
- [ ] Verify watermark advanced correctly (DynamoDB watermark table)
- [ ] Verify schema snapshot created (S3 schema bucket)
- [ ] Verify audit records written (DynamoDB audit-log table)

**Owner:** Platform Engineering  
**Timeline:** 0.5 day  
**Sign-off:** [ ] QA Lead

---

### Transformation Test (Dev → Staging)
- [ ] Transform raw Salesforce Account → curated layer
- [ ] Verify field mapping applied (source fields renamed correctly)
- [ ] Verify PII masking applied (emails masked, etc.)
- [ ] Verify quality checks run (report generated)
- [ ] Verify curated Parquet written to S3
- [ ] Verify Glue catalog entry created

**Owner:** Data Engineering  
**Timeline:** 0.5 day  
**Sign-off:** [ ] Data Quality Lead

---

### Entity Resolution Test
- [ ] Perform test resolution: Salesforce Account (1 record) + NetSuite Customer (1 record) matching → golden record produced
- [ ] Verify golden record schema matches declared output_fields (14 canonical fields + 5 system fields)
- [ ] Verify lineage record written to governance bucket
- [ ] Verify analytics layer Parquet written with canonical prefix

**Owner:** Entity Resolution Team  
**Timeline:** 0.5 day  
**Sign-off:** [ ] Data Governance Lead

---

### End-to-End Pipeline Test (Prod-like, on Staging)
- [ ] Trigger complete pipeline manually (extraction → transformation → entity resolution → analytics publish)
- [ ] Verify all stages complete within SLO window (< 4 hours total)
- [ ] Verify no data loss (raw record count ≈ curated record count within expected filter)
- [ ] Verify serving database load successful (if serving store enabled)
- [ ] Verify no CloudWatch alerts fired (baseline health check)
- [ ] Verify all audit records present in DynamoDB

**Owner:** QA + Platform Engineering  
**Timeline:** 1 day  
**Sign-off:** [ ] QA Lead, [ ] Platform Engineering Lead

---

### Failure Scenario Testing
- [ ] **Test 1:** Simulate source API timeout → verify Step Functions retries, eventually succeeds
- [ ] **Test 2:** Simulate breaking schema drift → verify transformation blocked, alert fired, raw data preserved
- [ ] **Test 3:** Simulate quality blocking violation → verify curated write skipped, previous data unchanged, alert fired
- [ ] **Test 4:** Simulate DLQ message → verify replay mechanism works, data reprocessed correctly
- [ ] **Test 5:** Simulate Lambda out-of-memory → verify Step Functions timeout respected, DLQ entry created

**Owner:** QA + Platform Engineering  
**Timeline:** 1 day  
**Sign-off:** [ ] QA Lead

---

## Training & Operations

### Runbooks & Documentation
- [ ] Extraction failure runbook created (symptoms → diagnosis → remediation steps)
- [ ] Schema drift alert runbook created (how to handle breaking vs. non-breaking drift)
- [ ] Quality failure runbook created (how to review reports, decide on remediation)
- [ ] DLQ message replay runbook created (how to manually trigger replay)
- [ ] Watermark reset runbook created (emergency watermark correction procedure)
- [ ] On-call escalation procedure documented (who to page for what failure type)
- [ ] All runbooks reviewed by operations team

**Owner:** Platform Engineering + Operations  
**Timeline:** 1 day  
**Sign-off:** [ ] Ops Manager

---

### Team Training
- [ ] Platform engineering team trained on: Lambda deployment, Step Functions, DynamoDB, S3 management
- [ ] Data engineering team trained on: Configuration management, field mapping edits, schema governance
- [ ] Data governance team trained on: Entity resolution rules, classification policy updates, lineage review
- [ ] Operations team trained on: Alert response, runbook execution, incident escalation
- [ ] BI analysts trained on: Athena query access, analytics layer schema, data freshness expectations

**Owner:** Platform Engineering Lead  
**Timeline:** 2 days (1 day training + 1 day hands-on)  
**Sign-off:** [ ] Training Coordinator

---

### Runbook Testing (Disaster Recovery)
- [ ] Practice extraction failure response: Simulate alert, follow runbook, verify resolution
- [ ] Practice schema drift response: Simulate breaking drift, follow runbook, verify manual intervention
- [ ] Practice DLQ replay: Manually enqueue failed message, trigger replay, verify recovery
- [ ] Practice on-call escalation: Trigger alert, page on-call engineer, verify communication

**Owner:** Operations + Platform Engineering  
**Timeline:** 0.5 day  
**Sign-off:** [ ] Ops Manager

---

## Go-Live Approval

### Final Sign-Offs

**Infrastructure:** [ ] Platform Engineering Lead  
**Data Configuration:** [ ] Data Engineering Lead  
**Security:** [ ] CISO  
**Compliance:** [ ] Chief Compliance Officer  
**Operations:** [ ] VP Operations / Ops Manager  
**Finance:** [ ] CFO (budget approved)  
**Data Governance:** [ ] Chief Data Officer  
**Executive Sponsor:** [ ] VP / SVP (business owner)

### Go-Live Timeline

Once all checklist items complete:

1. **Day 1:** Enable production extraction schedules (start with 1 entity as canary)
2. **Day 1-2:** Monitor alerts, verify extraction + transformation + entity resolution
3. **Day 2-3:** Enable remaining 4 entities (staggered, one per day)
4. **Day 3-4:** Serve curated data to BI tools; train users
5. **Day 4-7:** Monitor production metrics, refine alerts, adjust schedules if needed

**Rollback plan:** If critical issue found post-go-live, disable schedules (< 5 min), revert to previous data sources until platform stabilized.

---

**Prepared by:** Platform Engineering Lead  
**Date:** 2026-06-17  
**Next review:** Post-go-live Day 7 (day 1 incident review; day 7 week 1 retrospective)

