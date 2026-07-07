# Enterprise Data Lake — Improvement Plan

**Prepared:** 2026-07-06  
**Scope:** Non-breaking improvements across all five quality dimensions  
**Principle:** Every change must be backward-compatible — existing running pipelines
(dev Salesforce, MySQL, Sage extractions) continue to work without modification.

---

## How to Read This Document

Each section maps to one quality dimension. Within each section, work items are
ordered by priority: **P0** (blocking SaaS launch), **P1** (required before
scale to millions of records), **P2** (operational excellence / GA readiness).

Each work item contains:
- Problem statement (what is wrong and why)
- Proposed solution (concrete design, no ambiguity)
- Affected files (exact file paths to change)
- Backward-compatibility guarantee
- Acceptance criteria

---

## 1. Scalable and Maintainable Architecture (SaaS)

### 1.1 [P0] Multi-Tenancy Data Model

**Problem**  
There is no tenant dimension anywhere in the platform. Every tenant's data
would share the same S3 prefixes, DynamoDB partition keys, Secrets Manager
paths, and Step Functions executions. Tenant A can see Tenant B's curated data
by guessing a prefix; there is no access boundary.

**Proposed Solution**

*Tenant code format:*  
Each tenant is identified by a `tenant_code`: a stable, human-readable slug
chosen at onboarding time (e.g., `acme-corp`, `globex-eu`, `initech`). Because
it is a slug and not an opaque UUID, an operator navigating the AWS S3 console
can immediately identify which tenant's data they are looking at without
cross-referencing a lookup table.

Validation pattern: `TENANT_CODE_PATTERN = r"^[a-z][a-z0-9\-]{1,47}$"`  
(lowercase letters, digits, hyphens; 2–48 characters; starts with a letter)

*Data plane isolation via `tenant_code` prefix:*

```
# S3 raw layer (today — single-tenant)
raw/{source_id}/{entity_id}/extraction_date={date}/run_id={run_id}/

# S3 raw layer (with multi-tenancy — tenant_code is the root directory)
{tenant_code}/raw/{source_id}/{entity_id}/extraction_date={date}/run_id={run_id}/

# Concrete example for tenant "acme-corp"
acme-corp/raw/salesforce/salesforce-account/extraction_date=2026-07-06/run_id=run-.../
```

Each tenant owns their root directory directly — browsing the S3 console shows
one folder per tenant with no shared parent. Apply the same scheme to every layer:
```
{tenant_code}/raw/{source_id}/{entity_id}/...
{tenant_code}/curated/{domain}/{entity_id}/...
{tenant_code}/canonical/{entity_type}/...
{tenant_code}/analytics/{entity_type}/...
{tenant_code}/lineage/{entity_id}/...
{tenant_code}/quality-reports/{source_id}/{entity_id}/...
```

*DynamoDB key extension:*  
All three tables (`entity-extraction-config`, `watermark-repository`,
`run-audit-log`) use `tenant_code` as a composite key prefix. New GSI on each
table: `GSI: tenant_code (PK) + entity_id (SK)` for tenant-scoped queries.

*Secrets Manager path extension:*
```
# Today:  {env}/sources/{source_id}/credentials
# SaaS:   {env}/{tenant_code}/sources/{source_id}/credentials

# Concrete example
dev/acme-corp/sources/salesforce/credentials
```

*IAM path-scoped policies (per-tenant role or resource tag):*  
Extraction runtime role gets a `Condition` restricting `s3:*` to the
`{tenant_code}/*` prefix (e.g., `acme-corp/*`). Use `aws:RequestTag/TenantCode`
or resource-based S3 bucket prefix policies.

*Step Functions execution name namespace:*  
Prefix all execution names with `{tenant_code}-` so CloudWatch Logs and execution
history are filterable per tenant (e.g., `acme-corp-salesforce-account-run-...`).

*EventBridge schedule name namespace:*  
All schedules are prefixed `{tenant_code}-{source_id}-{entity_id}`.

**Event schema change:**  
Add `tenant_code` as a required field to all Step Functions inputs.
Handler validation uses the new `TENANT_CODE_PATTERN` regex defined in
`contracts/identifier_policy.py`.

**Config change:**  
`EntityExtractionConfig` gets a `tenant_code: str` field (validated, required).
`target_raw_s3_prefix`, `schema_snapshot_s3_prefix` become derived, not stored —
computed from `tenant_code + source_id + entity_id` at runtime to prevent
injection.

**Backward compatibility:**  
Add `tenant_code` as optional with default `"default"` in all contracts initially.
A feature flag `MULTI_TENANT_MODE=true` in Lambda env vars activates the new
path scheme. Existing `dev` pipelines continue to work with `tenant_code=default`
under the existing prefix structure.

**Affected files:**
```
contracts/entity_configuration_contract.py          — add tenant_code field
contracts/identifier_policy.py                      — TENANT_CODE_PATTERN constant
connector_runtime/extraction_pipeline_handler.py    — require tenant_code in event
transformation/transformation_pipeline_handler.py   — require tenant_code
entity_resolution/entity_resolution_pipeline_handler.py
analytics_publisher/analytics_publisher_handler.py
transformation/transformation_pipeline.py           — TransformationContext + tenant_code
transformation/curated_utils.py                     — tenant_code in prefix helpers
watermark_management/watermark_repository/watermark_repository.py
connector_runtime/run_lifecycle/run_lifecycle.py
governance/lineage_record.py
infrastructure/modules/iam/main.tf                  — S3 path-scoped conditions
infrastructure/modules/metadata_persistence/main.tf — DynamoDB GSI additions
infrastructure/modules/orchestration/main.tf        — execution name prefix
scripts/seed_entity_config.py                       — tenant_code argument
scripts/trigger_extraction.py                       — tenant_code argument
```

**Acceptance criteria:**
- Each tenant owns a top-level root directory in S3 (e.g., `acme-corp/` and
  `globex-eu/` are sibling root folders — no shared `tenants/` parent)
- An IAM role for Tenant A cannot `s3:GetObject` on Tenant B's root prefix
- All existing dev pipeline runs still succeed with `tenant_code=default`
  (data lives under `default/raw/...`, `default/curated/...`, etc.)

---

### 1.2 [P0] SaaS Control Plane API

**Problem**  
There is no API for tenant onboarding, entity config management, pipeline
triggering, or run status queries. Everything is done via manual CLI scripts.

**Proposed Solution**

Introduce an API Gateway + Lambda control-plane layer as a new
`connector_runtime/api/` package. This is a separate deployment unit from the
data pipelines.

*API surface (REST over API Gateway):*

| Method | Path | Description |
|--------|------|-------------|
| POST | /tenants | Provision new tenant + seed default configs |
| GET | /tenants/{tenant_code}/entities | List configured entities |
| POST | /tenants/{tenant_code}/entities | Register new entity |
| POST | /tenants/{tenant_code}/pipelines/trigger | Trigger pipeline run |
| GET | /tenants/{tenant_code}/runs/{run_id} | Get run status |
| GET | /tenants/{tenant_code}/runs | List recent runs |

*Security:*  
- API Gateway with Cognito User Pool authorizer (per-tenant JWT scope)
- WAF with AWS Core Rule Set + Known Bad Inputs rule group + rate limiting
  (100 req/min per tenant IP, 1000 req/min per tenant JWT)
- All inputs validated with Pydantic before use in any AWS API call
- Response never includes raw data values — only metadata and S3 paths

