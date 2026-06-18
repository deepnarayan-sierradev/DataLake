# Enterprise Data Lake Platform — Phase-Wise Implementation Plan

Version: 1.0  
Date: 2026-06-11  
Spec Reference: Enterprise_Data_Lake_Platform_Full_Specification.md v2.0

---

## Overview

This plan organizes delivery into ten sequential phases. Each phase produces shippable, testable increments. No phase begins until all acceptance criteria from the prior phase are met.

**Non-negotiable constraints carried across every phase:**

- Python 3.14.x pinned to tested patch release
- Terraform only (no AWS CDK)
- AWS-first; cloud-neutral module contracts
- Security-by-default, least privilege, OWASP controls built-in from Phase 1
- No prohibited naming: helper, util, common, manager, phase1, phase2
- Secrets only from AWS Secrets Manager; never in code or logs

---

## Phase 1 — Foundation Infrastructure & Repository Skeleton

**Goal:** Provision the base AWS environment and establish the project repository structure that every subsequent phase builds on.

### Deliverables

#### 1.1 Repository Structure
```
enterprise-data-lake/
├── connector_runtime/
│   ├── interfaces/
│   ├── adapters/
│   ├── query_builders/
│   └── tests/
├── orchestration/
│   ├── step_functions/
│   └── event_bridge/
├── schema_management/
│   ├── snapshot_repository/
│   └── drift_evaluation/
├── watermark_management/
├── transformation/
│   ├── field_mapping/
│   └── quality_evaluation/
├── entity_resolution/
│   ├── matching_engine/
│   ├── resolution_config/
│   └── canonical_record_publisher/
├── governance/
├── observability/
├── infrastructure/
│   ├── modules/
│   │   ├── networking/
│   │   ├── storage/
│   │   ├── iam/
│   │   ├── secrets/
│   │   ├── metadata_persistence/
│   │   └── observability/
│   └── environments/
│       ├── dev/
│       ├── staging/
│       └── prod/
├── contracts/
├── ci_cd/
└── docs/
```

#### 1.2 Terraform Modules (AWS)
- **Networking:** VPC, private subnets, VPC endpoints for S3, DynamoDB, Secrets Manager, Glue
- **Storage:** S3 buckets — raw layer, curated layer, analytics layer, schema snapshots, terraform state — all with versioning, SSE-KMS encryption, access logging, and public access blocked
- **IAM:** Service roles for extraction runtime, transformation jobs, entity resolution, CI/CD; all resource-scoped with no wildcard actions
- **Secrets:** AWS Secrets Manager namespaced by `{environment}/{source_id}/credentials`; rotation schedules configured
- **Metadata Persistence:** DynamoDB table for watermark repository; DynamoDB table for run audit log
- **Observability:** CloudWatch log groups, metric namespaces, X-Ray tracing group, SNS alarm topics

#### 1.3 CI/CD Pipeline Scaffold
- Stages: `lint` → `type-check` → `unit-test` → `security-scan` → `dependency-scan` → `iac-policy-scan` → `build` → `deploy-dev` → `promote-staging` → `promote-prod`
- Branch protection: main requires PR approval + all pipeline stages green
- IaC policy scan using `terraform validate` + `checkov` or `tfsec`
- SAST scan integrated (e.g., Bandit for Python)
- Dependency scan (e.g., pip-audit / Safety)
- Immutable, signed release artifacts
- Production promotion requires manual approval gate

#### 1.4 Observability Contract (Baseline)
Define the shared structured log schema enforced across all services:
```json
{
  "run_id": "string",
  "source_id": "string",
  "entity_id": "string",
  "stage": "string",
  "status": "string",
  "duration_ms": "integer",
  "retry_count": "integer",
  "timestamp": "ISO8601"
}
```
No credentials, tokens, or PII fields permitted in any log emission.

### Acceptance Criteria
- [ ] Entire AWS environment (dev) provisioned and destroyed through Terraform with no manual steps
- [ ] Remote Terraform state in S3 with DynamoDB locking and restricted access
- [ ] Repository structure passes naming and structure lint checks
- [ ] CI/CD pipeline runs end-to-end with stub stages
- [ ] IAM roles have no wildcard `*` actions or resources
- [ ] All S3 buckets encrypted at rest and inaccessible from public internet
- [ ] Observability log schema published as a shared contract artifact

