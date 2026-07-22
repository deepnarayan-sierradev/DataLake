# Production Incident Runbook

**For:** On-call engineers, operations team, support  
**Purpose:** Quick response guide for common incidents  
**Last updated:** 2026-07-14

> **Lambda functions:** `EdlExtractionPipeline` · `EdlTransformationPipeline` · `EdlEntityResolutionPipeline` · `EdlAnalyticsLayerPublisher`  
> **Key buckets (prod):** `edl-raw-<PROD_ACCOUNT_ID>` · `edl-curated-<PROD_ACCOUNT_ID>` · `edl-analytics-<PROD_ACCOUNT_ID>` · `edl-schema-snapshots-<PROD_ACCOUNT_ID>`  
> Resource names no longer carry an environment prefix — dev/staging/prod each live in their own AWS account, so the same PascalCase name (e.g. `EdlExtractionPipeline`) resolves in every account; only S3 bucket names differ per environment, ending in that account's ID instead of an env prefix. See [PLATFORM_STATUS.md](PLATFORM_STATUS.md) for all resource names.

> ### Current environment reality — read before paging anyone
>
> No production environment exists yet — only `dev` is deployed (Salesforce and MySQL RDS have
> run end-to-end with real data). See [PLATFORM_STATUS.md](PLATFORM_STATUS.md) for current,
> authoritative environment status before treating any step below as something you can run
> against a live `prod` today; until `prod` exists, substitute `dev`'s real bucket/table names and
> account ID when actually running a command.
>
> `terraform apply`/`destroy` against `infrastructure/environments/prod` and `git push --force`
> are hard-blocked at the tool level in Claude Code sessions (`.claude/settings.json`), regardless
> of who's asking. If you're working an incident through an agent session and a step below
> genuinely requires one of those, a human needs to run it directly — the agent cannot.

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
  --table-name EdlEntityExtractionConfig \
  --key '{"source_id":{"S":"salesforce"},"entity_id":{"S":"salesforce-account"}}'
```

### Step 2: Diagnosis (3 min)

**Check network connectivity:**
```bash
# Verify Lambda can reach Secrets Manager
aws secretsmanager get-secret-value \
  --secret-id edl/sources/salesforce/credentials \
  --query 'SecretString' | grep instance_url

# Verify Lambda can reach source API
curl -I https://YOUR_SALESFORCE_INSTANCE.salesforce.com/services/oauth2/token
```

**Check credentials expiration:**
```bash
# Salesforce OAuth token expiry
aws secretsmanager get-secret-value \
  --secret-id edl/sources/salesforce/credentials \
  --query 'SecretString' | jq .client_id

# Has credential been rotated recently?
aws secretsmanager describe-secret \
  --secret-id edl/sources/salesforce/credentials \
  --query 'RotationRules'