*New Terraform module:* `infrastructure/modules/control_plane/`
- API Gateway REST API + WAF ACL
- Cognito User Pool + App Client
- Control-plane Lambda function
- API Lambda IAM role: `{env}-control-plane-runtime-role`
  - `dynamodb:PutItem/GetItem/Query` on entity-config table (scoped to `tenant_code` key prefix)
  - `states:StartExecution` on extraction pipeline state machine
  - `cloudwatch:GetMetricData` for run status queries
  - No direct S3 access (data access is data-plane only)

**Affected files:**
```
connector_runtime/api/                           — new package
connector_runtime/api/control_plane_handler.py
connector_runtime/api/tenant_provisioner.py
connector_runtime/api/run_status_client.py
infrastructure/modules/control_plane/            — new Terraform module
infrastructure/modules/control_plane/main.tf
infrastructure/modules/control_plane/variables.tf
infrastructure/modules/control_plane/outputs.tf
infrastructure/modules/waf/                      — new Terraform module
infrastructure/environments/dev/main.tf          — wire control_plane module
```

**Backward compatibility:**  
Additive — existing pipeline triggers via `scripts/trigger_extraction.py` still
work unchanged. The control plane is a new surface, not a replacement.

---

### 1.3 [P0] Config-Driven Entity Type Registry

**Problem**  
`entity_resolution/entity_type_registry.py` has `ENTITY_ID_TO_TYPE`,
`ENTITY_TYPE_PK_FIELD`, and `ENTITY_TYPE_SOURCES` as hardcoded Python dicts.
Adding a new entity type requires a code change and Lambda redeploy, which is
not SaaS-compatible when tenants may define custom entity types.

**Proposed Solution**

Replace hardcoded dicts with a DynamoDB-backed `EntityTypeRegistry` class that
mirrors the `ConfigurationRepositoryClient` pattern:

```python
class EntityTypeRegistryClient:
    """DynamoDB-backed entity type registry."""

    def get_entity_type(self, entity_id: str, tenant_code: str) -> str | None: ...
    def get_pk_field(self, entity_type: str) -> str | None: ...
    def get_contributing_sources(
        self, entity_type: str, tenant_code: str
    ) -> list[tuple[str, str]]: ...
    def register_entity_type(self, record: EntityTypeRecord) -> None: ...
```

*DynamoDB table:* `{env}-entity-type-registry`
- PK: `tenant_code`, SK: `entity_id`
- Attributes: `entity_type`, `pk_field`, `contributing_sources` (JSON list)

The hardcoded dicts in `entity_type_registry.py` become the **seed data** for
the `default` tenant (`tenant_code=default`), migrated once via
`scripts/seed_entity_type_registry.py`.

*Backward compatibility:* The existing constants remain as a fallback for the
`default` tenant if the DynamoDB record is absent (graceful degradation).

**Affected files:**
```
entity_resolution/entity_type_registry.py              — add registry client class
entity_resolution/entity_resolution_pipeline_handler.py — inject registry client
analytics_publisher/analytics_publisher_handler.py      — inject registry client
scripts/seed_entity_type_registry.py                    — new seed script
infrastructure/modules/metadata_persistence/main.tf     — new DynamoDB table
infrastructure/modules/iam/main.tf                      — ER/analytics role access
```

---

### 1.4 [P1] Config-Driven Survivorship Policy

**Problem**  
`entity_resolution/survivorship_policy.py` hardcodes field preference rules
per source. For SaaS, different tenants may have different data quality
expectations for the same entity type.

**Proposed Solution**

Survivorship rules become S3-backed JSON config (same pattern as field
mappings). A `SurvivorshipPolicyRegistryClient` loads them per
`(tenant_code, entity_type, version)`:

```
s3://{curated-bucket}/{tenant_code}/survivorship-policies/{entity_type}/v{n}.json

# Concrete example
s3://dev-edl-curated-layer/acme-corp/survivorship-policies/company/v1.json
```

Schema of `v1.json`:
```json
{
  "entity_type": "company",
  "policy_version": "v1",
  "field_rules": [
    {
      "field": "annual_revenue",
      "preferred_sources": ["salesforce", "netsuite"],
      "tiebreak": "most_recent"
    }
  ],
  "default_rule": { "tiebreak": "most_records" }
}
```

The existing hardcoded policy becomes the seed for `tenant_code=default`.

**Affected files:**
```
entity_resolution/survivorship_policy.py              — add registry client
entity_resolution/entity_resolution_pipeline_handler.py
config/survivorship_policies/                         — seed JSON files
scripts/seed_survivorship_policies.py                 — new seed script
```

---

### 1.5 [P1] Automated Environment Provisioning (DynamoDB Tables)

**Problem**  
Three DynamoDB tables are pre-existing and manually created. New environments
and new tenant environments cannot be provisioned automatically.

**Proposed Solution**

Migrate DynamoDB table management into Terraform using `resource` blocks with
`lifecycle { prevent_destroy = true }`. The existing tables are imported into
state (`terraform import`) rather than recreated:

```bash
terraform import \
  module.metadata_persistence.aws_dynamodb_table.entity_extraction_config \
  dev-entity-extraction-config
```

Add a `scripts/bootstrap_environment.sh` that runs table creation + terraform
apply in the correct order for brand-new environments where import is not
applicable.

**Affected files:**
```
infrastructure/modules/metadata_persistence/main.tf  — change data → resource
infrastructure/modules/metadata_persistence/main.tf  — add lifecycle.prevent_destroy
scripts/bootstrap_environment.sh                     — new bootstrap script
docs/DEPLOYMENT_GUIDE.md + .html                     — update provisioning steps
```

---

### 1.6 [P0] SQS Burst Buffer Between EventBridge Scheduler and Step Functions

**Problem**

EventBridge Scheduler fires directly into Step Functions with no intermediary
queue. The platform is being prepared for customer launch with 80–100 entities
across all sources (Salesforce, NetSuite, MySQL, Sage). With schedules aligned
to the same hour, all 80–100 Step Functions executions start simultaneously,
each immediately invoking the extraction Lambda. The default account Lambda
concurrency limit is 1,000 — a soft limit shared across all four pipeline
Lambdas and any other account functions — and the burst would consume a large
fraction of it in the first second.

This is being addressed now, before customer launch, while only 5 dev entities
exist and the change is low-risk. Deferring to a later phase with live customer
traffic would require coordinating a zero-downtime schedule migration across all
active entities.

Compounding this, the current Step Functions retry blocks do not include
`Lambda.TooManyRequestsException`. A throttled invocation therefore falls
through to the `Catch → States.ALL` path and is DLQ'd as a hard failure rather
than retried as a transient condition.

**Proposed Solution**

Introduce an SQS FIFO queue as a burst-absorbing buffer between EventBridge
Scheduler and Step Functions. EventBridge continues to fire on the same cron
schedules but writes a trigger message to SQS instead of starting the state
machine directly. A lightweight `pipeline_trigger` Lambda consumes the queue
with controlled concurrency, starts one Step Functions execution per message,
and deletes the message on success.

```
EventBridge Scheduler (N schedules fire simultaneously)
        │
        ▼
SQS FIFO Queue  ─── absorbs all N messages instantly; no loss
  ({env}-edl-pipeline-trigger.fifo)
        │  (drains at controlled rate via Lambda ESM)
        ▼
Pipeline Trigger Lambda  ─── reserved_concurrency=50 caps execution start rate
  (SQS Event Source Mapping, batch_size=1)
        │
        ▼
Step Functions execution starts → extraction → transformation → ...
```