---

## Phase 2 — Configuration-Driven Connector Framework Core

**Goal:** Build the metadata-driven connector runtime skeleton, configuration contract, watermark framework, and schema snapshot repository. No source-specific code in this phase.

### Deliverables

#### 2.1 Configuration Contract
Define the versioned entity configuration schema (JSON Schema or Pydantic model):

```yaml
# Example entity configuration record
source_id: salesforce
entity_id: salesforce-account
load_type: incremental          # full | incremental
watermark_field: SystemModstamp
field_mode: all                 # all | standard | custom | includeOnly
include_fields: []
exclude_fields: [IsDeleted]
target_path: s3://raw/salesforce/account/
schema_path: s3://schema-snapshots/salesforce/account/
output_format: parquet
extraction_window_days: 1
active: true
```

- Schema validated before deployment; backward-compatible evolution with semantic versioning
- Configuration repository: DynamoDB or S3-backed with versioned records
- Runtime can read active configuration; deployment automation only can publish changes

#### 2.2 Connector Runtime Interface
Abstract base classes that every source adapter must implement:

```
ConnectorInterface
  ├── discover_queryable_fields(entity_config) -> FieldContract
  ├── build_extraction_query(entity_config, field_contract, watermark) -> QueryContract
  ├── execute_extraction(query_contract) -> ExtractionResultStream
  ├── get_extraction_error_taxonomy() -> ErrorTaxonomy
  └── get_capability_declaration() -> ConnectorCapabilities
```

- Plugin-style registration; connector capabilities declared (supports_bulk, supports_incremental, etc.)
- Shared runtime capabilities injected: retry policy, telemetry emitter, watermark client, schema snapshot client

#### 2.3 Watermark Repository
- DynamoDB-backed with optimistic concurrency (conditional writes)
- Record structure: `source_id`, `entity_id`, `environment`, `last_successful_watermark`, `upper_watermark`, `run_id`, `updated_at`
- Watermark written only after successful extraction + raw persistence + run validation
- Overlap window support (configurable days lookback to mitigate late-arriving updates)
- Replay operation: re-run past extraction window without advancing watermark

#### 2.4 Schema Snapshot Repository
- S3-backed immutable snapshots: `schema-snapshots/{source_id}/{entity_id}/{schema_version}/{extraction_date}.json`
- Each snapshot captures field name, type, precision, scale, length, nullable, queryable flags
- Snapshot written after every successful extraction run

#### 2.5 Schema Drift Evaluator
- Compares current snapshot against previous successful snapshot
- Classifies each change:
  - `non_breaking`: new nullable field added
  - `potentially_breaking`: field precision/scale/length change
  - `breaking`: field removed, type changed, non-nullable field added
- Additive (non-breaking) drift: raw extraction continues; downstream transformation alerted
- Breaking drift: alerts raised; downstream transformation paused pending review
- Drift report written to S3 alongside schema snapshot; never exposes sensitive field values

#### 2.6 Run Lifecycle Manager
- Immutable `run_id` generated at extraction start (UUID + timestamp)
- Canonical pipeline stage contract emitted at each boundary: `run_id`, `source_id`, `entity_id`, `extraction_window`, `schema_version`, `record_count`, `status`
- Dead-letter queue entry created on terminal failure for replay

### Acceptance Criteria
- [ ] Configuration schema validated; invalid configs rejected before runtime starts
- [ ] New entity onboarding completed with configuration record only — no code change
- [ ] Watermark never advances on failed or partially-completed run
- [ ] Schema drift report produced for every run (even when no drift)
- [ ] Additive field additions do not block extraction
- [ ] Breaking drift raises alert and blocks downstream transformation trigger
- [ ] Unit tests cover watermark concurrency, drift classification, and replay scenarios
- [ ] 80%+ coverage on all Phase 2 packages

---

## Phase 3 — Salesforce Connector

**Goal:** Implement the Salesforce source adapter using the connector framework built in Phase 2. No hardcoded field lists, no object-specific extractor classes.

### Deliverables

#### 3.1 Salesforce Authentication Client
- OAuth 2.0 client credentials flow; token retrieved from Secrets Manager at runtime
- Short-lived token caching with proactive refresh before expiry
- Token never logged; scrubbed from all diagnostic output
- API scopes restricted to minimum required objects and actions

