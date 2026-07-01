# Sage ERP — Gap Analysis & Implementation Plan

> Platform: Enterprise Data Lake  
> Scope: Sage Intacct (Phase 5, completed 2026-07-01) → full end-to-end pipeline  
> Author: Engineering Team  
> Date: 2026-07-01

---

## 1. Current State

Phase 5 delivered the **connector layer** for Sage Intacct:

| Component | Location | Status |
|-----------|----------|--------|
| `SageConnector` (Strategy pattern) | `connector_runtime/adapters/sage/sage_connector.py` | ✅ Complete |
| `IntacctAuthClient` (OAuth 2.0 client_credentials) | `products/intacct/intacct_auth.py` | ✅ Complete |
| `IntacctMetadataClient` (Models endpoint) | `products/intacct/intacct_metadata_client.py` | ✅ Complete |
| `IntacctQueryEngine` (JSON DSL, pagination) | `products/intacct/intacct_query_engine.py` | ✅ Complete |
| `SageHttpClient`, `SageCredentialManager`, `SageRawLayerWriter` | `common/` | ✅ Complete |
| `SageProductRegistry` | `common/sage_product_registry.py` | ✅ Complete |
| Protocols for extension (`SageAuthProtocol`, etc.) | `protocols/` | ✅ Complete |
| Unit tests (9 test modules) | `connector_runtime/tests/sage/` | ✅ Complete |
| Registered with `extraction_pipeline_handler.py` | Import line added | ✅ Complete |

**What the Phase 5 connector does NOT cover:**
- Entity extraction config in DynamoDB (pipeline cannot run without this)
- Field mapping configs (transformation pipeline fails without these)
- Entity resolution integration (Sage records excluded from golden records)
- Company survivorship policy update (Sage ignored as a data source)
- Local test runner (no developer onboarding tooling)
- Two security defects in error classification and rate-limit handling
- Infrastructure: Terraform Secrets Manager placeholder
- Governance: lineage record emission for Sage runs

---

## 2. Gap Analysis

### 2.1 Security Gaps

#### GAP-S1 — `SageMetadataError` classified as `UNKNOWN` (FIXED ✅)
**File:** `sage_connector.py` → `classify_extraction_error()`  
**Risk:** An invalid `object_path` (deterministic config error) was routed to `UNKNOWN`
instead of `DETERMINISTIC_INVALID_OBJECT`. This caused Step Functions to keep retrying a
request that can never succeed, consuming API quota and delaying alerting.  
**Fix (applied):** Added `SageMetadataDeterministicError` and `SageMetadataTransientError`
subclasses in `intacct_metadata_client.py`. The `_fetch_fields` method now raises the
correct subclass (`SageObjectNotFoundError` → deterministic; other `SageHttpError` → transient).
`classify_extraction_error` now routes each subclass independently.

#### GAP-S2 — Retry-After header not surfaced (FIXED ✅)
**File:** `sage_http_client.py` → `_parse_response()`  
**Risk:** HTTP 429 from Intacct includes a `Retry-After` header indicating when the client
may retry. Ignoring it prevented operators from tuning Step Functions retry intervals, and
wasted retries that would also be rate-limited.  
**Fix (applied):** The `Retry-After` value is now extracted and emitted as a structured log
event (`sage_rate_limit_exceeded`) so CloudWatch dashboards and alerting can surface it.
The retry sleep itself is intentionally left to Step Functions (platform convention).

#### GAP-S3 — No Terraform placeholder for Sage Secrets Manager secret (OPEN)
**Risk (OWASP A07):** There is no Terraform resource creating `{env}/sources/sage/intacct/credentials`
even as a placeholder. Operators must remember to create it manually before the first run.
A missing secret causes a runtime `SageCredentialError` that is only caught when the Lambda
actually executes, with no pre-deployment validation.  
**Remediation (Phase 6):** Add a `aws_secretsmanager_secret` resource (empty placeholder with
`lifecycle { ignore_changes = [secret_string] }`) in the `iam` Terraform module and wire a
`SecretReadOnly` policy statement for `{env}-extraction-runtime-role`. This makes the secret's
existence a Terraform plan precondition rather than an undocumented manual step.