*Why SQS FIFO over Standard:*
- Exactly-once processing prevents duplicate executions for the same entity
  at the same schedule tick.
- Message group ID = `{source_id}-{entity_id}` ensures per-entity ordering —
  one active execution per entity at a time.

*Queue configuration:*

| Parameter | Value | Rationale |
|---|---|---|
| `VisibilityTimeout` | `900s` | Matches Lambda max timeout — message reappears if trigger Lambda crashes before `StartExecution` succeeds |
| `MessageRetentionPeriod` | `86400s` | A missed schedule is retried within 24h |
| `ContentBasedDeduplication` | `true` | Duplicate schedule fires within the 5-minute dedup window are dropped automatically |
| `DeduplicationScope` | `messageGroup` | Per-entity dedup, not queue-wide |

*Pipeline Trigger Lambda (`orchestration/pipeline_trigger/`):*
- `reserved_concurrent_executions = 50` — starts at most 50 Step Functions
  executions per flush, preventing the Lambda concurrency spike from a burst
  of simultaneous EventBridge fires.
- Validates message body with Pydantic before calling `states:StartExecution`.
- Idempotent: uses the `name` parameter on `StartExecution` set to
  `{entity_id}-{schedule_tick_iso}`. Re-triggering the same schedule tick is
  a no-op (Step Functions rejects duplicate execution names with a
  `ExecutionAlreadyExists` error, which the trigger Lambda treats as success).

*Complementary fix — add `Lambda.TooManyRequestsException` to all Step Functions
retry blocks:*

```hcl
Retry = [
  {
    ErrorEquals = [
      "Lambda.ServiceException",
      "Lambda.AWSLambdaException",
      "Lambda.SdkClientException",
      "Lambda.TooManyRequestsException",  # NEW — throttle is transient, not fatal
      "TransientExtractionError"
    ]
    IntervalSeconds = 30      # longer base interval for throttle recovery
    MaxAttempts     = 5       # more attempts vs current 3
    BackoffRate     = 2.0
    JitterStrategy  = "FULL"  # full jitter prevents thundering-herd on retry burst
  }
]
```

*Reserved concurrency per Lambda function (starvation prevention, not a
throughput cap):*

| Lambda | `reserved_concurrent_executions` | Rationale |
|---|---|---|
| `extraction-pipeline` | 400 | Largest consumer; network I/O bound |
| `transformation-pipeline` | 300 | CPU bound; runs after extraction completes |
| `entity-resolution-pipeline` | 200 | Lower parallelism; reads curated layer |
| `analytics-layer-publisher` | 100 | Low frequency; final stage only |
| Unreserved pool | ~100 | Headroom for other account functions |

With the SQS buffer in place, the trigger Lambda feeds the extraction Lambda
at a rate it can absorb (≤50 new executions starting per minute). The reserved
ceiling is rarely reached in practice — it exists solely to prevent one function
from starving the others if a large burst does hit.

**Backward compatibility:**  
EventBridge schedules continue to fire on the same cron expressions. The only
change is the target resource: from `arn:aws:states:…` (direct Step Functions)
to `arn:aws:sqs:…` (FIFO queue). The Step Functions state machine definition,
all Lambda handlers, and entity configs are unchanged. Pipelines triggered
manually via `scripts/trigger_extraction.py` call `states:StartExecution`
directly and bypass the queue entirely.

**Affected files:**
```
orchestration/pipeline_trigger/                               — new package
orchestration/pipeline_trigger/pipeline_trigger_handler.py    — new Lambda handler
infrastructure/modules/orchestration/main.tf                  — SQS FIFO queue + ESM + trigger Lambda
infrastructure/modules/orchestration/main.tf                  — add Lambda.TooManyRequestsException to all Retry blocks
infrastructure/modules/orchestration/variables.tf             — trigger_lambda_reserved_concurrency variable
infrastructure/modules/lambda_pipeline/variables.tf           — reserved_concurrency default stays -1; set per env
infrastructure/environments/dev/main.tf                       — reserved_concurrent_executions per Lambda
infrastructure/environments/staging/main.tf
infrastructure/environments/prod/main.tf
infrastructure/modules/iam/main.tf                            — trigger Lambda IAM role:
                                                              —   sqs:ReceiveMessage/DeleteMessage/GetQueueAttributes
                                                              —   states:StartExecution (scoped to pipeline state machine)
```

**Acceptance criteria:**
- 500 EventBridge schedules firing within one second produce 500 SQS messages
  and zero concurrent Lambda invocations above `reserved_concurrent_executions`.
- Duplicate fires for the same entity within 5 minutes are deduplicated by SQS
  FIFO content-based deduplication — only one execution starts.
- A Lambda throttle (`TooManyRequestsException`) at any pipeline stage retries
  up to 5 times with full-jitter backoff before the execution is marked failed.
- Manual triggers via `scripts/trigger_extraction.py` continue to work
  unchanged (direct `states:StartExecution` path).

---

## 2. Design Patterns

### 2.1 [P1] Remove Dead `_GATE_ORDER` Variable

**Problem**  
`governance/source_onboarding_registry.py` line 43:
```python
_GATE_ORDER: Final[tuple[OnboardingGate, ...]] = ()  # populated after StrEnum defined
```
`Final` prevents reassignment. The comment is false. The variable is never
read — all logic uses `_GATE_ORDER_LIST`. A future developer could trust this
constant and introduce a bug.

**Proposed Solution**  
Remove `_GATE_ORDER` entirely. Rename `_GATE_ORDER_LIST` to `_GATE_ORDER`
(the intended name) so the name is canonical again.

```python
# Before
_GATE_ORDER: Final[tuple[OnboardingGate, ...]] = ()  # dead
_GATE_ORDER_LIST: Final[list[OnboardingGate]] = [...]

# After
_GATE_ORDER: Final[tuple[OnboardingGate, ...]] = (
    OnboardingGate.SOURCE_REGISTRATION,
    OnboardingGate.CREDENTIAL_REGISTRATION,
    OnboardingGate.ENTITY_MAPPING,
    OnboardingGate.EXTRACTION_PROFILE,
    OnboardingGate.SECURITY_GOVERNANCE,
    OnboardingGate.ACCEPTANCE_VALIDATION,
)
```

Update all six call sites to use `_GATE_ORDER` (already correct references,
just renamed from `_GATE_ORDER_LIST`).

**Affected files:**
```
governance/source_onboarding_registry.py   — rename + remove dead variable
governance/tests/test_source_onboarding.py — no change (tests use the client)
```

---

### 2.2 [P1] Per-Connector `connector_params` Schema Validation

**Problem**  
`connector_params` is accepted as `dict[str, str]` from the Step Functions
event. Salesforce validates `object_name` inside the adapter, but this happens
deep in the call stack. Other keys are unchecked.

**Proposed Solution**

Define a Pydantic params model per connector and validate at the handler entry
point before any AWS call:

```python
# connector_runtime/adapters/salesforce/salesforce_params.py
class SalesforceConnectorParams(BaseModel):
    model_config = {"extra": "forbid"}
    object_name: str = Field(..., pattern=r"^[A-Za-z][A-Za-z0-9_]{0,79}$")

# connector_runtime/adapters/mysql_rds/mysql_rds_params.py
class MySqlRdsConnectorParams(BaseModel):
    model_config = {"extra": "forbid"}
    table_name: str = Field(..., pattern=r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
```