#### 3.2 Salesforce Metadata Discovery Client (`SalesforceMetadataDiscoveryClient`)
- Calls Salesforce Describe API to discover all queryable fields for the entity
- Respects field mode configuration: `all`, `standard`, `custom`, `includeOnly`
- Filters out non-queryable and unsupported fields automatically
- Per-run metadata cache with expiration strategy
- New fields added to Salesforce appear in next extraction automatically

#### 3.3 Dynamic SOQL Query Builder (`SalesforceSoqlQueryBuilder`)
- Builds SOQL from discovered field contract + entity configuration
- Incremental: applies `WHERE {watermark_field} >= :lower_bound AND {watermark_field} < :upper_bound`
- Full: no watermark filter; optional partition strategy by date range
- No hardcoded field lists anywhere in the builder

#### 3.4 Bulk API 2.0 Query Job Controller (`SalesforceBulkQueryJobController`)
- Threshold: use Bulk API 2.0 when expected record volume ≥ 2,000
- Job lifecycle: create job → monitor status (polling with exponential backoff + jitter) → fetch results (paginated) → validate record counts → persist raw files → close job
- Enforces timeout handling; job expiration triggers controlled failure with DLQ entry
- Checks Salesforce API limits endpoint before job submission; enforces platform throttling controls
- Fallback behavior defined for throttling, partial results, and job expiration
- Bulk API 2.0 only; no Bulk API 1.0

#### 3.5 Raw Layer Writer (Salesforce)
- Writes immutable Parquet files to S3 raw layer
- Partition scheme: `s3://raw/salesforce/{entity_id}/extraction_date={YYYY-MM-DD}/run_id={run_id}/`
- Extraction metadata written alongside payload: `run_id`, `source_id`, `entity_id`, `extraction_timestamp`, `schema_version`, `record_count`
- Append-only writes; no overwrites of prior raw files

### Testing Requirements
- Unit tests with mocked Salesforce API responses covering: field discovery, SOQL construction, bulk job lifecycle, drift scenarios, retry on transient faults
- Integration tests against Salesforce sandbox
- Contract test: adding new object requires configuration record only, no code change
- Security test: OAuth token not present in any log output

### Acceptance Criteria
- [ ] New Salesforce object added by creating a configuration record — zero code changes
- [ ] Field additions in Salesforce appear in next extraction without code change
- [ ] Bulk API 2.0 handles million-record entities with controlled retries and resumability
- [ ] Raw records match source payload structure with no transformation artifacts
- [ ] OAuth token never appears in logs, traces, or error messages
- [ ] API limit check runs before every bulk job submission

---

## Phase 4 — NetSuite and MySQL RDS Connectors

**Goal:** Implement source adapters for NetSuite and MySQL RDS using the same connector framework contract.

### Deliverables

#### 4.1 NetSuite Connector
- **`NetSuiteMetadataAdapter`:** Discovers available record types and fields via SuiteAnalytics or REST API
- **`NetSuiteIncrementalQueryPlanner`:** Builds queries using NetSuite's search or query API with watermark window
- Credentials retrieved from Secrets Manager; token-based authentication (TBA OAuth 2.0)
- Raw output to S3: `s3://raw/netsuite/{entity_id}/extraction_date={YYYY-MM-DD}/run_id={run_id}/`

#### 4.2 MySQL RDS Connector
- **`MySqlIncrementalExtractor`:** JDBC/Python connector; builds parameterized queries with watermark field range
- **`MySqlSchemaIntrospectionClient`:** Reads `information_schema` to discover columns and types
- Parameterized queries only — no string interpolation of user-controlled values (injection prevention)
- Private connectivity via VPC; no public RDS endpoint exposure
- Credentials from Secrets Manager with rotation
- Raw output to S3: `s3://raw/mysql_rds/{entity_id}/extraction_date={YYYY-MM-DD}/run_id={run_id}/`

#### 4.3 Connector Certification Checklist
For each new connector before production onboarding:
- [ ] Connector implements full `ConnectorInterface` contract
- [ ] Credentials retrieved from Secrets Manager only
- [ ] No sensitive values in logs
- [ ] Schema discovery dynamic (no hardcoded field lists)
- [ ] Incremental extraction with watermark support
- [ ] Retry policy configured (transient vs deterministic fault distinction)
- [ ] Unit and integration tests pass
- [ ] Security review completed

