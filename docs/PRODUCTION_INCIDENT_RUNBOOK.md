# Production Incident Runbook

**For:** On-call engineers, operations team, support  
**Purpose:** Quick response guide for common incidents  
**Last updated:** 2026-06-17

---

## Quick Alert Reference

### Alert Priority Matrix

| Alert | Severity | Response time | Escalate to | Action |
|---|---|---|---|---|
| **Extraction Failure** | 🔴 High | 15 min | Data platform lead | Investigate source connectivity; check DynamoDB config |
| **Quality Blocking Violation** | 🟡 Medium | 30 min | Data quality team | Review quality report; decide on config change |
| **Schema Breaking Drift** | 🟡 Medium | 60 min | Data governance | Manual schema review; document change; approve transformation |
| **Watermark Lag > 26 hrs** | 🟡 Medium | 60 min | Data platform lead | Check extraction completion; manually advance if verified safe |
| **DLQ Message Age > 4 hrs** | 🟡 Medium | 30 min | Platform engineer | Check replay mechanism; trigger replay if safe |
| **Lambda Timeout** | 🟡 Medium | 30 min | Platform engineer | Analyze performance logs; increase timeout or scale extraction |
| **S3 Upload Failure** | 🔴 High | 5 min | AWS support (if quota exceeded) | Check S3 bucket permissions; verify KMS key access |
| **Watermark Advancement Blocked** | 🟡 Medium | 15 min | Database engineer | Check DynamoDB write throttling; verify optimistic lock logic |

---

## Runbooks by Scenario

---

## SCENARIO 1: Extraction Failure Alert

**Alert name:** `ExtractionFailureAlert`  
**Severity:** High  
**Typical root cause:** Source API temporarily unavailable, network connectivity issue, or auth failure

### Step 1: Gather Information (2 min)

```bash
# Get the specific failure from CloudWatch Logs
aws logs tail /aws/lambda/extraction --follow

# Check the failed execution in Step Functions
aws stepfunctions describe-execution \
  --execution-arn <ARN_from_alert> \
  --query 'output'

# Examine DynamoDB config to understand what entity failed
aws dynamodb get-item \
  --table-name prod-entity-extraction-config \
  --key '{"source_id":{"S":"salesforce"},"entity_id":{"S":"salesforce-account"}}'
```

### Step 2: Diagnosis (3 min)

**Check network connectivity:**
```bash
# Verify Lambda can reach Secrets Manager
aws secretsmanager get-secret-value \
  --secret-id prod/sources/salesforce/credentials \
  --query 'SecretString' | grep instance_url

# Verify Lambda can reach source API
curl -I https://YOUR_SALESFORCE_INSTANCE.salesforce.com/services/oauth2/token
```

**Check credentials expiration:**
```bash
# Salesforce OAuth token expiry
aws secretsmanager get-secret-value \
  --secret-id prod/sources/salesforce/credentials \
  --query 'SecretString' | jq .client_id

# Has credential been rotated recently?
aws secretsmanager describe-secret \
  --secret-id prod/sources/salesforce/credentials \
  --query 'RotationRules'
```

**Check source system status:**
- **Salesforce:** Check https://status.salesforce.com/ (Bulk API 2.0 or REST API down?)
- **NetSuite:** Check https://netsuite.status.io/ (REST API down?)
- **MySQL RDS:** Check AWS RDS dashboard (instance available? CPU/memory normal?)

### Step 3: Resolution

**If source API is temporarily down:**
```
Action: Wait 15 minutes, manually trigger re-run
aws stepfunctions start-execution \
  --state-machine-arn <STATE_MACHINE_ARN> \
  --input '{"source_id":"salesforce","entity_id":"salesforce-account","environment":"prod"}'
```

**If credentials are stale/expired:**
```bash
# Immediately rotate the credential
aws secretsmanager rotate-secret \
  --secret-id prod/sources/salesforce/credentials \
  --rotation-lambda-arn <ROTATION_LAMBDA_ARN> \
  --rotation-rules AutomaticallyAfterDays=90

# Manually update new OAuth token
aws secretsmanager put-secret-value \
  --secret-id prod/sources/salesforce/credentials \
  --secret-string '{"instance_url":"...","client_id":"NEW_ID","client_secret":"NEW_SECRET"}'

# Trigger re-run
aws stepfunctions start-execution \
  --state-machine-arn <STATE_MACHINE_ARN> \
  --input '{"source_id":"salesforce","entity_id":"salesforce-account","environment":"prod"}'
```