The connector registry maps `source_id → params_model_class`. Validation runs
in `extraction_pipeline_handler.py` immediately after event validation:

```python
params_model_cls = connector_registry.get_params_model(source_id)
if params_model_cls is not None:
    try:
        params_model_cls.model_validate(connector_params)
    except ValidationError as exc:
        raise ValueError(f"Invalid connector_params: {exc}") from exc
```

**Affected files:**
```
connector_runtime/adapters/salesforce/salesforce_params.py  — new
connector_runtime/adapters/mysql_rds/mysql_rds_params.py    — new
connector_runtime/adapters/netsuite/netsuite_params.py      — new
connector_runtime/adapters/sage/sage_params.py              — new
connector_runtime/registry.py                               — add params_model registration
connector_runtime/extraction_pipeline_handler.py            — validate at entry
```

---

### 2.3 [P1] Replace `_table_to_records` Full Materialisation with Batch Iteration

**Problem**  
`_table_to_records(table)` in `transformation/transformation_pipeline.py`
converts an entire PyArrow table to a list of Python dicts before yielding.
For a large Parquet file (e.g., 500MB, 2M rows), this materialises the entire
file in Python heap before the first record is yielded.

**Proposed Solution**  
Use PyArrow `RecordBatch` iteration (`table.to_batches(max_chunksize=10_000)`)
which yields slices of the Arrow table without materialising all rows:

```python
def _iter_raw_records(s3, bucket, raw_s3_prefix) -> Iterator[dict[str, Any]]:
    ...
    for page in paginator.paginate(...):
        for obj in page.get("Contents", []):
            ...
            table = pq.read_table(buf)
            for batch in table.to_batches(max_chunksize=10_000):
                # Only 10K rows in Python heap at a time
                batch_dict = batch.to_pydict()
                n_rows = batch.num_rows
                cols = list(batch_dict.keys())
                for i in range(n_rows):
                    yield {col: batch_dict[col][i] for col in cols}
            del table
```

Peak memory per file drops from `O(file_rows)` to `O(max_chunksize)`.

**Affected files:**
```
transformation/transformation_pipeline.py  — _iter_raw_records + _table_to_records
transformation/tests/                       — update unit tests for batch iteration
```

---

## 3. Application Performance

### 3.1 [P0] Replace In-Memory SCD Accumulator with Server-Side Merge

**Problem**  
`CuratedAccumulator` calls `load_curated_records()` which reads ALL previous
curated records into a Python list:
```python
records.extend(table.to_pylist())   # unbounded — all records in RAM
```
Then `merge_records()` copies the list into a dict (2× RAM peak).
For a 1M-record entity: 1M × 400 bytes × 2 = ~800MB before the merge starts.
This exhausts Lambda memory well before millions-of-records scale.

**Proposed Solution: DuckDB in-process MERGE**

Replace the load-into-memory pattern with an in-process DuckDB MERGE that
streams directly from S3 Parquet without materialising into Python dicts:

```python
import duckdb

def merge_with_duckdb(
    s3_previous_prefix: str,
    delta_records: list[dict[str, Any]],   # today's delta (already in memory)
    pk_field: str,
    soft_delete_field: str | None,
    s3_bucket: str,
    region_name: str,
) -> Iterator[dict[str, Any]]:
    """Stream-merge previous state + delta using DuckDB without full RAM load."""
    con = duckdb.connect(":memory:")
    # Install and load httpfs for S3 access
    con.execute("INSTALL httpfs; LOAD httpfs;")
    con.execute(f"SET s3_region='{region_name}';")

    # Register delta as an in-memory table (delta is small — only today's changes)
    con.register("delta", pa.Table.from_pylist(delta_records))

    # Stream-read previous state directly from S3 Parquet (never fully in Python RAM)
    previous_glob = f"s3://{s3_bucket}/{s3_previous_prefix}*.parquet"

    # SCD Type 1 MERGE: delta wins on pk match; keep previous rows not in delta
    merged = con.execute(f"""
        SELECT * FROM read_parquet('{previous_glob}')
        WHERE {pk_field} NOT IN (SELECT {pk_field} FROM delta)
        UNION ALL
        SELECT * FROM delta
        WHERE {soft_delete_field or 'true'} IS NOT TRUE
    """).arrow()

    yield from merged.to_batches(max_chunksize=50_000)
    con.close()
```

The previous state is never in Python RAM — DuckDB reads the Parquet files
directly via its S3 reader and streams the join result.

*Alternative for entities too large even for DuckDB within Lambda (>50M rows):*  
Add a config flag `merge_strategy: "lambda_duckdb" | "glue_merge"` to
`EntityExtractionConfig`. When set to `"glue_merge"`, the transformation Lambda
submits an AWS Glue Streaming job instead of running the merge inline. The Glue
job writes the merged output directly to the curated partition and signals
completion via an SQS message. The Step Functions state machine awaits the SQS
signal (`.waitForTaskToken` pattern) instead of waiting for Lambda completion.

**Affected files:**
```
transformation/curated_accumulator.py            — replace merge_records + load logic
transformation/curated_utils.py                  — add duckdb merge helper
transformation/transformation_pipeline.py        — update accumulate() call site
pyproject.toml                                    — add duckdb>=0.10 dependency
infrastructure/modules/transformation_lambda/main.tf — increase memory for DuckDB (2048MB)
contracts/entity_configuration_contract.py        — add merge_strategy field (optional, default "lambda_duckdb")
infrastructure/modules/glue/                      — new module for Glue merge job (P1 for large entities)
```

---

### 3.2 [P0] Stream Canonical Records Through to Curated Writer (Eliminate `canonical_records` List)

**Problem**  
`transformation_pipeline.py` accumulates all mapped records into
`canonical_records: list[dict[str, Any]]` before any downstream step.
For 1M records this is a full in-memory copy regardless of whether the
accumulator is active.

**Proposed Solution**

Refactor the pipeline to use a two-pass approach:
- **Pass 1 (quality scan):** Stream records through the quality evaluator using
  a `QualityPolicyEvaluator.streaming_evaluate()` method that computes stats
  without retaining records. Returns a `QualityReport` + `bool is_blocked`.
- **Pass 2 (write):** If not blocked, stream records a second time directly
  from `_iter_raw_records` → apply_mapping → masking → curated writer.

The curated writer is refactored to accept an `Iterator[dict[str, Any]]` and
write using PyArrow `RecordBatchWriter` to an S3 multipart upload, so no full
list is ever materialised:

```python
class CuratedLayerWriter:
    def write_streaming(
        self,
        records: Iterator[dict[str, Any]],
        schema: pa.Schema,
        ...
    ) -> CuratedWriteResult:
        """Write records via PyArrow streaming writer + S3 multipart upload."""
        part_size_bytes = 64 * 1024 * 1024  # 64 MB per S3 part
        mpu = self._s3.create_multipart_upload(Bucket=..., Key=key)
        writer = pq.ParquetWriter(buffer, schema, compression="snappy")
        for batch in _iter_record_batches(records, batch_size=50_000):
            writer.write_batch(batch)
            if buffer.tell() >= part_size_bytes:
                _upload_part(...)
                buffer.seek(0); buffer.truncate(0)
        writer.close()
        _upload_final_part(...)
        self._s3.complete_multipart_upload(...)
```

Peak memory: O(batch_size) = O(50K records × 400 bytes) ≈ 20MB regardless of
total record count.