### Acceptance Criteria
- [ ] NetSuite and MySQL RDS data lands in raw layer with correct partition structure
- [ ] Each connector passes the connector certification checklist
- [ ] No SQL injection vectors in MySQL query construction
- [ ] Private VPC connectivity for MySQL RDS confirmed
- [ ] All three connectors (Salesforce, NetSuite, MySQL) run through the same orchestration framework

---

## Phase 5 — Orchestration, Pipeline Lifecycle & Reliability

**Goal:** Wire the full extraction pipeline using AWS Step Functions and EventBridge. Implement fault tolerance, retry framework, and DLQ replay.

### Deliverables

#### 5.1 Extraction Orchestration Workflow (Step Functions)
Pipeline stages as state machine steps:
```
Load Entity Configuration
  → Retrieve Source Credentials
  → Start Connector Extraction Run
  → Persist Schema Snapshot
  → Evaluate Schema Drift
  → Validate Raw Output Record Count
  → Update Watermark (success only)
  → Trigger Transformation Pipeline
  → Emit Run Completion Event
```
- Each step emits structured status event to CloudWatch and audit DynamoDB table
- Failure at any step routes to DLQ with full context for replay
- Step Functions state is externalized; no in-memory state assumptions

#### 5.2 EventBridge Scheduler
- Schedules extraction workflows per entity with configurable cron expressions
- Source-specific and entity-specific scheduling supported via configuration
- Event-driven trigger support alongside schedule-based triggers

#### 5.3 Reliability Framework (`ExtractionRetryPolicy`, `RunReplayController`)
- Exponential backoff with jitter for transient faults (timeouts, throttling, temporary network failures)
- Fail-fast (no retry) for deterministic failures: invalid credentials, invalid object names, schema-validation failures, invalid configuration
- Circuit-breaker: configurable failure threshold per source before pausing extraction
- Retry count tracked per stage and emitted as metric
- `RunReplayController`: re-runs a specific `run_id` window safely; idempotent; does not duplicate curated records or regress watermarks
- Availability target: 99.9% uptime for extraction control plane

#### 5.4 Dead-Letter Queue & Failure Queue
- Failed runs enqueued with: `run_id`, `source_id`, `entity_id`, `failed_stage`, `error_classification`, `error_message`, `timestamp`
- Replay operation consumes DLQ entry and re-triggers Step Functions with original parameters
- Audit trail preserved for all retries and recovery actions

### Acceptance Criteria
- [ ] End-to-end extraction run for all three sources completes through Step Functions
- [ ] Watermark is not updated when any pipeline step fails
- [ ] Replay of a failed run does not produce duplicate raw files or incorrect watermarks
- [ ] DLQ entries created for all terminal failures with sufficient context to replay
- [ ] Retry metrics visible in CloudWatch per source and entity
- [ ] 99.9% control plane availability demonstrated in staging load test

---

## Phase 6 — Curated Layer Transformation & Data Quality

**Goal:** Implement the transformation pipeline that reads from raw, applies business rules, maps fields to canonical domain models, validates quality, and publishes curated datasets.

### Deliverables

#### 6.1 Transformation Pipeline (AWS Glue or Spark)
- Triggered by Step Functions after successful raw extraction and watermark update
- Reads raw Parquet files from S3; never modifies raw data
- Applies transformation jobs per source and entity
- Transformation logic versioned; canonical models in `contracts/` package

#### 6.2 Cross-System Field Mapping Registry
- Declarative mapping rules: source field → canonical field → target type/format
- Handles cross-system shape differences (e.g., Salesforce `FirstName`+`LastName` → canonical `full_name`)
- Mapping registry versioned; backward-compatible evolution
- New mappings added without pipeline code changes

#### 6.3 Data Quality Evaluation
- Quality policies defined per entity: null checks, range checks, referential integrity, pattern matching
- Quality report written to S3 alongside curated dataset
- Policy violation classification: `warning` (continues), `blocking` (pauses publication pending review)
- Quality metrics emitted to CloudWatch