**If network is unreachable:**
```
Action: Check VPC security group rules allow outbound to source
aws ec2 describe-security-groups --group-ids <LAMBDA_SG> | grep -A 5 IpPermissionsEgress
Verify: Outbound HTTPS (443) allowed to source IP/domain
If missing: Add rule via Terraform or AWS console
```

### Step 4: Post-Incident

- [ ] Document root cause in incident tracker
- [ ] If credential issue: add calendar reminder for next rotation date
- [ ] If network issue: add monitoring for source connectivity
- [ ] Notify data team: extraction was delayed but will complete in next scheduled window

---

## SCENARIO 2: Quality Blocking Violation

**Alert name:** `QualityBlockingViolationAlert`  
**Severity:** Medium  
**Typical root cause:** Source data changed; quality policy needs update; or bad data in source

### Step 1: Gather Information (3 min)

```bash
# Get the quality report from S3
aws s3 cp s3://prod-edl-analytics/quality_reports/salesforce-account/2026-06-17.json - | jq '.blocking_violations'

# Count failed records
jq '.summary.total_records_failed' < 2026-06-17.json

# What field failed?
jq '.blocking_violations[] | {field: .field_name, check_type: .check_type, violation_count: .violation_count}' < 2026-06-17.json
```

### Step 2: Investigation (5–10 min)

**Query the raw data to understand the issue:**

```sql
-- Run this in Athena against raw layer
SELECT 
  COUNT(*) as violation_count,
  account_name,
  NULL as account_name_is_null
FROM `dev-edl-raw-layer`.`salesforce_account`
WHERE account_name IS NULL
  AND partition_date = '2026-06-17'
GROUP BY account_name
LIMIT 10;

-- Or if it's a pattern violation (e.g., invalid email):
SELECT 
  email,
  COUNT(*) as count
FROM `dev-edl-raw-layer`.`salesforce_contact`
WHERE email NOT LIKE '%@%.%'
  AND partition_date = '2026-06-17'
GROUP BY email
LIMIT 10;
```

### Step 3: Decision (2 min)

**Option A: Source data is genuinely bad (missing values, invalid formats)**
```
Action: Contact data owner in source system (Salesforce admin, NetSuite admin, etc.)
Goal: Fix the source data; platform will pick it up on next run
Example: "500 Account records have null Name field (required in CRM)" → Salesforce team investigates & fixes
Timeline: Wait for fix, then trigger manual re-run
```

**Option B: Quality policy is too strict**
```
Action: Update quality policy to match actual data
Example: Email field had new valid format not covered by regex
Timeline: Update S3 quality policy file, test in staging, deploy to prod
```

**Option C: This is expected data variance**
```
Action: Convert BLOCKING rule to WARNING (doesn't stop publication)
Example: Order_amount NULL for cancelled orders is expected
Timeline: Update policy, re-run transformation
```

### Step 4: Remediation

**Update quality policy (if Option B or C):**

```bash
# Download current policy
aws s3 cp s3://prod-schema-snapshots/quality_policies/salesforce-account.json ./

# Edit policy file: change blocking violation to WARNING or update regex pattern
# Example: change email pattern from strict to permissive
# vi salesforce-account.json

# Verify syntax
python -m json.tool salesforce-account.json

# Upload updated policy
aws s3 cp salesforce-account.json s3://prod-schema-snapshots/quality_policies/

# Trigger transformation re-run with new policy
aws lambda invoke \
  --function-name transformation-pipeline \
  --payload '{"source_id":"salesforce","entity_id":"salesforce-account","environment":"prod","run_id":"<RUN_ID>"}' \
  response.json
```

### Step 5: Notification

- [ ] Document decision (which option chosen, who approved)
- [ ] Notify data quality team of policy change
- [ ] If source data issue: create ticket with data owner, set follow-up date
- [ ] Verify curated data published successfully on next re-run

---

## SCENARIO 3: Schema Breaking Drift

**Alert name:** `SchemaDriftAlert` (breaking severity)  
**Severity:** Medium  
**Typical root cause:** Source system schema changed (field removed, type changed, or made mandatory)