#### GAP-S4 — Credential cache TTL does not invalidate on auth failure (OPEN)
**File:** `sage_credential_manager.py`  
**Risk:** `SageCredentialManager` caches credentials for `_CREDENTIAL_CACHE_TTL_SECONDS` (3600s).
If Secrets Manager rotation fires mid-run, the connector retries with a stale `client_secret`
for up to an hour. `IntacctAuthClient.invalidate_token()` handles token-level expiry but does
not invalidate the credential cache.  
**Remediation (Phase 6):** On `IntacctAuthError` in `_refresh_token`, call
`self._credentials.invalidate()` (add a public `invalidate()` method to `SageCredentialManager`)
before re-raising so the next attempt re-fetches from Secrets Manager.

---

### 2.2 Performance Gaps

#### GAP-P1 — `PAGE_SIZE` hardcoded at 4,000 (OPEN)
**File:** `intacct_query_engine.py`  
**Impact:** Intacct's hard limit is 4,000 records per page (platform max). The value is
correct but is not configurable per entity. For objects with very wide schemas (many fields),
Intacct may return pages slower; for objects with very narrow schemas, a larger page size
would be beneficial if Intacct ever raises the limit.  
**Remediation (Phase 6):** Expose `page_size` as an optional `connector_params` key with
`PAGE_SIZE` as the default. Validate: `1 ≤ page_size ≤ PAGE_SIZE`.

#### GAP-P2 — Lambda timeout risk for large Intacct datasets (OPEN)
**Impact:** At 4,000 records/page and the default Lambda timeout (15 min), the connector
can process approximately 1.2M–2M records before timing out (assuming ~30ms/page round-trip).
Very large Intacct environments (>2M customers) will fail with a timeout.  
**Remediation (Phase 6):** Add a record-count circuit breaker: if the record count after N
pages exceeds a configurable `max_records_per_run` threshold (default: 500,000), complete the
current page, write what was collected, and record the last-seen `auditInfo.modifiedAt` as
a partial watermark so the next run resumes from there.

#### GAP-P3 — Metadata caching is per Lambda instance only (OPEN)
**Impact:** Every cold-start Lambda invocation makes a Models endpoint call to discover fields.
For high-frequency Intacct entities, this adds latency and API calls.  
**Remediation (Phase 6):** Cache the `FieldContract` fingerprint in the `schema_snapshot_repository`
(already used by the platform for drift detection). If the fingerprint matches the snapshot, skip
the live Models call and use the snapshot's field list.

#### GAP-P4 — `SageHttpClient` uses default connection pool settings (OPEN)
**File:** `sage_http_client.py`  
**Impact:** `requests.Session` defaults to a connection pool of size 10. For Lambda
(single-threaded), this is sufficient but the pool is rebuilt on every cold start.  
**Remediation (Phase 6):** Mount a `HTTPAdapter` with `pool_connections=1, pool_maxsize=2`
and enable `keep_alive` for connection reuse within a single pagination loop.

---

### 2.3 Architecture Gaps

#### GAP-A1 — No entity extraction config in DynamoDB (FIXED ✅)
**File:** `scripts/seed_entity_config.py`  
**Impact (blocker):** Without DynamoDB records for `sage-intacct-customer` and
`sage-intacct-vendor`, the extraction pipeline fails immediately when resolving entity config.  
**Fix (applied):** Added both entities to `_build_records()` with correct connector_params,
watermark field (`auditInfo.modifiedAt`), and sage-specific raw S3 prefix function
(`_sage_raw_prefix` using the `sage/{product_name}/{entity_id}/` path scheme used by
`SageRawLayerWriter`).

#### GAP-A2 — No field mapping configs (FIXED ✅)
**Files created:**
- `config/field_mappings/sage/sage-intacct-customer/v1.json`
- `config/field_mappings/sage/sage-intacct-vendor/v1.json`  