#### 6.4 Curated Layer Writer
- Output: S3 curated layer in Parquet (columnar, efficient for analytics queries)
- Partition scheme: `s3://curated/{domain}/{entity_id}/curated_date={YYYY-MM-DD}/`
- Dataset named by domain and purpose: e.g., `customer_profile_curated`
- Masking/tokenization applied for sensitive attributes per data classification policy
- Curated consumers receive read-only access; transformation jobs write-only to curated prefixes
- Schema contract published to AWS Glue Data Catalog after each successful publication

### Acceptance Criteria
- [ ] Curated outputs satisfy mapping, quality, and lineage requirements
- [ ] Raw data is never modified by transformation pipeline
- [ ] Data quality blocking violations halt publication; warnings are logged and monitored
- [ ] Curated datasets registered in Glue Data Catalog with schema and lineage metadata
- [ ] Sensitive attribute masking applied per classification policy
- [ ] New entity transformation added without modifying unrelated transformation modules

---

## Phase 7 — Entity Resolution & Golden Record Publishing

**Goal:** Implement deterministic and probabilistic entity matching across sources, survivorship rules, and golden record publication to the analytics layer.

### Deliverables

#### 7.1 Matching Engine (`MatchRuleEngine`)
- Configured match rule sets per entity type (e.g., company, person)
- Deterministic matching: exact match on email, phone, or domain identifiers
- Probabilistic matching: scoring on name, address, normalized identifiers with configurable `match_threshold` and per-field `weight`
- **Match rules fully externally configurable via JSON** (no hardcoded thresholds, field names, or source names in Python)
- Blocking strategies reduce O(n²) pairwise comparisons to O(b·k²) before matching runs
- Explainability record written for every match decision: rule applied, score, matched fields, confidence level

#### 7.2 Survivorship Policy (`GoldenRecordSurvivorshipPolicy`)
- Defines which source "wins" per attribute when records conflict
- Policies configurable per entity type and attribute via JSON — no hardcoded source names or field strategies in Python
- `output_fields` list in each policy declares the **explicit output schema** of canonical records — only declared fields appear in the Parquet output; internal source IDs and duplicate name variants are excluded
- Conflict resolution tracked and auditable per field

#### 7.3 Golden Record Publisher
- Generates mastered entity records from matched curated records
- Golden record includes: `golden_id`, `contributing_source_records`, `survivorship_version`, `match_run_id`, and only the fields in the survivorship policy `output_fields`
- Published to analytics layer S3: `s3://analytics/canonical/{entity_type}/`
- **Production entry point: `GoldenRecordPublisher.from_registry(registry, entity_type, ...)`** — loads config from `ResolutionConfigRegistry`; no rule set or policy constructed inline in Lambda handler code
- Reproducible and traceable to source records and rule versions
- PII-safe diagnostics: match statistics logged without exposing PII values in observability

#### 7.4 `ResolutionConfigRegistry` (S3-backed config loader)
- Loads versioned `MatchRuleSet` and `SurvivorshipPolicy` from S3 at runtime
- Config S3 paths: `entity-resolution/{entity_type}/match_rules_{version}.json`, `survivorship_{version}.json`, `latest.json`
- Source JSON files live in `config/entity_resolution/{entity_type}/` (Git)
- Published via `seed_entity_resolution_configs.py` (analogous to field mapping seeding)
- In-process cache per warm Lambda invocation; cache invalidated on `publish()`
- Two entity types defined at platform launch: `company` (Salesforce Account + NetSuite Customer) and `person` (Salesforce Contact)
- Adding a new entity type or updating match thresholds requires only a JSON config change and S3 publish — no Python code change

#### 7.5 Non-Mastered Analytics Dataset Publication
- Curated domain datasets published separately to analytics layer alongside golden records
- Analytics layer contains both: curated domain datasets and golden record datasets
- No conflation of golden records with all downstream analytical datasets

### Acceptance Criteria
- [ ] Golden record generation is reproducible given same input data and rule versions
- [ ] Every match decision has an explainability record traceable to rule version and source records
- [ ] Golden records and non-mastered curated datasets published as distinct datasets in analytics layer
- [ ] PII values absent from match statistics logs and CloudWatch metrics
- [ ] Precision and recall evaluation workflow defined and documented
- [ ] Match rules and survivorship policies are loaded from S3 config at runtime — no field names, thresholds, or source priorities hardcoded in Python
- [ ] Adding a new entity type requires only new JSON config files + S3 publish — zero Python code change
- [ ] Canonical output schema controlled exclusively by `output_fields` in survivorship JSON — source-internal IDs and duplicate name columns absent from Parquet output
- [ ] `ResolutionConfigRegistry` tested with 100% branch coverage including error paths and cache invalidation
- [ ] Entity resolution config JSON files committed to `config/entity_resolution/` and seeded via script (same pattern as field mappings)