### Step 1: Review Drift Report (2 min)

```bash
# Get the drift report from S3
aws s3 cp s3://prod-schema-snapshots/drift_reports/salesforce-account/2026-06-17.json - | jq '.'

# Example output:
# {
#   "drift_classification": "BREAKING",
#   "changes": [
#     {
#       "field_name": "LegacyAccountId__c",
#       "change_type": "FIELD_REMOVED",
#       "previous_type": "string",
#       "current_type": null
#     }
#   ]
# }
```

### Step 2: Investigation (5 min)

**Verify the change in the source system:**

```bash
# For Salesforce: check field history
# For NetSuite: check saved search for field availability
# For MySQL: run DESCRIBE table statement

# Example for MySQL:
aws rds-data execute-statement \
  --resource-arn "arn:aws:rds:us-east-1:ACCOUNT_ID:db:prod-rds-instance" \
  --secret-arn "arn:aws:secretsmanager:us-east-1:ACCOUNT_ID:secret:prod/sources/mysql-rds/credentials" \
  --sql "DESCRIBE prod_schema.orders"
```

### Step 3: Governance Review (5–15 min)

**Convene data governance team:**

1. Is the removed/changed field critical for analytics?
   - If no: Approve transformation to proceed (ignore removed field)
   - If yes: Block transformation; require manual data reconciliation

2. Update schema snapshot + field mapping to reflect change

3. Document decision in audit trail

### Step 4: Remediation

**If field removal is non-critical:**

```bash
# Update schema snapshot to reflect new schema
aws s3 cp s3://prod-schema-snapshots/schemas/salesforce-account/latest.json ./schema-latest.json

# Update field mapping to exclude removed field
aws s3 cp s3://prod-schema-snapshots/field_mappings/salesforce-account/v1.json ./mapping-v1.json
# Edit: remove any reference to LegacyAccountId__c
# Save as v2.json

aws s3 cp ./mapping-v2.json s3://prod-schema-snapshots/field_mappings/salesforce-account/v2.json

# Update entity config to reference new mapping version
aws dynamodb update-item \
  --table-name prod-entity-extraction-config \
  --key '{"source_id":{"S":"salesforce"},"entity_id":{"S":"salesforce-account"}}' \
  --attribute-updates '{"field_mapping_version":{"Value":{"S":"v2"},"Action":"PUT"}}'

# Trigger transformation re-run
aws lambda invoke \
  --function-name transformation-pipeline \
  --payload '{"source_id":"salesforce","entity_id":"salesforce-account","environment":"prod"}' \
  response.json
```

### Step 5: Notification

- [ ] Document schema change in data catalog / Glue
- [ ] Notify downstream analytics teams: "Salesforce Account field removed (non-critical); curated data updated"
- [ ] Schedule sync meeting with Salesforce admin to understand why field was removed (prevent future surprises)

---

## SCENARIO 4: Watermark Lag Alert

**Alert name:** `WatermarkLagAlert`  
**Severity:** Medium  
**Typical root cause:** Extraction is running but slowly; incremental window is far behind current time

### Step 1: Check Extraction Status (2 min)

```bash
# Get latest extraction run for the entity
aws dynamodb query \
  --table-name prod-watermark-repository \
  --key-condition-expression "source_id = :source AND entity_id = :entity" \
  --expression-attribute-values '{":source":{"S":"salesforce"},":entity":{"S":"salesforce-account"}}' \
  --sort-order Descending \
  --limit 1

# Example output:
# {
#   "source_id": "salesforce",
#   "entity_id": "salesforce-account",
#   "last_successful_extraction_time": "2026-06-16T02:00:00Z",
#   "watermark_value": "2026-06-16T02:00:00Z",
#   "lag_seconds": 86400  # 24 hours
# }
```

### Step 2: Diagnosis

**Check if extraction is still running:**

```bash
# Get the most recent execution
aws stepfunctions list-executions \
  --state-machine-arn <STATE_MACHINE_ARN> \
  --status-filter RUNNING \
  | jq '.executions[0]'

# If running: check elapsed time
# If > 15 minutes: may be processing large volume

# Get execution details
aws stepfunctions describe-execution \
  --execution-arn <EXECUTION_ARN> \
  | jq '{status: .status, startDate: .startDate, stopDate: .stopDate}'

# Check Lambda logs to see progress
aws logs tail /aws/lambda/extraction --since 30m --follow
```