**Impact (blocker):** The transformation pipeline reads field mappings from S3. With no
mapping file, every Sage record would be dropped at the curated layer.  
**Fix (applied):** Both mappings created. Customer maps Intacct `id → account_id` (consistent
with the cross-source `company` PK convention). Vendor uses `vendor_id` (not yet in entity
resolution — tracked as GAP-A5).

#### GAP-A3 — Sage not wired into entity resolution (FIXED ✅)
**File:** `entity_resolution/entity_resolution_pipeline_handler.py`  
**Impact:** `sage-intacct-customer` triggered an unhandled `ValueError` ("No entity type
mapping found") in the entity resolution Lambda, causing the Step Functions execution to fail
at that stage even though extraction + transformation succeeded.  
**Fix (applied):** Added `sage-intacct-customer → company` to `_ENTITY_ID_TO_TYPE` and
`("sage", "sage-intacct-customer")` to `_ENTITY_TYPE_SOURCES["company"]`. Sources with no
curated data are skipped gracefully (existing platform logic).

#### GAP-A4 — Company survivorship ignored Sage Intacct (FIXED ✅)
**File:** `config/entity_resolution/company/survivorship_v1.json`  
**Impact:** Even with entity resolution wired, survivorship rules only referenced
`salesforce` and `netsuite`. Sage Intacct — the operational AR system — was not preferred
for financial fields (credit_limit, outstanding_balance, currency_code).  
**Fix (applied):** Updated all `source_priority` lists to include `sage` as the third source.
Sage is preferred for `credit_limit`, `outstanding_balance`, `currency_code`, and `is_active`
since Intacct AR is the system of record for these financial attributes.

#### GAP-A5 — `sage-intacct-vendor` not in entity resolution (OPEN)
**Impact:** Vendor records flow through extraction → transformation but fail the entity
resolution stage (no `_ENTITY_ID_TO_TYPE` mapping).  
**Remediation (Phase 6):** Design a `supplier` entity type with its own match rules and
survivorship policy. Candidate cross-source match: Intacct vendor vs. NetSuite vendor
(accounts-payable/vendor). Until then, set `schedule_enabled: false` for
`sage-intacct-vendor` (already done in seed config) to prevent pipeline failures.

#### GAP-A6 — Governance lineage not emitted for Sage runs (OPEN)
**File:** `governance/lineage_record.py`  
**Impact:** The data catalog and lineage graph have no entries for Sage-sourced data. This
violates the platform's data governance contract (OWASP A09 — data lineage gap).  
**Remediation (Phase 6):** The extraction pipeline handler already calls lineage emission for
other sources. Verify that `SageConnector` satisfies the lineage contract interface; no
connector-specific code change should be needed.

---

### 2.4 Design Gaps

#### GAP-D1 — `is_active` mapped as raw string, not boolean (OPEN)
**File:** `config/field_mappings/sage/sage-intacct-customer/v1.json`  
**Impact:** Intacct returns `status = "active" | "inactive"`. The boolean cast in
`_cast_value` maps `"true"/"1"/"yes"` to `true` — `"active"` is not in that set.
The company survivorship policy expects `is_active` to be boolean for `first_non_null`
and `source_priority` resolution. The current workaround maps status to `customer_status`
(string) and drops `is_active` from the Sage mapping.  
**Remediation (Phase 6):** Add a `value_map` transformation type to `FieldMappingApplicator`
with a `mapping` dict in `transformation_params`:
```json
{
  "transformation": "value_map",
  "transformation_params": {
    "mapping": {"active": true, "inactive": false},
    "default": false
  }
}
```
Then update the Sage customer v1.json to produce `is_active: boolean`.

#### GAP-D2 — Dot-notation nested field handling not tested against live API (OPEN)
**Files:** `config/field_mappings/sage/sage-intacct-customer/v1.json`  
**Impact:** The Intacct REST API query service can return dot-notation fields either as flat
string keys (`"auditInfo.modifiedAt": "..."`) or as nested JSON objects depending on the
endpoint version and response mode. The field mapping assumes flat keys.  
**Remediation (Phase 6):** Run `--dry-run` against a real Intacct instance. If nested objects
are returned, add a `flatten_nested` pre-processing step in `IntacctMetadataClient` that
walks nested dicts and produces dot-notation flat keys before passing records to the
field mapping applicator.

#### GAP-D3 — `SageRawLayerWriter` `s3_prefix="sage"` is hardcoded in `_build_sage` (OPEN)
**File:** `sage_connector.py` → `_build_sage()`  
**Impact:** The `s3_prefix` is baked in as the string `"sage"` rather than being read from
the entity config's `target_raw_s3_prefix`. This prevents per-environment path customisation
and makes the actual write path diverge from the DynamoDB-stored prefix when they disagree.  
**Remediation (Phase 6):** Thread `entity_config.target_raw_s3_prefix` through the builder
so `SageRawLayerWriter` uses the config value instead of a hardcoded prefix.

---

### 2.5 Scalability Gaps

#### GAP-SC1 — Only `intacct` product supported (OPEN)
**File:** `common/sage_product_registry.py`  
**Impact:** Sage X3, Sage 100, Sage 200, Sage 300, Sage Accounting are listed as comments
in `SUPPORTED_SAGE_PRODUCTS` but are not implemented. The strategy pattern is designed for
extension; adding a product requires implementing three protocol classes.  
**Remediation (Phase 7+):** Implement per-product strategy triples:

| Product | Auth | Query | Priority |
|---------|------|-------|----------|
| Sage X3 | SOAP/REST hybrid | X3 query service | Phase 7 |
| Sage 100 | SQL Server ODBC | Direct SQL | Phase 7 |
| Sage 200 | REST (Sage 200 API) | OData v4 | Phase 8 |
| Sage Accounting | REST (Sage Accounting API) | REST GET | Phase 8 |

Each product only needs the three classes + a registry entry — `SageConnector` itself does
not change.

#### GAP-SC2 — Single entity per Step Functions execution (OPEN)
**Impact:** Each Sage object (customer, vendor, invoice, etc.) requires a separate Step
Functions execution. For Intacct environments with 20+ configured entities, the orchestration
layer must schedule 20 separate executions.  
**Remediation (Phase 7):** The platform Step Functions orchestration already handles this via
scheduled EventBridge rules per entity. No connector changes needed — this is a configuration
and schedule management concern.

#### GAP-SC3 — No Intacct AR Invoice or AP Bill entities (OPEN)
**Impact:** The most volumetric Intacct objects — AR invoices and AP bills — are not configured.
These are the records that give the data lake its transactional value.  
**Remediation (Phase 6):** Add entity configs and field mappings for:
- `sage-intacct-arinvoice` → `accounts-receivable/invoice` → entity type: `contract` (AR)
- `sage-intacct-apbill` → `accounts-payable/bill` → entity type: `contract` (AP)

---

### 2.6 Maintainability Gaps

#### GAP-M1 — No local test runner (FIXED ✅)
**File created:** `scripts/run_sage_connector_local.py`  
**Fix (applied):** Local runner with `--dry-run` mode (OAuth + schema + 5 records, no S3
write) mirrors the pattern of `run_salesforce_connector_local.py`.

#### GAP-M2 — Runbook lacks Sage trigger commands (OPEN)
**File:** `/memories/repo/pending-work-and-runbook.md`  
**Remediation (Phase 6):** Add Sage trigger commands to the runbook (see §4 below for the
commands). Update memory file once Step Functions execution is validated end-to-end.

#### GAP-M3 — No integration test for Sage extraction pipeline (OPEN)
**Impact:** Unit tests exist for all Sage components (9 test modules) but there is no
integration test that validates the full path: entity config → extraction → raw Parquet
→ transformation → curated Parquet.  
**Remediation (Phase 6):** Extend `connector_runtime/tests/sage/` with a mock-based
integration test that wires `SageConnector` through `ExtractionWorkflow` against stubbed
DynamoDB, S3, and Intacct API responses. Pattern: follow `test_extraction_pipeline_handler.py`.

#### GAP-M4 — `_FIELD_NAME_PATTERN` dot-notation support undocumented (OPEN)
**File:** `transformation/field_mapping/field_mapping_registry.py`  
**Impact:** `_FIELD_NAME_PATTERN` allows dots (`a-zA-Z0-9_.`) but this is not documented in
the module docstring. Developers adding new source-specific mappings may not know that
dot-notation field names are valid.  
**Remediation (Phase 6):** Add a comment in `FieldMappingRule.__post_init__` explaining that
dot-notation names (e.g. `auditInfo.modifiedAt`) require the source record to contain them
as flat string keys.

---

## 3. Implementation Phases

### Phase 5 (COMPLETE — 2026-07-01)
- [x] Sage connector layer (SageConnector + all Intacct strategies)
- [x] Unit tests (9 modules)
- [x] extraction_pipeline_handler import

### Phase 5.5 — Gaps Fixed This Session (2026-07-01)

| # | Gap | File(s) Changed |
|---|-----|-----------------|
| GAP-S1 | `SageMetadataError` correct error classification | `intacct_metadata_client.py`, `sage_connector.py` |
| GAP-S2 | `Retry-After` header surfaced in structured logs | `sage_http_client.py` |
| GAP-A1 | Entity extraction configs in DynamoDB seed script | `scripts/seed_entity_config.py` |
| GAP-A2 | Field mapping configs (customer + vendor) | `config/field_mappings/sage/` (new directory) |
| GAP-A3 | `sage-intacct-customer` wired into entity resolution | `entity_resolution_pipeline_handler.py` |
| GAP-A4 | Company survivorship includes Sage as 3rd source | `config/entity_resolution/company/survivorship_v1.json` |
| GAP-M1 | Local test runner script | `scripts/run_sage_connector_local.py` (new file) |

### Phase 6 — Remaining Gaps (Recommended Next Sprint)

**Priority 1 — Blockers / High Risk:**
1. **GAP-S3** — Terraform Secrets Manager placeholder for `{env}/sources/sage/intacct/credentials`
2. **GAP-S4** — Credential cache invalidation on `IntacctAuthError`
3. **GAP-D1** — `value_map` transformation type for `is_active` boolean mapping
4. **GAP-D2** — Validate dot-notation nested field handling against live Intacct API
5. **GAP-A5** — `sage-intacct-vendor` entity resolution (supplier entity type design)
6. **GAP-A6** — Governance lineage record emission for Sage runs

**Priority 2 — Performance / Scalability:**
7. **GAP-P1** — Configurable `page_size` in `connector_params`
8. **GAP-P2** — Lambda timeout circuit breaker for very large datasets
9. **GAP-P3** — `FieldContract` fingerprint cache via `SchemaSnapshotRepository`
10. **GAP-SC3** — `sage-intacct-arinvoice` and `sage-intacct-apbill` entities

**Priority 3 — Maintainability / Observability:**
11. **GAP-M2** — Runbook Sage trigger commands
12. **GAP-M3** — Integration test for full Sage pipeline
13. **GAP-M4** — Document dot-notation support in field mapping

**Priority 4 — Scalability (Phase 7+):**
14. **GAP-SC1** — Sage X3 product strategy implementation
15. **GAP-D3** — Thread `target_raw_s3_prefix` through `_build_sage` builder

---

## 4. Operational Commands — Sage Intacct

### Prerequisites (one-time setup per environment)
```bash
# 1. Create Secrets Manager secret (manual step until GAP-S3 is fixed)
AWS_PROFILE=dev aws secretsmanager create-secret \
  --name dev/sources/sage/intacct/credentials \
  --secret-string '{
    "base_url":      "https://api.intacct.com/ia/api/v1",
    "token_url":     "https://api.intacct.com/ia/api/v1/auth/token",
    "client_id":     "<your-client-id>",
    "client_secret": "<your-client-secret>",
    "company_id":    "<your-company-id>"
  }' \
  --region us-east-1

# 2. Seed entity configs to DynamoDB
AWS_PROFILE=dev python scripts/seed_entity_config.py --environment dev --region us-east-1

# 3. Upload field mappings to S3
AWS_PROFILE=dev python scripts/seed_field_mappings.py \
  --environment dev --region us-east-1 --source-id sage
```

### Local Connectivity Test (dry-run, no S3 write)
```bash
AWS_PROFILE=dev python scripts/run_sage_connector_local.py \
  --entity-id sage-intacct-customer --dry-run

AWS_PROFILE=dev python scripts/run_sage_connector_local.py \
  --entity-id sage-intacct-vendor --dry-run
```

### Trigger Extraction via Step Functions
```bash
# Sage Intacct Customer (incremental, contributes to company entity resolution)
AWS_PROFILE=dev python scripts/trigger_extraction.py \
  --source-id sage --entity-id sage-intacct-customer \
  --environment dev --region us-east-1 \
  --state-machine-arn arn:aws:states:us-east-1:087972550871:stateMachine:dev-extraction-pipeline \
  --param sage_product=intacct \
  --param object_path=accounts-receivable/customer

# Sage Intacct Vendor (incremental, no entity resolution yet)
AWS_PROFILE=dev python scripts/trigger_extraction.py \
  --source-id sage --entity-id sage-intacct-vendor \
  --environment dev --region us-east-1 \
  --state-machine-arn arn:aws:states:us-east-1:087972550871:stateMachine:dev-extraction-pipeline \
  --param sage_product=intacct \
  --param object_path=accounts-payable/vendor
```

### Upload Field Mappings After Config Change
```bash
AWS_PROFILE=dev python scripts/seed_field_mappings.py \
  --environment dev --region us-east-1 --source-id sage
```

---

## 5. Adding a New Sage Product (e.g. Sage X3)

1. Create `connector_runtime/adapters/sage/products/x3/` with:
   - `x3_auth.py` — implements `SageAuthProtocol` (X3 uses SOAP or REST depending on version)
   - `x3_metadata_client.py` — implements `SageMetadataProtocol`
   - `x3_query_engine.py` — implements `SageQueryProtocol`

2. In `sage_product_registry.py`:
   - Add `"x3"` to `SUPPORTED_SAGE_PRODUCTS`
   - Register the triple with `_register_product("x3", SageProductStrategies(...))`

3. In `sage_connector.py`:
   - Add `"x3": frozenset({...})` to `_PRODUCT_REQUIRED_CREDENTIAL_KEYS`

4. Create entity configs in `seed_entity_config.py` with `"sage_product": "x3"`

5. Create field mappings in `config/field_mappings/sage/sage-x3-{entity}/v1.json`

No changes to `SageConnector`, `ConnectorInterface`, or the extraction pipeline handler.

---

## 6. Architecture Decision Record — Sage `source_id`

**Decision:** All Sage products share a single `source_id = "sage"` (not `sage-intacct`,
`sage-x3`, etc.).

**Rationale:**
- The Strategy pattern already encodes product-specific logic inside the connector.
- Using a single source_id means the ConnectorRegistry has one entry for all Sage products,
  and `_build_sage` dispatches to the correct product based on `connector_params.sage_product`.
- Alternative (separate source_ids per product) would require separate registry entries,
  separate IAM policy statements, and separate Secrets Manager path prefixes — increasing
  infrastructure complexity for each new product without behavioural benefit.

**Consequence:** The S3 raw path includes `{sage_product}` as the second segment
(`sage/{product_name}/{entity_id}/`) to prevent cross-product collisions:
```
s3://dev-edl-raw-layer/sage/intacct/sage-intacct-customer/extraction_date=.../
s3://dev-edl-raw-layer/sage/x3/sage-x3-customer/extraction_date=.../
```