---

## Phase 8 — Analytics Layer & Target Serving Store

**Goal:** Publish consumption-optimized analytics datasets and load the serving database for BI, API, and application consumers.

### Deliverables

#### 8.1 Analytics Layer Publisher
- Consumes curated datasets and golden records
- Optimizes output for query performance: partitioning, columnar format (Parquet/ORC), statistics
- Registers datasets in Glue Data Catalog with lineage from curated and golden record sources
- Analytics consumers (BI, AI/ML) receive read-only access to analytics layer prefixes

#### 8.2 Target Serving Database Load
- Loads curated and/or analytics outputs into target serving store (RDS, Redshift, or equivalent)
- Table creation driven by published schema contracts; not hardcoded DDL
- Idempotent load: upsert or replace strategies defined per entity
- Load metrics: records loaded, load duration, error counts emitted to CloudWatch

#### 8.3 BI and AI/ML Consumption Paths
- Athena or equivalent query interface over analytics S3 layer
- Feature store integration path documented for AI/ML consumers
- Access controls: read-only role per consumer domain

### Acceptance Criteria
- [ ] Analytics datasets queryable by BI tool within defined SLO after pipeline completion
- [ ] Target database schema created and updated from contract without manual DDL
- [ ] Idempotent load confirmed: re-running load does not duplicate records
- [ ] AI/ML feature consumer path documented with example access pattern

---

## Phase 9 — Governance, Security Hardening & OWASP Compliance

**Goal:** Complete governance controls, validate security posture against OWASP Top 10, finalize data catalog, lineage, and policy enforcement.

### Deliverables

#### 9.1 Data Governance Service
- Every production dataset registered with: owner (data steward), lineage path, data classification, retention policy, privacy classification
- Automated lineage capture at extraction, transformation, and publication boundaries
- Governance catalog integrated with ingestion runtime and curated publication pipeline
- Governance metadata versioned and auditable

#### 9.2 OWASP Top 10 Control Evidence
| Control | Implementation |
|---|---|
| A01 Broken Access Control | IAM least-privilege policies; resource-scoped roles per service and environment; no wildcards |
| A02 Security Misconfiguration | Terraform + Checkov/tfsec IaC scanning; hardened container images; no default credentials |
| A03 Software Supply Chain | pip-audit / Safety in CI; pinned dependency versions; signed release artifacts |
| A04 Cryptographic Failures | SSE-KMS on all S3/DynamoDB; TLS mandatory on all network paths; no deprecated cipher suites |
| A05 Injection | Parameterized queries in MySQL extractor; SOQL built from discovered fields, not user input |
| A06 Insecure Design | Threat model per connector and stage; security reviewed at design phase |
| A07 Authentication Failures | OAuth 2.0 with short-lived tokens; Secrets Manager rotation; workload identity for service-to-service |
| A08 Software/Data Integrity | Signed artifacts; Terraform state integrity; immutable raw layer |
| A09 Security Logging/Alerting Failures | Structured logging with CloudWatch; scrubbed credentials/PII; SIEM integration; alert SLOs defined |
| A10 Exceptional Conditions | Failure taxonomy (transient vs deterministic); DLQ for terminal failures; no silent swallowing of errors |

#### 9.3 Security Hardening
- Container image scanning in CI (if containers used)
- Quarterly access review process defined; automated IAM Access Analyzer findings reviewed
- Penetration test and configuration review completed before production launch
- Secrets rotation schedule active for all registered credentials
- KMS key rotation enabled

#### 9.4 Retention, Privacy & Legal Hold
- Retention policies defined per data classification and source
- Legal hold capability on raw layer S3 objects
- PII data classification applied to every source entity before extraction activation

### Acceptance Criteria
- [ ] Every production dataset has registered owner, lineage path, classification, and retention policy
- [ ] CI pipeline includes all OWASP-aligned controls with pass/fail gates
- [ ] Penetration and configuration review show no critical open findings
- [ ] Secrets Manager rotation active for all source credentials
- [ ] IAM Access Analyzer shows no unintended public or cross-account access
- [ ] Security architecture review board sign-off received