**Affected files:**
```
transformation/transformation_pipeline.py              — remove canonical_records list; two-pass
transformation/curated_layer_writer.py                 — add write_streaming() method
transformation/quality_evaluation/quality_policy_evaluator.py — add streaming_evaluate()
transformation/tests/                                   — update tests
```

---

### 3.3 [P0] S3 Multipart Upload for All Large Parquet Writes

**Problem**  
`CuratedLayerWriter.write()`, `GoldenRecordPublisher`, `CanonicalRecordPublisher`,
and `analytics_publisher_handler.py` all use single `put_object` calls.
The S3 single PUT limit is 5GB. For >100MB files (common at millions of records),
single-PUT is slow, memory-intensive, and failure-prone (one retry re-uploads
the whole file).

**Proposed Solution**

Extract a shared `S3ParquetWriter` utility into `observability/s3_writer.py`
(available to all pipeline stages) that automatically selects:
- Single PUT for < 8MB (avoids multipart overhead for small files)
- Multipart upload for ≥ 8MB (64MB parts; parallel part uploads optional)

```python
# observability/s3_writer.py
class S3ParquetWriter:
    MULTIPART_THRESHOLD_BYTES: Final[int] = 8 * 1024 * 1024

    def write(
        self,
        records_iter: Iterator[dict[str, Any]],
        bucket: str,
        key: str,
        schema: pa.Schema | None = None,
        compression: str = "snappy",
    ) -> int:
        """Write records to S3 Parquet. Returns record count."""
```

All five write sites import this single implementation.

**Affected files:**
```
observability/s3_writer.py                                  — new shared utility
transformation/curated_layer_writer.py                      — use S3ParquetWriter
entity_resolution/canonical_record_publisher/               — use S3ParquetWriter
entity_resolution/golden_record_publisher/                  — use S3ParquetWriter
analytics_publisher/analytics_publisher_handler.py          — use S3ParquetWriter
```

---

### 3.4 [P0] Stream Entity Resolution and Analytics Publisher

**Problem**  
`entity_resolution_pipeline_handler.py` calls `load_curated_records()` for
each contributing source then holds all records in memory during matching.
`analytics_publisher_handler.py` calls `_load_parquet_records()` then processes
all golden records in a list.

**Proposed Solution**

*Entity resolution:*  
Use DuckDB to join multi-source curated records and produce candidate pairs
without loading all sources into Python RAM simultaneously:

```python
# In the ER handler: DuckDB joins sources; Python only processes candidate blocks
con.execute("""
    SELECT s1.*, s2.* FROM salesforce_curated s1
    JOIN netsuite_curated s2
    ON s1.email_domain = s2.email_domain
""")  # DuckDB streams the join; Python gets match candidates batch by batch
```

The `RecordBlocker` processes one block at a time (already designed for this),
so the only change is feeding it from a DuckDB streaming cursor instead of a
pre-loaded Python list.

*Analytics publisher:*  
Replace `_load_parquet_records()` with a streaming iterator that processes
golden records in 50K-row batches. Each batch is stripped of system fields and
flushed to the PyArrow multipart writer before the next batch is loaded.

**Affected files:**
```
entity_resolution/entity_resolution_pipeline_handler.py   — DuckDB-based multi-source join
entity_resolution/matching_engine/record_blocker.py       — accept iterator input
analytics_publisher/analytics_publisher_handler.py        — streaming batch processing
```

---

### 3.5 [P1] Lambda Timeout Safety Valve for Large Entities

**Problem**  
Lambda hard limit is 900 seconds. Entities with millions of records (NetSuite,
Sage, Salesforce full loads) can exceed this. `GAP-P2` in the repo is
unresolved.

**Proposed Solution**

*Extraction stage:* Add a checkpoint-and-resume mechanism using the watermark
table. The extraction handler checks remaining Lambda time every 10,000 records.
If < 120s remain, it commits a partial watermark, writes the records extracted
so far, and raises `LambdaTimeoutWarning` (a new exception class). Step
Functions catches this as a non-fatal error and re-triggers extraction from the
partial watermark. The `run_id` carries a `-part{n}` suffix for partial runs.

*Transformation stage:* Already safe because it reads from S3 (bounded by
file size, not extraction time). If a single raw file is very large, add
`EntityExtractionConfig.raw_partition_size_mb` (default: 256MB) that causes
the extraction writer to split into multiple files.

*Configuration flag:*
```python
class EntityExtractionConfig(BaseModel):
    max_records_per_lambda_run: int | None = Field(
        default=None,
        description="Hard cap on records per Lambda invocation. "
                    "Triggers checkpoint-and-resume when reached."
    )
```

**Affected files:**
```
contracts/entity_configuration_contract.py           — max_records_per_lambda_run field
connector_runtime/interfaces/connector_interface.py  — ExtractionCheckpointResult
orchestration/step_functions/extraction_workflow.py  — checkpoint logic
orchestration/step_functions/                        — extraction_workflow state machine JSON update
infrastructure/modules/orchestration/main.tf         — new Catch/Retry for LambdaTimeoutWarning
```

---

### 3.6 [P1] NetSuite Page Size Increase

**Problem**  
`connector_runtime/adapters/netsuite/netsuite_connector.py` uses
`_PAGE_SIZE = 1_000`. For a 1M-record entity this requires 1,000 API calls.

**Proposed Solution**  
Increase to `_PAGE_SIZE = 10_000` (NetSuite SuiteTalk REST maximum).
Add `connector_params.page_size` override in `NetsuitConnectorParams` so
tenants can tune per entity without code changes.

**Affected files:**
```
connector_runtime/adapters/netsuite/netsuite_connector.py
connector_runtime/adapters/netsuite/netsuite_params.py   — add page_size field
```

---

### 3.7 [P1] Lambda Memory Sizing per Entity Type

**Problem**  
All Lambda functions use a flat 1024MB across all environments and entity types.
DuckDB-based merge and large-entity processing require more memory.

**Proposed Solution**  
Add a `lambda_memory_mb` override to `EntityExtractionConfig` (optional,
default: null). When set, the Step Functions `Parameters` block passes this
value as `lambdaMemoryOverrideMb` to a Lambda function configured with
Provisioned Concurrency aliases per memory tier:

- `{function-name}:memory-1024` — 1024MB (default, small entities)
- `{function-name}:memory-4096` — 4096MB (medium entities, DuckDB merge)
- `{function-name}:memory-8192` — 8192MB (large entities)

Step Functions invokes the appropriate alias based on the config value.

**Affected files:**
```
contracts/entity_configuration_contract.py              — lambda_memory_mb field
infrastructure/modules/transformation_lambda/main.tf    — Lambda aliases per tier
infrastructure/modules/orchestration/main.tf            — alias ARN selection in SFN
```

---

## 4. Security

### 4.1 [P0] Remove Dead `_GATE_ORDER` Code

See **2.1** above. Duplicate cross-reference for the security dimension:
the misleading comment `# populated after StrEnum defined` could cause a
developer to add a re-assignment attempt that would silently be ignored (Python
ignores re-assignment to a `Final` at module level only in type checkers — at
runtime it would be a `TypeError` from `typing`). Remove and consolidate.

---

### 4.2 [P1] Add CloudWatch Alarm on Circuit Breaker DynamoDB Fallback

**Problem**  
When the distributed circuit breaker cannot reach DynamoDB, it silently falls
back to in-process state and logs `_logger.warning("circuit_breaker_ddb_init_failed")`.
No alarm fires. A VPC routing misconfiguration could silently disable
cross-container circuit protection for days.