**If extraction failed:**
```bash
# Go to SCENARIO 1: Extraction Failure Alert
```

**If extraction is running but slow:**
```bash
# Check raw data write throughput
aws s3api list-objects-v2 \
  --bucket prod-raw-layer \
  --prefix salesforce/salesforce-account/extraction_date=2026-06-17 \
  --query 'Contents | length'

# If hundreds of files: extraction is progressing (slow source API)
# Expected time: 10–15 minutes for large entities (500k+ records)
```

### Step 3: Action

**If extraction is slow but progressing:**
```
Action: Wait another 30 minutes; monitor Lambda execution logs
No action needed; this is expected for high-volume extractions
```

**If extraction is stuck (no new files created in 10 min):**
```bash
# Check if Lambda is hitting timeout
aws logs filter-log-events \
  --log-group-name /aws/lambda/extraction \
  --filter-pattern "Task timed out" \
  --since 30m

# If timeout found: increase Lambda timeout in terraform/environment/prod/main.tf
# Lambda timeout: 15 minutes → consider 20 minutes for very large entities
# Redeploy Lambda
terraform apply -target="aws_lambda_function.extraction" -var="extraction_timeout_sec=1200"
```

---

## SCENARIO 5: DLQ Message Age > 4 Hours

**Alert name:** `DLQMessageAgeAlert`  
**Severity:** Medium  
**Typical root cause:** Failed run was queued but not manually replayed

### Step 1: Check DLQ Contents (2 min)

```bash
# List messages in DLQ (SNS topic)
aws sns list-subscriptions-by-topic \
  --topic-arn <DLQ_TOPIC_ARN>

# Get the message details
aws sns receive-message \
  --queue-url <DLQ_QUEUE_URL> \
  | jq '.Messages[0]'

# Example DLQ message:
# {
#   "run_id": "run-20260617-020045678-xyz",
#   "source_id": "salesforce",
#   "entity_id": "salesforce-account",
#   "failed_stage": "EXTRACTION",
#   "error_message": "Source API returned 429 (rate limit exceeded)",
#   "enqueued_at": "2026-06-17T02:15:00Z"
# }
```

### Step 2: Decide on Replay

**Check if the issue is resolved:**

```bash
# If error was "rate limit exceeded": Wait 1 hour, then replay
# If error was "credential invalid": Check if credential was rotated; if yes, replay
# If error was "network timeout": Check source status; if recovered, replay
```

### Step 3: Manual Replay

```bash
# Use the RunReplayController to re-run the failed extraction
aws lambda invoke \
  --function-name run-replay-controller \
  --payload '{
    "run_id":"run-20260617-020045678-xyz",
    "source_id":"salesforce",
    "entity_id":"salesforce-account",
    "environment":"prod"
  }' \
  response.json

# Monitor the replay
aws logs tail /aws/lambda/extraction --since 1m --follow --filter-pattern "run-20260617"
```

### Step 4: Verification

```bash
# Check if replay completed successfully
aws dynamodb query \
  --table-name prod-run-audit-log \
  --key-condition-expression "run_id = :run_id" \
  --expression-attribute-values '{":run_id":{"S":"run-20260617-020045678-xyz"}}' \
  | jq '.Items[] | {stage: .stage, status: .status}'

# Expected: all stages completed with status=SUCCESS
```

### Step 5: Documentation

- [ ] Record replay in incident tracker
- [ ] Note root cause and resolution
- [ ] Alert threshold check: Is 4-hour DLQ age threshold still appropriate? Consider adjusting if frequent

---

## SCENARIO 6: Lambda Out-of-Memory Error

**Alert name:** `LambdaOutOfMemoryAlert`  
**Severity:** High  
**Typical root cause:** Entity extraction volume exceeds allocated Lambda memory

### Step 1: Confirm OOM (1 min)

```bash
# Check Lambda logs for OOM error
aws logs tail /aws/lambda/extraction --filter-pattern "OutOfMemory" --since 10m

# Get Lambda memory configuration
aws lambda get-function-configuration \
  --function-name extraction-pipeline \
  | jq '.MemorySize'

# Typical: 512 MB (default)
```

### Step 2: Diagnosis