```

**Check source system status:**
- **Salesforce:** Check https://status.salesforce.com/ (Bulk API 2.0 or REST API down?)
- **NetSuite:** Check https://netsuite.status.io/ (REST API down?)
- **MySQL RDS:** Check AWS RDS dashboard (instance available? CPU/memory normal?)
- **Sage Intacct:** Check https://status.intacct.com/ (API or authentication service down?)
- **Sage X3:** Check your X3 server's OData service endpoint reachability (no public status page — contact Sage X3 admin)

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
  --secret-id edl/sources/salesforce/credentials \
  --rotation-lambda-arn <ROTATION_LAMBDA_ARN> \
  --rotation-rules AutomaticallyAfterDays=90

# Manually update new OAuth token
aws secretsmanager put-secret-value \
  --secret-id edl/sources/salesforce/credentials \
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
aws s3 cp s3://edl-analytics-<PROD_ACCOUNT_ID>/quality_reports/salesforce-account/2026-06-17.json - | jq '.blocking_violations'

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
FROM `edl-raw-087972550871`.`salesforce_account`
WHERE account_name IS NULL
  AND partition_date = '2026-06-17'
GROUP BY account_name
LIMIT 10;

-- Or if it's a pattern violation (e.g., invalid email):
SELECT 
  email,
  COUNT(*) as count
FROM `edl-raw-087972550871`.`salesforce_contact`
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
aws s3 cp s3://edl-schema-snapshots-<PROD_ACCOUNT_ID>/quality_policies/salesforce-account.json ./

# Edit policy file: change blocking violation to WARNING or update regex pattern
# Example: change email pattern from strict to permissive
# vi salesforce-account.json

# Verify syntax
python -m json.tool salesforce-account.json

# Upload updated policy
aws s3 cp salesforce-account.json s3://edl-schema-snapshots-<PROD_ACCOUNT_ID>/quality_policies/

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
aws s3 cp s3://edl-schema-snapshots-<PROD_ACCOUNT_ID>/drift_reports/salesforce-account/2026-06-17.json - | jq '.'

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
  --secret-arn "arn:aws:secretsmanager:us-east-1:ACCOUNT_ID:secret:edl/sources/mysql-rds/credentials" \
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
aws s3 cp s3://edl-schema-snapshots-<PROD_ACCOUNT_ID>/schemas/salesforce-account/latest.json ./schema-latest.json

# Update field mapping to exclude removed field
aws s3 cp s3://edl-schema-snapshots-<PROD_ACCOUNT_ID>/field_mappings/salesforce-account/v1.json ./mapping-v1.json
# Edit: remove any reference to LegacyAccountId__c
# Save as v2.json

aws s3 cp ./mapping-v2.json s3://edl-schema-snapshots-<PROD_ACCOUNT_ID>/field_mappings/salesforce-account/v2.json

# Update entity config to reference new mapping version
aws dynamodb update-item \
  --table-name EdlEntityExtractionConfig \
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
# Get latest extraction run for the entity.
# NOTE: EdlWatermarkRepository's "source_id" key attribute stores
# tenant_scoped_key(tenant_code, source_id), i.e. "{tenant_code}#{source_id}"
# (contracts/identifier_policy.py) — NOT the bare source_id. This is
# genuinely key-level tenant isolation (see docs/PIPELINE_FLOW.md's canonical
# isolation table). For the default tenant that's "demo#salesforce", not
# "salesforce". Querying with the bare source_id returns zero items, which
# looks identical to "no watermark yet" — confirm the tenant_code from the
# alert/run_id before assuming this is a fresh entity.
aws dynamodb query \
  --table-name EdlWatermarkRepository \
  --key-condition-expression "source_id = :source AND entity_id = :entity" \
  --expression-attribute-values '{":source":{"S":"demo#salesforce"},":entity":{"S":"salesforce-account"}}'

# Example output:
# {
#   "source_id": "demo#salesforce",
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
  --bucket edl-raw-<PROD_ACCOUNT_ID> \
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

The DLQ is an SQS queue (`extraction_failure_dlq`, `infrastructure/modules/metadata_persistence/main.tf`),
not an SNS topic — alarm notifications go through a separate SNS topic (`platform_alerts`); see
Scenario 7 below for that distinction.

```bash
# Peek at DLQ messages without deleting them (visibility-timeout 0)
aws sqs receive-message \
  --queue-url <DLQ_QUEUE_URL> \
  --max-number-of-messages 10 \
  --visibility-timeout 0 \
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
  --table-name EdlRunAuditLog \
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

**Option B: ECS Fargate — not built, do not attempt mid-incident**