**Proposed Solution**

Add a CloudWatch metric filter on the circuit breaker log group that converts
the `circuit_breaker_ddb_init_failed` log event into a `CircuitBreakerDDBFallback`
metric, and an alarm at threshold > 0:

```hcl
# infrastructure/modules/observability/main.tf
resource "aws_cloudwatch_log_metric_filter" "cb_ddb_fallback" {
  name           = "${var.environment}-cb-ddb-fallback"
  pattern        = "{ $.event = \"circuit_breaker_ddb_init_failed\" }"
  log_group_name = "/edl/${var.environment}/connector-runtime"

  metric_transformation {
    name      = "CircuitBreakerDDBFallback"
    namespace = "EnterpriseDatalake"
    value     = "1"
  }
}

resource "aws_cloudwatch_metric_alarm" "cb_ddb_fallback" {
  alarm_name          = "${var.environment}-edl-circuit-breaker-ddb-fallback"
  alarm_description   = "Circuit breaker fell back to in-process state. Distributed protection disabled."
  metric_name         = "CircuitBreakerDDBFallback"
  namespace           = "EnterpriseDatalake"
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  period              = 300
  evaluation_periods  = 1
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.platform_alerts.arn]
}
```

**Affected files:**
```
infrastructure/modules/observability/main.tf   — metric filter + alarm
```

---

### 4.3 [P1] Automated Secrets Manager Rotation

**Problem**  
`source_onboarding_registry.py` requires a `CREDENTIAL_REGISTRATION` gate but
actual rotation schedules are not managed in Terraform. Credentials never rotate
unless manually updated.

**Proposed Solution**

Add `aws_secretsmanager_secret_rotation` resources to `modules/secrets/main.tf`
for each connector credential type. The rotation Lambda is a thin wrapper that
calls the source system's "reset client secret" or "regenerate API key" API:

```hcl
# infrastructure/modules/secrets/main.tf
resource "aws_secretsmanager_secret_rotation" "salesforce_credentials" {
  secret_id           = aws_secretsmanager_secret.salesforce_credentials.id
  rotation_lambda_arn = aws_lambda_function.credential_rotator.arn
  rotation_rules {
    automatically_after_days = var.credential_rotation_days  # default: 90
  }
}
```

For connectors that do not support programmatic rotation (e.g., Sage Intacct),
add a rotation notification Lambda that sends an SNS alert 7 days before
expiry so the operations team can rotate manually.

**Affected files:**
```
infrastructure/modules/secrets/main.tf          — rotation resources
infrastructure/modules/secrets/variables.tf     — rotation_days variable
connector_runtime/credential_rotator/           — new rotation Lambda package
connector_runtime/credential_rotator/handler.py
```

---

### 4.4 [P1] DLQ Consumer with Access Control and Replay Audit

**Problem**  
The SQS DLQ exists and has correct IAM restrictions, but there is no Lambda
consumer. Messages accumulate silently with no processing, no depth alarm, and
no automated replay path.

**Proposed Solution**

Add a DLQ processor Lambda (`orchestration/dlq_processor/dlq_processor_handler.py`)
that:
1. Reads messages from the DLQ
2. Validates the message schema (Pydantic)
3. Logs an audit record to `{env}-edl-run-audit-log` with `stage=dlq_received`
4. Optionally re-submits to Step Functions for replay (when `auto_replay: true`
   in config; disabled by default)
5. Sends SNS notification with run_id, source_id, entity_id, and failure reason

The DLQ processor is triggered by an SQS Event Source Mapping with
`batch_size=1` (one message at a time for clear audit records).

**Affected files:**
```
orchestration/dlq_processor/                            — new package
orchestration/dlq_processor/dlq_processor_handler.py
infrastructure/modules/orchestration/main.tf            — DLQ processor Lambda + ESM
infrastructure/modules/iam/main.tf                      — DLQ processor IAM role
```

---

### 4.5 [P2] CloudWatch Log Metric Filters for Security Events

**Problem**  
There are no metric filters detecting repeated input validation failures,
unexpected `ValueError` spikes (possible injection probing), or auth errors.

**Proposed Solution**

Add metric filters for:

| Log Event | Metric | Alarm Threshold |
|-----------|--------|-----------------|
| Input validation failure (`ValueError` in handler) | `InputValidationFailures` | > 5 in 5 min |
| Credential retrieval failure | `CredentialRetrievalFailures` | > 0 |
| DeterministicInvalidCredentials classification | `InvalidCredentialClassifications` | > 0 |
| Schema drift BREAKING classification | `BreakingSchemaDriftCount` | > 0 (already exists) |
| Circuit breaker opened | `CircuitBreakerOpened` | > 0 |

```hcl
resource "aws_cloudwatch_log_metric_filter" "input_validation_failures" {
  name           = "${var.environment}-input-validation-failures"
  pattern        = "{ $.level = \"error\" && $.event = \"input_validation_failed\" }"
  log_group_name = "/edl/${var.environment}/connector-runtime"
  metric_transformation {
    name      = "InputValidationFailures"
    namespace = "EnterpriseDatalake"
    value     = "1"
  }
}
```

**Affected files:**
```
infrastructure/modules/observability/main.tf   — 5 metric filters + alarms
```

---

## 5. Monitoring and Observability

### 5.1 [P0] Fix Transformation Dashboard — Add `Stage` Dimension to Metrics Emitter

**Problem**  
`CloudWatchMetricsEmitter._build_dimensions()` emits only `SourceId`,
`EntityId`, `Environment`. The `transformation_slo` CloudWatch dashboard
queries `["EnterpriseDatalake", "RecordsExtracted", "Stage", "transformation"]`.
Because `Stage` is never emitted, the dashboard shows zero data for all widgets,
and the `transformation_quality_blocked` alarm never fires even when quality
violations occur.

**Proposed Solution**

Add an optional `stage` parameter to every `emit_*` method, and include
`Stage` in the dimension set when provided:

```python
def emit_records_extracted(
    self,
    source_id: str,
    entity_id: str,
    environment: str,
    count: int,
    stage: str | None = None,          # NEW — optional, defaults to None
) -> None:
    dims = self._build_dimensions(source_id, entity_id, environment, stage=stage)
    ...

@staticmethod
def _build_dimensions(
    source_id: str, entity_id: str, environment: str,
    stage: str | None = None,
) -> list[dict[str, str]]:
    dims = [
        {"Name": "SourceId",     "Value": source_id},
        {"Name": "EntityId",     "Value": entity_id},
        {"Name": "Environment",  "Value": environment},
    ]
    if stage:
        dims.append({"Name": "Stage", "Value": stage})
    return dims
```

Update call sites:
- Extraction pipeline: `stage="extraction"` on all emit calls
- Transformation pipeline `_emit_transformation_metrics`: `stage="transformation"`
- Entity resolution handler: `stage="entity_resolution"` (new — see 5.2)
- Analytics publisher: `stage="analytics_publication"` (new — see 5.2)

This change is fully backward-compatible: existing CloudWatch alarms that
query without a `Stage` dimension filter continue to match all metrics
(CloudWatch sums across all dimension combinations by default).

**Affected files:**
```
observability/metrics_emitter.py                        — add stage param to all emit_* methods
transformation/transformation_pipeline.py               — pass stage="transformation"
connector_runtime/run_lifecycle/run_lifecycle.py         — pass stage="extraction" (if emitter called there)
observability/tests/test_metrics_emitter.py             — update tests
```