---

## Phase 10 — Future Connector Onboarding Framework & Operationalization

**Goal:** Solidify the connector SDK as a reusable platform capability, operationalize runbooks, SLO dashboards, and prepare for new source onboarding (Dynamics 365, HubSpot, SAP, PostgreSQL, REST APIs, CSV/SFTP).

### Deliverables

#### 10.1 Connector SDK Documentation & Certification
- Connector SDK guide: implement `ConnectorInterface`, register capability declaration, configure entity records
- Connector certification checklist published as mandatory onboarding artifact
- New connector integration: configuration + adapter implementation only; no core framework changes
- Validated against at least one new planned connector (e.g., PostgreSQL or REST API)

#### 10.2 Source Onboarding Process
Standardized onboarding gate sequence:
1. Source registration (source_id, owner, SLA, data classification)
2. Credential registration (Secrets Manager entry + rotation schedule)
3. Entity mapping (configuration records for each entity)
4. Extraction profile validation (dry run in dev; schema snapshot captured)
5. Security and governance sign-off
6. Acceptance validation (canary activation; record count and quality checks)

#### 10.3 SLO Dashboards & Operational Runbooks
- CloudWatch dashboards per source: extraction duration, records extracted, schema drift count, watermark lag, retry count, failure rate
- Alert thresholds bound to SLOs per source and entity criticality
- Runbooks for:
  - Ingestion failure (including escalation path)
  - Schema drift alert (breaking vs non-breaking response)
  - Replay operation (step-by-step)
  - Watermark rollback
  - Connector credential rotation
  - Production deployment freeze and rollback

#### 10.4 Architecture Health & Standards Review Process
- Quarterly architecture health assessment: principle compliance, platform risk, cost trends
- Quarterly standards validation: Python version, Salesforce API, OWASP Top 10, Terraform, AWS IAM guidance
- Cost-per-million-records tracking and latency P50/P90/P99 by stage

#### 10.5 Naming Guide & Developer Onboarding
- Naming dictionary published as mandatory onboarding artifact
- Pull request template with compliance checklist: naming, security, tests, architecture alignment
- Prohibited generic naming check automated in CI (lint rule)

### Acceptance Criteria
- [ ] New connector integrated by implementing `ConnectorInterface` + configuration; zero core runtime refactoring
- [ ] Source onboarding checklist includes security, governance, and operational sign-off gates
- [ ] SLO dashboards live in CloudWatch for all three production sources
- [ ] All critical alert runbooks have named owner and escalation path
- [ ] Quarterly standards validation checklist completed with dated evidence
- [ ] Pull requests with prohibited generic naming fail CI lint check

---

## Dependency & Sequencing Summary

```
Phase 1 (Infrastructure & Repo) 
  └─> Phase 2 (Connector Framework Core)
        ├─> Phase 3 (Salesforce Connector)
        ├─> Phase 4 (NetSuite & MySQL RDS Connectors)
        └─> Phase 5 (Orchestration & Reliability)
              ├─> Phase 6 (Curated Transformation)
              │     └─> Phase 7 (Entity Resolution & Golden Records)
              │           └─> Phase 8 (Analytics Layer & Serving)
              └─> Phase 9 (Governance, Security, OWASP) [can run in parallel with 6-8]
                    └─> Phase 10 (Connector SDK & Operationalization)
```

Phases 3 and 4 can proceed in parallel after Phase 2 is complete.  
Phase 9 security and governance work can run in parallel with Phases 6–8 transformation work.

---

## Cross-Phase Standards (Active from Phase 1 Onward)

| Standard | Enforcement |
|---|---|
| Python 3.14.x pinned | CI build environment and `pyproject.toml` |
| Terraform only for IaC | Architecture review gate; no CDK code merged |
| No wildcard IAM | Checkov policy check in CI |
| Secrets from Secrets Manager only | SAST + code review check |
| No credentials or PII in logs | Unit test for log redaction per service |
| Naming standards enforced | CI lint rule; PR template checklist |
| 80%+ test coverage on critical packages | CI coverage gate |
| All APIs authenticated | Architecture review and security test |

END OF IMPLEMENTATION PLAN