There is no ECS Fargate path in this platform today — no `aws_ecs_*` Terraform resource exists
anywhere in `infrastructure/`, `compute_type` is not a real field on the entity config contract,
and Step Functions has no ECS-routing branch to send a run to. If an entity is genuinely too large
for Lambda (Option A's memory ceiling reached), the real options are: reduce the extraction window
(`extraction_window_days`) to shrink each run's record count, or treat this as a platform
enhancement request — not something to hand-roll during an incident. Do not run
`terraform apply -target="aws_ecs_task_definition.extraction"`; that resource does not exist and
the command will fail.

### Step 4: Long-term Monitoring

- [ ] Add CloudWatch alarm: "Lambda memory utilization > 90%"
- [ ] Track record volume per entity; plan memory/compute upgrades ahead of growth
- [ ] For rapid-growth entities, consider scheduling migration to ECS proactively

---

## SCENARIO 7: DLQ Queue or Alerting Topic Configuration Issue

**Alert name:** Custom monitoring  
**Severity:** Medium  
**Typical root cause:** DLQ queue policy/permissions changed, or the SNS alerting topic's
subscription was removed

There are two distinct resources here — don't conflate them:

- **`extraction_failure_dlq`** — an SQS queue (`infrastructure/modules/metadata_persistence/main.tf`)
  that actually holds failed-run messages for replay.
- **`platform_alerts`** — a separate SNS topic (`infrastructure/modules/observability/main.tf`)
  that CloudWatch alarms publish to, fanning out to email/PagerDuty. It does not hold DLQ messages;
  it only carries alarm notifications, including the DLQ-depth alarm that fires when
  `extraction_failure_dlq` backs up.

### Recovery — DLQ queue (SQS)

```bash
# Verify the queue exists
aws sqs get-queue-url --queue-name extraction_failure_dlq

# If not found: recreate from Terraform
terraform apply -target="aws_sqs_queue.extraction_failure_dlq"

# Verify the queue policy still allows Step Functions / the extraction Lambda to send messages
aws sqs get-queue-attributes \
  --queue-url <DLQ_QUEUE_URL> \
  --attribute-names Policy RedrivePolicy
```

### Recovery — alerting topic (SNS)

```bash
# Verify the topic exists
aws sns list-topics | grep platform_alerts

# If not found: recreate from Terraform
terraform apply -target="aws_sns_topic.platform_alerts"

# Verify email/PagerDuty subscription is still active
aws sns list-subscriptions-by-topic --topic-arn <PLATFORM_ALERTS_TOPIC_ARN>

# If the subscription was removed: re-add
aws sns subscribe \
  --topic-arn <PLATFORM_ALERTS_TOPIC_ARN> \
  --protocol email \
  --notification-endpoint ops-team@company.com
```

---

## SCENARIO 8: Suspected Cross-Tenant Data Incident

**Alert name:** Manual escalation (no dedicated alarm yet — see "Detection gap" below)
**Severity:** Critical
**Typical root cause:** A regression in an application-level tenant guard, a misconfigured
control-plane request, a wildcard Lake Formation grant reaching another tenant's data, or a
manual/ad-hoc AWS CLI operation that bypassed the platform's own code paths.

### Which layer leaked — check the canonical table first

Tenant isolation is not uniform across layers — some are genuinely key/prefix-enforced, some are
application-level guards only, and some (Secrets Manager, Glue/Athena) aren't isolated at all
today. **Don't re-derive this from memory** — check
[`docs/PIPELINE_FLOW.md`'s "Multi-tenancy — the canonical isolation model"](PIPELINE_FLOW.md#multi-tenancy--the-canonical-isolation-model)
for the current, authoritative layer-by-layer table before triaging. Every open gap behind these
mechanisms (no IAM enforcement anywhere, shared Secrets Manager credentials, the raw-layer S3 gap,
the `entity-extraction-config` key-level gap, etc.) is tracked in detail in
[`docs/KNOWN_GAPS_AND_ROADMAP.md`](KNOWN_GAPS_AND_ROADMAP.md).

Two of those gaps come up often enough in practice to flag explicitly:

- **Glue/Athena is a wildcard grant, not per-tenant isolation.** Three IAM principals configured
  in dev's `terraform.tfvars` (`analytics_reader_principals`) hold a Lake Formation
  `SELECT`+`DESCRIBE` grant with `wildcard = true` across the whole shared `edl_curated`/
  `edl_analytics` database — meaning each of those principals can already query every tenant's
  tables, not just one. A "cross-tenant Athena query" report may not be a regression at all — check
  whether it's this grant working exactly as configured before assuming a code bug.
- **The serving store's isolation is solid but currently unreachable from outside the VPC** (no
  VPN/PrivateLink/bastion exists yet). That's a network-reachability gap, not a data leak — if the
  report is actually "a tenant can't connect their BI tool" rather than "a tenant saw another
  tenant's data," see Scenario 9 below instead of triaging it here.

An automated regression test for every key/prefix-level and application-guard mechanism (all except
Secrets Manager and Glue/Athena) lives in `tests/test_tenant_isolation.py` — this is the single
place to check whether isolation itself has a known, tested gap before assuming a live incident is
novel.

### Step 1: Establish Blast Radius (5 min)

1. **Identify the tenant_code(s) involved.** Every structured log line, DynamoDB item, and S3 key
   under the mechanisms above carries `tenant_code`. Pull the specific `run_id` or record from the
   alert/report and read its `tenant_code` field directly — do not infer it from context.
2. **Determine which layer leaked.** Cross-reference against the canonical table in
   `docs/PIPELINE_FLOW.md`. A leak in `entity-extraction-config` is an application-code regression
   in `_enforce_tenant_match`, not an IAM failure — IAM was never enforcing it. A leak in S3,
   `watermark-repository`, or the `entity-type-registry` table means the key/prefix isolation
   mechanism itself failed (a code regression that changed how the key is built, not a missing
   check on top of an already-shared key) — treat this as more severe than a config-table leak,
   since there's no secondary guard behind it.
3. **Scope the affected record set:**

```bash
# Find every run for a suspect tenant in the audit log (Scan — no tenant-code GSI yet)
aws dynamodb scan \
  --table-name EdlRunAuditLog \
  --filter-expression "tenant_code = :tc" \
  --expression-attribute-values '{":tc":{"S":"<SUSPECT_TENANT_CODE>"}}'

# Find every S3 object under a tenant's prefix (any bucket)
aws s3 ls s3://edl-curated-<PROD_ACCOUNT_ID>/<SUSPECT_TENANT_CODE>/ --recursive
aws s3 ls s3://edl-raw-<PROD_ACCOUNT_ID>/<SUSPECT_TENANT_CODE>/ --recursive
```

### Step 2: Contain (10 min)

- If the leak is at the **control-plane API**: revoke the offending Cognito refresh token / disable
  the user pool app client temporarily if the auth check itself is compromised (not just a single
  bad request).
- If the leak is in the **`entity-extraction-config` application-level guard**: the underlying data
  was never IAM-isolated, so containment means fixing and redeploying the guard code immediately
  (`connector_runtime/configuration_repository/configuration_repository.py`), not just an
  access-policy change.
- If the leak is at the **S3, watermark-repository, or entity-type-registry layer**: these are the
  mechanisms the platform advertises as genuinely key/prefix-enforced — a leak here means the key
  construction itself regressed (`watermark_management/watermark_repository/watermark_repository.py`
  for watermarks). Treat as a security incident requiring immediate IAM policy review
  (`infrastructure/modules/iam/main.tf`) in addition to the code fix.

### Step 3: Confirm the Fix (10 min)

Run `tests/test_tenant_isolation.py` against the patched code before redeploying:

```bash
.venv/bin/pytest tests/test_tenant_isolation.py -v --no-cov
```

If the incident revealed a gap this file doesn't cover, add a new regression test to it as part of
the fix — the exit criterion for closing the incident is a red-then-green test, not just a manual
verification.

### Step 4: Notification and Post-Incident

- [ ] Notify both tenants' points of contact if either tenant's data was exposed to the other,
      per the data processing agreement / contractual notification SLA
- [ ] Document which layer failed (application guard vs. IAM) — this determines whether the fix is
      a code patch, a Terraform change, or both
- [ ] Add a regression test to `tests/test_tenant_isolation.py` covering the exact failure mode
- [ ] Re-run the full isolation test suite plus `terraform plan` for the affected IAM module before
      the next deploy
- [ ] If the leak was in `entity-extraction-config` (the one table still on an application-level
      guard, not key-level isolation), escalate the underlying tenant-key partitioning work tracked
      in `docs/KNOWN_GAPS_AND_ROADMAP.md` — an application bug is the second line of defense, not
      the first, and there currently is no first line for that table

### Detection gap

There is no dedicated CloudWatch alarm today that would catch a cross-tenant read as it happens —
detection currently relies on this runbook being invoked after a report (customer complaint, code
review finding an unguarded call site, or a failing `tests/test_tenant_isolation.py` run in CI).
Adding a real-time detection mechanism (e.g., a metric filter on the `curated_prefix_*_rejected`
structured log events, which already fire when `find_latest_curated_prefix`'s path-traversal guard
rejects an unsafe prefix, or on `ConfigurationRepositoryClient`'s `_enforce_tenant_match` rejection
path for `entity-extraction-config`) is tracked as follow-up observability work, not yet
implemented. Note `watermark-repository` has no equivalent mismatch log event to filter on — since
its key is already tenant-scoped, a cross-tenant watermark read simply misses the key entirely (a
normal "no prior watermark" `get_item` miss), which is indistinguishable in logs from a genuine
first run.

---

## SCENARIO 9: Tenant Reports Their BI Tool Cannot Connect to the Serving Store

**Alert name:** Support ticket / manual report (no dedicated alarm)  
**Severity:** Medium  
**Typical root cause:** A known, currently-unresolved network-reachability gap — not a credential
or tenant-isolation bug. Read Step 0 before spending time elsewhere.

### Step 0: Rule out the known gap first (2 min)

The serving store's per-tenant database/schema and credential isolation is implemented correctly
(one database per tenant for MySQL; one schema per tenant for PostgreSQL/SQL Server/Azure SQL/Redshift),
but the RDS instance (`infrastructure/modules/serving_store_database/main.tf`) — and, for the
Redshift engine, the Redshift Serverless workgroup (`infrastructure/modules/serving_store_redshift/main.tf`) —
is `publicly_accessible = false`, sits in private subnets, and its security group only allows inbound
traffic from the loader Lambda's own security group. There is no VPN, PrivateLink, or bastion host
anywhere in `infrastructure/modules/networking/` yet — so no external Power BI/Tableau connection
can reach the database today, for any tenant, on any engine, regardless of credentials. There is also no
script/API yet to hand a tenant its reader credential once connectivity exists. Full detail and
candidate fix options (Client VPN, PrivateLink, site-to-site) are in
[`docs/KNOWN_GAPS_AND_ROADMAP.md`](KNOWN_GAPS_AND_ROADMAP.md).

Confirm the serving store is even deployed in this environment before going further — see current
status in [`docs/PLATFORM_STATUS.md`](PLATFORM_STATUS.md) (code-complete as of 2026-07-11, not yet
applied anywhere as of this writing):

```bash
terraform -chdir=infrastructure/environments/<env> state list | grep serving_store
```

If the instance exists and the tenant is reporting a connection timeout (not an authentication
failure), this is almost certainly the network-reachability gap above. Escalate to the platform
lead for a VPN/PrivateLink design decision — don't spend the response-time budget debugging
database credentials for a problem that's actually "there is no network path."

### Step 1: Notification

- [ ] If the root cause is the network-reachability gap: tell the tenant this is a known platform
      limitation being tracked (`docs/KNOWN_GAPS_AND_ROADMAP.md`), not a bug specific to their
      account, and route the timeline question to the platform lead rather than promising a fix
      date
- [ ] If it turns out to be a genuine credential/auth issue instead: document and resolve as a
      normal support ticket, and note in the incident tracker that Step 0's known-gap check was
      ruled out first

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
| Suspected cross-tenant data incident | Security team + Chief Data Officer + affected tenants' account owners | Immediate |
| Tenant BI tool cannot reach serving store | Platform engineering lead | Immediate triage (likely a known network-reachability gap, not an emergency once confirmed) |

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

**Last updated:** 2026-07-14  
**Owner:** Platform Engineering Lead  
**Review cycle:** Monthly (or after major incident) — and again the moment `staging`/`prod` are
actually provisioned, since large parts of this runbook are currently written against a
production environment that does not exist yet

---

## Technology Reference for Incident Response

Quick reference for tools and AWS services used during incident investigation and resolution.

### AWS Console Navigation

| Service | Console path | Key view for incidents |
|---|---|---|
| **Step Functions** | Console → Step Functions → State Machines → `EdlExtractionPipeline` | Execution history; failed executions; input/output per stage |
| **Lambda** | Console → Lambda → Functions → `EdlExtraction*` | Invocation errors; CloudWatch log link; concurrency |
| **CloudWatch Logs** | Console → CloudWatch → Log Groups → `/edl/*` | Structured JSON log events; filter by `run_id` |
| **CloudWatch Alarms** | Console → CloudWatch → Alarms | Active alarms; threshold; recent datapoints |
| **X-Ray** | Console → X-Ray → Traces | Service map; latency; fault trace for specific `run_id` |
| **DynamoDB** | Console → DynamoDB → Tables → `EdlWatermarkRepository` | Current watermark; version; last run ID |
| **DynamoDB** | Console → DynamoDB → Tables → `EdlRunAuditLog` | Stage-by-stage audit record per run |
| **SQS (DLQ)** | Console → SQS → `EdlExtractionFailureDlq` | Message count; message body (contains `run_id`, `source_id`, `entity_id`, `failed_stage`) |
| **S3** | Console → S3 → `edl-schema-snapshots-<PROD_ACCOUNT_ID>` | Latest schema snapshot; drift report |
| **Secrets Manager** | Console → Secrets Manager → `edl/sources/{source}/credentials` | Last rotation date; version status |

### CLI Quick Commands for Incident Investigation

```bash
# Check current watermark for a source/entity.
# The "source_id" key attribute is tenant-scoped ("{tenant_code}#{source_id}",
# e.g. "demo#salesforce" for the default tenant) — not the bare source_id.
# See the note in Scenario 4 above and docs/PIPELINE_FLOW.md's canonical
# isolation table.
aws dynamodb get-item \
  --table-name EdlWatermarkRepository \
  --key '{"source_id":{"S":"demo#salesforce"},"entity_id":{"S":"salesforce-account"}}'

# Read DLQ messages (peek without deleting) — this is an SQS queue, not SNS.
aws sqs receive-message \
  --queue-url https://sqs.us-east-1.amazonaws.com/ACCOUNT/EdlExtractionFailureDlq \
  --max-number-of-messages 10 \
  --visibility-timeout 0

# Get most recent Step Functions execution status
aws stepfunctions list-executions \
  --state-machine-arn arn:aws:states:us-east-1:ACCOUNT:stateMachine:EdlExtractionPipeline \
  --status-filter FAILED \
  --max-results 5

# Check latest schema snapshot
aws s3 cp s3://edl-schema-snapshots-<PROD_ACCOUNT_ID>/salesforce/salesforce-account/latest.json -

# Check CloudWatch alarm state (real alarm_name values from
# infrastructure/modules/observability/main.tf — there are 17 alarms total
# across observability + orchestration modules, not just these three)
aws cloudwatch describe-alarms \
  --alarm-names EdlExtractionFailures EdlSchemaDriftBreakingDetected EdlWatermarkLagSloBreach

# Trigger manual replay
python scripts/trigger_extraction.py \
  --source-id salesforce \
  --entity-id salesforce-account \
  --environment prod \
  --is-replay \
  --replay-of-run-id <run_id_from_DLQ>
```

### Key Log Fields for Filtering

| Field | Meaning | Filter example |
|---|---|---|
| `run_id` | Unique ID for a pipeline run (`run-{YYYYMMDD}-{uuid}`) | `{ $.run_id = "run-20260622-*" }` |
| `source_id` | Source system identifier | `{ $.source_id = "salesforce" }` |
| `entity_id` | Entity being extracted | `{ $.entity_id = "salesforce-account" }` |
| `stage` | Pipeline stage enum | `{ $.stage = "EXTRACTION" }` |
| `status` | Run status enum | `{ $.status = "FAILURE" }` |
| `error_classification` | Failure category | `{ $.error_classification = "TRANSIENT_NETWORK" }` |

### Observability Stack

| Layer | Tool | Location |
|---|---|---|
| Structured logs | structlog → CloudWatch Logs | Log group: `/edl/{service}` |
| Custom metrics | CloudWatch (namespace: `EnterpriseDatalake`) | 6 canonical metrics per run |
| Alarms | CloudWatch Alarms → SNS (`platform_alerts`) → Email/PagerDuty | 17 alarms (`infrastructure/modules/observability/main.tf` + 3 more in `orchestration/main.tf`) — not the "4" figure some older docs still cite |
| Distributed traces | AWS X-Ray | Service map; trace by `run_id` annotation |
| DLQ | SQS `EdlExtractionFailureDlq` | KMS-encrypted; 14-day message retention |