---

### 5.2 [P0] Add CloudWatch Metrics to Entity Resolution and Analytics Publisher

**Problem**  
Both handlers have zero `CloudWatchMetricsEmitter` integration. No metrics are
emitted for golden record count, cluster count, analytics record count, or
stage duration. These pipeline stages are invisible to CloudWatch.

**Proposed Solution**

*New metrics to add to `CloudWatchMetricsEmitter`:*

```python
def emit_stage_duration(
    self, source_id, entity_id, environment, stage, duration_ms
) -> None: ...

def emit_golden_record_count(
    self, source_id, entity_id, environment, count
) -> None: ...

def emit_cluster_count(
    self, source_id, entity_id, environment, count
) -> None: ...
```

*Entity resolution handler:* Wire a `CloudWatchMetricsEmitter` instance
(same as extraction and transformation handlers). Emit at stage end:

```python
metrics_emitter.emit_stage_duration(..., stage="entity_resolution", ...)
metrics_emitter.emit_golden_record_count(..., count=golden_record_count)
metrics_emitter.emit_cluster_count(..., count=cluster_count)
metrics_emitter.emit_records_failed(..., stage="entity_resolution",
    count=failed_match_count)
metrics_emitter.flush()
```

*Analytics publisher handler:* Same pattern:
```python
metrics_emitter.emit_stage_duration(..., stage="analytics_publication", ...)
metrics_emitter.emit_records_extracted(..., stage="analytics_publication",
    count=analytics_record_count)
metrics_emitter.flush()
```

*Add dashboards for entity resolution and analytics stages to
`infrastructure/modules/observability/main.tf`.*

**Affected files:**
```
observability/metrics_emitter.py                             — new emit methods
entity_resolution/entity_resolution_pipeline_handler.py      — wire emitter
analytics_publisher/analytics_publisher_handler.py            — wire emitter
infrastructure/modules/observability/main.tf                  — new dashboards + alarms
```

---

### 5.3 [P0] Add DLQ Depth Alarm

**Problem**  
The extraction failure DLQ has no `aws_cloudwatch_metric_alarm` on
`ApproximateNumberOfMessagesVisible`. Failed run messages accumulate silently.

**Proposed Solution**

```hcl
# infrastructure/modules/observability/main.tf
resource "aws_cloudwatch_metric_alarm" "dlq_messages_present" {
  alarm_name          = "${var.environment}-edl-dlq-messages-present"
  alarm_description   = "Extraction failure DLQ contains unprocessed messages. Investigate failed runs."
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  period              = 60
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  dimensions          = {
    QueueName = "${var.environment}-edl-extraction-failure-dlq"
  }
  statistic           = "Maximum"
  threshold           = 0
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.platform_alerts.arn]
}
```

The DLQ queue name must be passed as a variable from `modules/metadata_persistence/`.

**Affected files:**
```
infrastructure/modules/observability/main.tf      — DLQ alarm resource
infrastructure/modules/observability/variables.tf — extraction_failure_dlq_name variable
infrastructure/environments/dev/main.tf           — wire dlq_name into observability module
infrastructure/environments/staging/main.tf
infrastructure/environments/prod/main.tf
```

---

### 5.4 [P1] Add Lambda-Level Error, Duration, and Throttle Alarms

**Problem**  
No alarms on Lambda built-in metrics. An OOM kill (`Lambda.AWSLambdaException`)
or a function approaching the 900s timeout would not fire its own alert — the
SFN execution failure alarm is the only signal, which comes too late for
diagnosis.

**Proposed Solution**

Add a reusable `lambda_alarms` sub-module in `modules/observability/` that
generates three alarms per Lambda function:

```hcl
module "lambda_alarms" {
  for_each = {
    extraction    = var.extraction_lambda_name
    transformation = var.transformation_lambda_name
    entity_resolution = var.entity_resolution_lambda_name
    analytics_publisher = var.analytics_publisher_lambda_name
  }

  source        = "./lambda_alarms"
  function_name = each.value
  environment   = var.environment
  alert_topic_arn = aws_sns_topic.platform_alerts.arn
}

# lambda_alarms/main.tf creates:
# 1. Errors > 0 — any Lambda error
# 2. Duration > (timeout_seconds * 0.85 * 1000) ms — 85% of timeout
# 3. Throttles > 0 — concurrency limit hit
```

**Affected files:**
```
infrastructure/modules/observability/lambda_alarms/main.tf   — new sub-module
infrastructure/modules/observability/main.tf                 — call sub-module per function
infrastructure/modules/observability/variables.tf            — lambda name variables
infrastructure/environments/{dev,staging,prod}/main.tf       — pass lambda names
```

---

### 5.5 [P1] Add AWS X-Ray SDK Instrumentation

**Problem**  
Lambda Active mode captures function-level X-Ray traces only. Individual boto3
calls (S3 `get_object`, DynamoDB `GetItem`, Secrets Manager `GetSecretValue`)
do not appear as subsegments in traces. You cannot identify which specific AWS
call is slow within a pipeline run.

**Proposed Solution**

Add `aws-xray-sdk` to `pyproject.toml` and patch all boto3 clients at Lambda
startup:

```python
# In each Lambda handler module, before any boto3 client creation:
from aws_xray_sdk.core import xray_recorder, patch_all
patch_all()  # instruments boto3, requests, pymysql, and other supported libs
```

Add custom subsegments for business-logic sections whose duration is important:

```python
with xray_recorder.in_subsegment("field_mapping"):
    for record in _iter_raw_records(...):
        canonical_records.append(applicator.apply(record, rule_set))

with xray_recorder.in_subsegment("scd_merge"):
    acc_result = self._curated_accumulator.accumulate(...)
```

Add `tenant_code`, `source_id`, `entity_id`, and `run_id` as X-Ray annotations
on every trace:

```python
xray_recorder.put_annotation("tenant_code", tenant_code)
xray_recorder.put_annotation("source_id", source_id)
xray_recorder.put_annotation("run_id", run_id)
```

The X-Ray tracing group in `modules/observability/main.tf` already exists and
will automatically group these enriched traces.

**Affected files:**
```
pyproject.toml                                     — add aws-xray-sdk>=2.14
connector_runtime/extraction_pipeline_handler.py   — patch_all() + annotations
transformation/transformation_pipeline_handler.py  — patch_all() + subsegments
entity_resolution/entity_resolution_pipeline_handler.py
analytics_publisher/analytics_publisher_handler.py
observability/lambda_utils.py                      — add configure_xray() helper
```

---

### 5.6 [P1] Pre-Built CloudWatch Logs Insights Queries

**Problem**  
There are no saved `aws_cloudwatch_query_definition` resources. On-call
engineers must construct queries from scratch during incidents.

**Proposed Solution**

Add a Terraform resource block for each saved query:

```hcl
resource "aws_cloudwatch_query_definition" "failed_runs_last_24h" {
  name = "${var.environment}/edl/failed-runs-last-24h"
  log_group_names = [
    "/edl/${var.environment}/connector-runtime",
    "/edl/${var.environment}/transformation",
  ]
  query_string = <<-EOT
    fields run_id, source_id, entity_id, @timestamp
    | filter level = "error"
    | sort @timestamp desc
    | limit 50
  EOT
}
```

Saved queries to create:

| Name | Purpose |
|------|---------|
| `failed-runs-last-24h` | All ERROR-level events last 24h, by run_id |
| `mapping-failures-by-entity` | Count of mapping failures per entity_id |
| `schema-drift-events` | All BREAKING drift events |
| `watermark-lag-by-source` | Latest watermark lag per source |
| `circuit-breaker-events` | All circuit breaker open/reset events |
| `cold-start-duration` | Lambda init duration (REPORT lines) |
| `dlq-enqueue-history` | All DLQ entries with run_id + failure reason |

**Affected files:**
```
infrastructure/modules/observability/main.tf   — 7 query definition resources
```

---

### 5.7 [P2] End-to-End SLA Metric

**Problem**  
There is no metric for total pipeline duration from extraction trigger to
analytics publication. For SaaS SLA commitments (e.g., "data refreshed within
4 hours"), this is essential.

**Proposed Solution**

The analytics publisher handler has access to the original extraction `run_id`
(passed through via Step Functions state). Add a `run_start_time` field to the
Step Functions execution input (set by the trigger script / control plane API
at execution start). The analytics publisher computes:

```python
e2e_duration_ms = (datetime.now(UTC) - run_start_time).total_seconds() * 1000
metrics_emitter.emit_stage_duration(
    ..., stage="e2e_pipeline", duration_ms=e2e_duration_ms
)
```

Add a CloudWatch alarm: `E2EPipelineDuration > SLA_threshold_ms`
(configurable per environment; default 4 hours = 14,400,000ms).

**Affected files:**
```
analytics_publisher/analytics_publisher_handler.py        — compute + emit e2e duration
infrastructure/modules/orchestration/main.tf              — add run_start_time to SFN input
infrastructure/modules/observability/main.tf              — E2E SLA alarm
```

---

### 5.8 [P2] PagerDuty / OpsGenie Integration

**Problem**  
SNS has an email subscription only. No on-call routing or escalation policy.

**Proposed Solution**

Add an HTTPS SNS subscription endpoint for the incident management platform.
The SNS topic already exists and is KMS-encrypted; only an endpoint needs to
be added:

```hcl
# infrastructure/modules/observability/main.tf
resource "aws_sns_topic_subscription" "pagerduty" {
  count     = var.pagerduty_integration_url != "" ? 1 : 0
  topic_arn = aws_sns_topic.platform_alerts.arn
  protocol  = "https"
  endpoint  = var.pagerduty_integration_url
  endpoint_auto_confirms = true
}
```

The PagerDuty/OpsGenie integration URL is stored in Secrets Manager
(`{env}/ops/pagerduty_integration_url`) and retrieved by a Terraform data
source, not hardcoded.

**Affected files:**
```
infrastructure/modules/observability/main.tf       — sns subscription resource
infrastructure/modules/observability/variables.tf  — pagerduty_integration_url variable
infrastructure/environments/{dev,staging,prod}/main.tf
```

---

## Implementation Sequence

The work is ordered so that each phase leaves the platform in a fully
functioning state. No phase breaks any running pipeline.

```
Phase 1 — Foundation fixes (implemented before first customer launch)
  ├── 1.6  SQS burst buffer + Lambda.TooManyRequestsException retry fix + reserved concurrency
  │         (P0 — implemented now at 5 dev entities before scale to 80-100 at launch)
  ├── 2.1  Remove dead _GATE_ORDER / rename _GATE_ORDER_LIST
  ├── 5.1  Fix Stage dimension in CloudWatchMetricsEmitter (1-line additive change)
  ├── 5.3  Add DLQ depth alarm (Terraform only)
  └── 5.4  Add Lambda error/duration/throttle alarms (Terraform only)

Phase 2 — Observability completeness
  ├── 5.2  Wire metrics into entity resolution + analytics publisher handlers
  ├── 5.5  Add X-Ray SDK (pyproject + patch_all + annotations)
  ├── 5.6  CloudWatch Logs Insights saved queries
  ├── 4.2  Circuit breaker DDB fallback alarm (metric filter + Terraform)
  └── 4.5  Security event metric filters + alarms

Phase 3 — Performance (no SaaS tenancy required; works on existing single-tenant setup)
  ├── 2.3  Batch iteration in _iter_raw_records / _table_to_records
  ├── 3.3  Shared S3ParquetWriter with multipart upload
  ├── 3.2  Stream canonical records (two-pass pipeline)
  ├── 3.4  Stream entity resolution + analytics publisher (DuckDB join)
  ├── 3.1  Replace in-memory SCD accumulator with DuckDB merge
  ├── 3.5  Checkpoint-and-resume for large entities
  └── 3.6  NetSuite page size + 3.7 Lambda memory aliases

Phase 4 — Design hygiene
  └── 2.2  Per-connector connector_params schema validation (Pydantic)

Phase 5 — Security hardening
  ├── 4.3  Automated Secrets Manager rotation
  └── 4.4  DLQ consumer Lambda + replay audit

Phase 6 — SaaS foundations (requires Phase 1-5 complete)
  ├── 1.3  Config-driven entity type registry (DynamoDB-backed)
  ├── 1.4  Config-driven survivorship policy (S3-backed)
  ├── 1.5  Terraform-managed DynamoDB tables
  └── 1.1  Multi-tenancy data model (tenant_code slug in all paths + IAM scoping)

Phase 7 — SaaS control plane
  └── 1.2  Control plane API (API Gateway + Cognito + WAF)

Phase 8 — SaaS operational excellence
  ├── 5.7  End-to-end SLA metric
  └── 5.8  PagerDuty / OpsGenie integration
```

---

## Risk Register

| Risk | Mitigation |
|------|-----------|
| DuckDB Lambda layer size (+~30MB compressed) | Use Lambda Layer or container image |
| `aws-xray-sdk` `patch_all()` breaks pymysql | Pin `aws-xray-sdk>=2.14`; test with moto in CI |
| Two-pass raw read doubles S3 GET cost | First pass is quality-only (fast streaming); accept cost for correctness |
| Multi-tenancy S3 path change requires data migration | `tenant_code=default` maps existing paths to `default/raw/...`, `default/curated/...`; no data movement needed for dev |
| Two tenants accidentally choosing the same `tenant_code` slug | Control plane validates uniqueness at registration time via DynamoDB conditional `attribute_not_exists(tenant_code)` |
| DynamoDB table Terraform import may fail for pre-existing tables | Test import in a staging account before running in prod |
| SQS FIFO dedup window is 5 minutes — two schedule fires >5 min apart for the same entity both start executions | Use `StartExecution` `name` parameter as idempotency key; Step Functions rejects duplicates regardless of SQS dedup window |
| Pipeline Trigger Lambda crash between SQS receive and `StartExecution` causes message re-delivery after visibility timeout | `StartExecution` with a deterministic `name` makes re-delivery a safe no-op |
| Switching EventBridge target from Step Functions to SQS requires updating all existing schedules | `ExtractionScheduleClient` manages schedules at runtime — update the target ARN in one place and re-seed schedules |
| `max_chunksize` batch size tuning | Start at 10K; instrument with X-Ray to find optimal value |

---

## Non-Goals (Explicitly Out of Scope)

- Re-architecting the Step Functions state machine topology
- Replacing DynamoDB with a different metadata store
- Migrating existing raw/curated S3 data to new tenant-scoped paths
- Adding new source connectors (Stripe, HubSpot, etc.)
- Changing the Parquet/Snappy storage format
- Replacing structlog with a different logging library