```bash
# Check extraction volume for the entity that OOMed
# Estimate: ~2 KB per record in memory (depends on field count)

# If extracting 250k records: 250k × 2KB = 500 MB
# If extracting 500k+ records: Will exceed 512 MB

# Check how many records were attempted:
aws logs filter-log-events \
  --log-group-name /aws/lambda/extraction \
  --filter-pattern "TotalRecordsExtracted" \
  | jq '.events[-1].message' | grep TotalRecordsExtracted
```

### Step 3: Resolution

**Option A: Increase Lambda memory**

```bash
# Edit Terraform
# infrastructure/environments/prod/main.tf
# module "extraction_lambda" {
#   memory_size = 1024  # Increase from 512 to 1024 MB
# }

terraform plan -target="aws_lambda_function.extraction"
terraform apply -target="aws_lambda_function.extraction"

# Re-run extraction
aws stepfunctions start-execution \
  --state-machine-arn <STATE_MACHINE_ARN> \
  --input '{"source_id":"salesforce","entity_id":"salesforce-account","environment":"prod"}'
```

**Option B: Switch to ECS Fargate (for very large entities)**

If entity exceeds 10M records/day, Lambda becomes impractical. Migrate to ECS:

```bash
# Add ECS task definition for this entity
# Edit extraction config in DynamoDB:
aws dynamodb update-item \
  --table-name prod-entity-extraction-config \
  --key '{"source_id":{"S":"salesforce"},"entity_id":{"S":"salesforce-account"}}' \
  --attribute-updates '{"compute_type":{"Value":{"S":"ECS_FARGATE"},"Action":"PUT"}}'

# Update Step Functions to route to ECS task instead of Lambda
terraform apply -target="aws_ecs_task_definition.extraction"
```

### Step 4: Long-term Monitoring

- [ ] Add CloudWatch alarm: "Lambda memory utilization > 90%"
- [ ] Track record volume per entity; plan memory/compute upgrades ahead of growth
- [ ] For rapid-growth entities, consider scheduling migration to ECS proactively

---

## SCENARIO 7: Deadletter Queue Configuration Issue

**Alert name:** Custom monitoring  
**Severity:** Medium  
**Typical root cause:** DLQ topic deleted, subscription removed, or permissions changed

### Recovery

```bash
# Verify DLQ topic exists
aws sns list-topics | grep dlq

# If not found: recreate from Terraform
terraform apply -target="aws_sns_topic.pipeline_failure_dlq"

# Verify Step Functions can publish to DLQ
aws sns get-topic-attributes \
  --topic-arn <DLQ_TOPIC_ARN> \
  | jq '.Attributes.Policy'

# Verify email subscription is active
aws sns list-subscriptions-by-topic \
  --topic-arn <DLQ_TOPIC_ARN>

# If subscription removed: re-add
aws sns subscribe \
  --topic-arn <DLQ_TOPIC_ARN> \
  --protocol email \
  --notification-endpoint ops-team@company.com
```

---

## Escalation Matrix

| Scenario | Escalate to | When |
|---|---|---|
| Extraction failure (> 3 consecutive retries) | Data platform lead + AWS support | After 2 hours unresolved |
| Quality blocking violation (> 50% of records) | Chief Data Officer | Immediate (data quality issue) |
| Schema breaking drift | Data governance + Chief Data Officer | Within 1 hour |
| Watermark lag (> 48 hours) | VP of Data + on-call engineer | Immediate |
| DLQ aging (> 8 hours) | VP of Operations | Within 4 hours |
| Network/VPC issue | AWS support + infrastructure team | Immediate |
| Secrets rotation failed | Security team + AWS support | Immediate |

---

## Post-Incident Review Checklist

After every production incident:

- [ ] Documented root cause in ticket system
- [ ] Identified contributing factors (monitoring gap, config error, source system issue)
- [ ] Proposed permanent fix (if applicable) or added monitoring (if detection gap)
- [ ] Created follow-up ticket if fix requires code change / deployment
- [ ] Notified all impacted teams (data team, analytics team, compliance if data quality issue)
- [ ] Added new runbook or updated existing if unclear
- [ ] Scheduled training for team on the incident resolution
- [ ] Updated SLOs if response time was insufficient

---

**Last updated:** 2026-06-17  
**Owner:** Platform Engineering Lead  
**Review cycle:** Monthly (or after major incident)

