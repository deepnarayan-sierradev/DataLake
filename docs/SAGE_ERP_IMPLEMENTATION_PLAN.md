# Sage ERP — Gap Analysis & Implementation Plan

> Platform: Enterprise Data Lake
> Scope: Sage Intacct + Sage X3 connector layer, entity resolution, and field mappings
> Author: Engineering Team
> Last verified against code: 2026-07-09

---

## 1. Current State

The Sage connector layer supports two products — **Intacct** and **X3** — through a single
`SageConnector` Strategy-pattern implementation. Both are code-complete and unit-tested; neither
has live credentials populated yet (see `docs/PLATFORM_STATUS.md` — all Sage secrets exist only
as empty Terraform-managed shells in dev).

| Component | Location | Status |
|-----------|----------|--------|
| `SageConnector` (Strategy pattern, dispatches Intacct JSON-POST vs. X3 OData GET) | `connector_runtime/adapters/sage/sage_connector.py` | ✅ Complete |
| `IntacctAuthClient` (OAuth 2.0 client_credentials) | `products/intacct/intacct_auth.py` | ✅ Complete |
| `IntacctMetadataClient` (Models endpoint) | `products/intacct/intacct_metadata_client.py` | ✅ Complete |
| `IntacctQueryEngine` (JSON DSL, start/size pagination) | `products/intacct/intacct_query_engine.py` | ✅ Complete |
| `X3AuthClient` (OAuth 2.0 client_credentials, folder-scoped) | `products/x3/x3_auth.py` | ✅ Complete |
| `X3MetadataClient` (live `$top=1` sampling + curated static fallback for BPCUSTOMER/BPSUPPLIER/SORDER/SINVOICE/PITM) | `products/x3/x3_metadata_client.py` | ✅ Complete |
| `X3QueryEngine` (OData v4 `$select`/`$filter`/`$orderby`, nextLink + `$skip` pagination) | `products/x3/x3_query_engine.py` | ✅ Complete |
| `SageHttpClient`, `SageCredentialManager`, `SageRawLayerWriter` | `common/` | ✅ Complete (shared by both products) |
| `SageProductRegistry` (`SUPPORTED_SAGE_PRODUCTS = {"intacct", "x3"}`) | `common/sage_product_registry.py` | ✅ Complete |
| Protocols for extension (`SageAuthProtocol`, etc.) | `protocols/` | ✅ Complete |
| Unit tests (11 modules: 8 shared/Intacct + 3 X3-specific) | `connector_runtime/tests/sage/` | ✅ Complete |
| Registered with `extraction_pipeline_handler.py` | Import line added | ✅ Complete |
| Formal connector certification run (`connector_runtime/certification/connector_certification_checklist.py`) | — | ⚠️ Not exercised — see §1.1 |

Sage 100, Sage 200, Sage 300, and Sage Accounting remain commented-out placeholders in
`SUPPORTED_SAGE_PRODUCTS` — genuinely not implemented.

**Entity coverage (`entity_resolution/entity_type_registry.py`):**

| Entity ID | Product | Entity type | Golden-record wiring |
|---|---|---|---|
| `sage-intacct-customer` | Intacct | `company` | ✅ `salesforce` → `netsuite` → `sage-intacct-customer` → `sage-x3-customer` |
| `sage-x3-customer` | X3 | `company` | ✅ same `company` survivorship policy |
| `sage-intacct-vendor` | Intacct | `supplier` (new type) | ✅ own match rules + survivorship (`config/entity_resolution/supplier/`) |
| `sage-x3-supplier` | X3 | `supplier` | ✅ same `supplier` survivorship policy |
| `sage-intacct-arinvoice` | Intacct | `ar_invoice` (new type) | ✅ own match rules + survivorship (`config/entity_resolution/ar_invoice/`) |
| `sage-intacct-apbill` | Intacct | `ap_bill` (new type) | ✅ own match rules + survivorship (`config/entity_resolution/ap_bill/`) |

All six entities are seeded in `scripts/seed_entity_config.py`. `sage-x3-supplier` is seeded with
`schedule_enabled: false` (no live X3 environment validated against yet); the other five are
schedule-enabled.

### 1.1 Certification status

`ConnectorCertificationChecklist` (source_id format, `ConnectorInterface` subclass check, required
methods implemented, no `os.environ` access, no banned identifiers in the class name) is generic
infrastructure — its own test suite (`connector_runtime/tests/test_connector_certification_checklist.py`)
exercises it against a fake connector, not against `SageConnector`. There is no persisted
certification report or test run that says "Sage Intacct passed" or "Sage X3 passed." By
inspection `SageConnector` would pass every automated check (source_id `"sage"` matches the
pattern, subclasses `ConnectorInterface`, implements all five abstract methods with real bodies,
never touches `os.environ`, and the class name contains none of the prohibited terms) — but treat
that as "should pass if run," not "certified," until someone actually calls `checklist.certify()`
against it and the report is captured somewhere durable.

---

## 2. Gap Analysis

### 2.1 Security Gaps

#### GAP-S1 — `SageMetadataError` classified as `UNKNOWN` (FIXED ✅)
**File:** `sage_connector.py` → `classify_extraction_error()`
Added `SageMetadataDeterministicError` / `SageMetadataTransientError` subclasses in
`intacct_metadata_client.py`; `classify_extraction_error` routes each independently. X3's metadata
client raises the same subclasses, so this fix already covers both products without change.

#### GAP-S2 — Retry-After header not surfaced (FIXED ✅)
**File:** `sage_http_client.py` → `_parse_response()`. HTTP 429 `Retry-After` is extracted and
logged as `sage_rate_limit_exceeded` for both products (shared client).

#### GAP-S3 — No Terraform placeholder for Sage Secrets Manager secret (FIXED ✅)
`infrastructure/modules/secrets/main.tf` defines both
`aws_secretsmanager_secret.sage_intacct_credentials` and `aws_secretsmanager_secret.sage_x3_credentials`,
each with a resource policy restricting `GetSecretValue` to the extraction runtime role. Both are
still empty shells — populating the actual credential value remains a manual step (see §4).

#### GAP-S4 — Credential cache TTL does not invalidate on auth failure (OPEN — confirmed still true)
**Files:** `products/intacct/intacct_auth.py::_refresh_token()`, `products/x3/x3_auth.py::_refresh_token()`
`SageCredentialManager` (via `SecretsManagerCredentialClient`) now has a public `invalidate_cache()`
method, but neither `IntacctAuthClient._refresh_token()` nor `X3AuthClient._refresh_token()` calls
it when the token endpoint rejects the credentials (`SageAuthenticationError` → `IntacctAuthError`/
`X3AuthError`). If Secrets Manager rotation fires mid-run, both products retry with a stale
`client_secret` for up to the 3600s cache TTL.
**Remediation:** In both `_refresh_token` methods, call `self._credentials.invalidate_cache()`
before re-raising on a rejected-credentials response.

---

### 2.2 Performance Gaps

#### GAP-P1 — Page size hardcoded, not configurable (OPEN — now applies to both products)
**Files:** `intacct_query_engine.py` (`PAGE_SIZE = 4_000`), `x3_query_engine.py` (`X3_PAGE_SIZE = 1_000`)
Both are Intacct's/X3's respective platform maximums and are correct defaults, but neither is
exposed as a per-entity `connector_params` override.

#### GAP-P2 — Lambda timeout risk for large datasets (OPEN — unchanged)
No record-count circuit breaker exists for either product. Still applies to high-volume Intacct
AR invoice/AP bill entities in particular now that those entities exist (see §2.3 GAP-SC3).

#### GAP-P3 — Metadata caching is per-Lambda-instance only (OPEN — unchanged)
Neither `IntacctMetadataClient` nor `X3MetadataClient` persists its discovered schema to
`schema_snapshot_repository` for cross-invocation reuse; both cache in-memory per instance only.
X3's live `$top=1` sampling makes this marginally more expensive per cold start than Intacct's
Models call, since it also executes the sample query described in GAP-D4 below.

#### GAP-P4 — `SageHttpClient` uses default connection pool settings (OPEN — unchanged)
`requests.Session()` defaults still apply; not overridden for either product.

---

### 2.3 Architecture Gaps

#### GAP-A1 — No entity extraction config in DynamoDB (FIXED ✅)
`scripts/seed_entity_config.py` now seeds all six entities listed in §1 (both Intacct's four and
X3's two), each with correct `connector_params`, watermark field, and raw S3 prefix.

#### GAP-A2 — No field mapping configs (FIXED ✅ — now covers both products)
- `config/field_mappings/sage/sage-intacct-customer/v1.json`
- `config/field_mappings/sage/sage-intacct-vendor/v1.json`
- `config/field_mappings/sage/sage-x3-customer/v1.json`
- `config/field_mappings/sage/sage-x3-supplier/v1.json`

X3's customer/supplier mappings both align their native PK (`BPCNUM_0`/`BPSNUM_0`) to
`account_id`/`vendor_id` respectively, matching the cross-source PK convention.

#### GAP-A3 — Sage not wired into entity resolution (FIXED ✅)
`sage-intacct-customer` and `sage-x3-customer` both resolve to the `company` entity type and
participate in `config/entity_resolution/company/survivorship_v1.json`.

#### GAP-A4 — Company survivorship ignored Sage Intacct (FIXED ✅)
`source_priority` lists in `config/entity_resolution/company/survivorship_v1.json` include `sage`
(covering both `sage-intacct-customer` and `sage-x3-customer`, since both map to source_id
`"sage"`) as a preferred source for `credit_limit`, `outstanding_balance`, `currency_code`, and
`is_active`.

#### GAP-A5 — `sage-intacct-vendor` not in entity resolution (FIXED ✅ — resolved since this doc was written)
A dedicated `supplier` entity type now exists (`entity_resolution/entity_type_registry.py`:
`ENTITY_TYPE_PK_FIELD["supplier"] = "vendor_id"`), with its own match rules and survivorship
policy at `config/entity_resolution/supplier/`. Both `sage-intacct-vendor` and `sage-x3-supplier`
feed it. `sage-x3-supplier` is currently `schedule_enabled: false` in the seed config, so it
extracts on manual trigger only, not on a schedule.

#### GAP-A6 — Governance lineage not emitted for Sage runs (FIXED / moot ✅)
Lineage emission (`governance/lineage_record.py`) is called generically from
`transformation/transformation_pipeline.py` and `entity_resolution/publishing_shared.py` with no
source-specific branching — every entity that flows through the standard transformation pipeline,
Sage included, gets a lineage record. This confirms the remediation note this doc originally made
("no connector-specific code change should be needed") was correct; no further work is needed here.

#### GAP-D3 (moved here from Design, since it's now resolved by an architecture change) — `s3_prefix` hardcoding (FIXED / moot ✅)
The original concern was that `_build_sage()` hardcoded `"sage"` as the S3 prefix rather than
reading `target_raw_s3_prefix` from entity config. That code path no longer exists:
`SageRawLayerWriter` now derives its path segment from the validated `sage_product`
(`path_segments=[f"sage-{sage_product}"]` in `common/sage_raw_layer_writer.py`), which is the same
value `scripts/seed_entity_config.py::_sage_raw_prefix()` uses to compute each entity's
`target_raw_s3_prefix`. The two can no longer diverge because both derive from the same
whitelisted `sage_product` value.

#### GAP-SC3 — No Intacct AR Invoice or AP Bill entities (FIXED ✅ — resolved since this doc was written)
`sage-intacct-arinvoice` (`ar_invoice` entity type) and `sage-intacct-apbill` (`ap_bill` entity
type) are both seeded in `scripts/seed_entity_config.py`, each with dedicated match rules and
survivorship policy under `config/entity_resolution/ar_invoice/` and `config/entity_resolution/ap_bill/`.
Caveat: `scripts/run_sage_connector_local.py`'s `_ENTITY_CONFIG` dict does not yet include these
two entities — the local dry-run tool only knows about `sage-intacct-customer`,
`sage-intacct-vendor`, `sage-x3-customer`, and `sage-x3-supplier`. Minor tooling gap, not a
pipeline gap.

---

### 2.4 Design Gaps

#### GAP-D1 — `is_active` mapped as raw string, not boolean, for Sage Intacct customer (OPEN — unchanged)
**File:** `config/field_mappings/sage/sage-intacct-customer/v1.json`
Still true as originally documented: Intacct's `status = "active"|"inactive"` string isn't in the
boolean-cast truthy set, so the mapping still routes it to a separate `customer_status` string
field and drops `is_active` from Sage Intacct customer records. No `value_map` transformation type
has been added to `FieldMappingApplicator`/`field_mapping_registry.py`.

#### GAP-D2 — Dot-notation nested field handling not tested against live API (OPEN — unchanged)
Still unverified against a real Intacct instance whether dot-notation fields
(`auditInfo.modifiedAt`) arrive flat or nested.

#### GAP-D4 — X3 `is_active` field mapping may reference the wrong field name (NEW — needs verification)
**Files:** `config/field_mappings/sage/sage-x3-customer/v1.json`, `sage-x3-supplier/v1.json`
Both map `IPTFLG_0` → `is_active` (cast to boolean). But `X3MetadataClient`'s curated static
fallback schema (`products/x3/x3_metadata_client.py::_X3_STATIC_SCHEMAS`) for both `BPCUSTOMER`
and `BPSUPPLIER` lists `ENAFLG_0` ("Active Flag (1=active, 2=inactive)") — there is no `IPTFLG_0`
in either static schema. If live `$top=1` sampling succeeds, whatever field names the real X3 API
returns win (so `IPTFLG_0` may well be correct there); but if the endpoint is empty and the static
fallback is used, `IPTFLG_0` is absent and `is_active` silently drops (`missing_field_behavior:
"drop_field"`). Also note the static schema's `ENAFLG_0` is typed `"integer"` (1/2), not boolean,
so a direct `cast: boolean` on it wouldn't work correctly either (2 is still truthy) — this needs
resolving against a real X3 instance before go-live, not just a naming fix.

---

### 2.5 Scalability Gaps

#### GAP-SC1 — Only `intacct` product supported (RESOLVED for X3 ✅ — Sage 100/200/300/Accounting still OPEN)
Sage X3 is fully implemented end-to-end (see §1). Remaining products:

| Product | Auth | Query | Priority |
|---------|------|-------|----------|
| Sage X3 | OAuth 2.0 client_credentials | OData v4 | ✅ Done |
| Sage 100 | SQL Server ODBC | Direct SQL | Not started |
| Sage 200 | REST (Sage 200 API) | OData v4 | Not started |
| Sage Accounting | REST (Sage Accounting API) | REST GET | Not started |

Each remaining product only needs the three strategy classes + a registry entry — confirmed by
the X3 addition, which touched none of `SageConnector`, `ConnectorInterface`, or the extraction
pipeline handler.

#### GAP-SC2 — Single entity per Step Functions execution (OPEN — unchanged, by design)
Each Sage entity (now 6, not 2) still requires its own scheduled execution. This is a platform
convention (EventBridge Scheduler per entity), not a Sage-specific gap.

---

### 2.6 Maintainability Gaps

#### GAP-M1 — No local test runner (FIXED ✅)
`scripts/run_sage_connector_local.py` supports all four schedule-enabled entities
(`sage-intacct-customer`, `sage-intacct-vendor`, `sage-x3-customer`, `sage-x3-supplier`) in
`--dry-run` mode. Does not yet cover `sage-intacct-arinvoice`/`sage-intacct-apbill` (see GAP-SC3
caveat above).

#### GAP-M2 — Runbook lacks Sage trigger commands (OPEN — unchanged, low confidence in current path)
The original remediation pointed at a memory file outside this repo; not something this doc can
verify. §4 below has trigger commands for both products; treat that as the actual runbook content
until a formal one exists.

#### GAP-M3 — No integration test for Sage extraction pipeline (OPEN — unchanged, now covers X3 too)
Unit tests exist for all 11 Sage modules (8 shared/Intacct + 3 X3), but there is still no
integration test wiring `SageConnector` through `ExtractionWorkflow` end-to-end for either product.

#### GAP-M4 — `_FIELD_NAME_PATTERN` dot-notation support undocumented (OPEN — unchanged)
`transformation/field_mapping/field_mapping_registry.py`'s `_FIELD_NAME_PATTERN` still allows dots
with no docstring or comment explaining why.

---

## 3. Implementation Phases

### Phase 5 (COMPLETE — 2026-07-01)
- [x] Sage connector layer (SageConnector + all Intacct strategies)
- [x] Unit tests (original 9 modules)
- [x] extraction_pipeline_handler import

### Phase 5.5 (COMPLETE — 2026-07-01)
Gaps fixed: GAP-S1, GAP-S2, GAP-A1, GAP-A2 (Intacct only at the time), GAP-A3, GAP-A4, GAP-M1.

### Phase 6 (COMPLETE — landed since 2026-07-01, exact date not tracked in this doc)
The batch that made this doc stale. Confirmed against current code:
- [x] **GAP-SC1** — Full Sage X3 product implementation (auth, metadata, query engine, tests,
  entity configs, field mappings)
- [x] **GAP-S3** — Terraform secret shells for both `sage/intacct` and `sage/x3`
- [x] **GAP-A5** — `supplier` entity type; `sage-intacct-vendor` and `sage-x3-supplier` both wired
  into entity resolution
- [x] **GAP-SC3** — `sage-intacct-arinvoice` (`ar_invoice`) and `sage-intacct-apbill` (`ap_bill`)
  entities, each with dedicated entity-resolution config
- [x] **GAP-A6** — confirmed moot (lineage emission was already generic)
- [x] **GAP-D3** — confirmed moot (superseded by product-derived path segments)

### Phase 7 — Remaining Gaps (recommended next)

**Priority 1 — Correctness / security:**
1. **GAP-D4** (new) — Verify `IPTFLG_0` vs. `ENAFLG_0` for X3 `is_active` against a live X3
   instance; fix the static fallback schema and/or field mapping, whichever is wrong
2. **GAP-S4** — Call `invalidate_cache()` on auth rejection in both `IntacctAuthClient` and
   `X3AuthClient`
3. **GAP-D1** — `value_map` transformation type for Intacct customer `is_active`
4. **GAP-D2** — Validate dot-notation nested field handling against a live Intacct API

**Priority 2 — Performance / scalability:**
5. **GAP-P1** — Configurable page size for both `intacct_query_engine.py` and `x3_query_engine.py`
6. **GAP-P2** — Lambda timeout circuit breaker (now more urgent given AR invoice/AP bill volume)
7. **GAP-P3** — `FieldContract` fingerprint cache via `SchemaSnapshotRepository`
8. **GAP-P4** — `SageHttpClient` connection pool tuning

**Priority 3 — Maintainability / observability:**
9. **GAP-M3** — Integration test covering both Intacct and X3 end-to-end
10. Add `sage-intacct-arinvoice`/`sage-intacct-apbill` to `run_sage_connector_local.py`'s
    `_ENTITY_CONFIG` (tooling gap noted under GAP-SC3)
11. **GAP-M4** — Document dot-notation support in field mapping
12. Actually run `ConnectorCertificationChecklist.certify()` against `SageConnector` for both
    products and persist the report somewhere durable (§1.1)

**Priority 4 — Scalability (future):**
13. Sage 100 / 200 / 300 / Accounting product implementations (GAP-SC1 remainder)

---

## 4. Operational Commands — Sage Intacct and Sage X3

### Prerequisites (one-time setup per environment)
```bash
# 1. Populate the Secrets Manager secret values. Terraform creates both secret
#    resources as empty shells (infrastructure/modules/secrets/main.tf) —
#    populating the actual credential values is a manual step for each product.
AWS_PROFILE=dev aws secretsmanager put-secret-value \
  --secret-id edl/sources/sage/intacct/credentials \
  --secret-string '{
    "base_url":      "https://api.intacct.com/ia/api/v1",
    "token_url":     "https://api.intacct.com/ia/api/v1/auth/token",
    "client_id":     "<your-client-id>",
    "client_secret": "<your-client-secret>",
    "company_id":    "<your-company-id>"
  }' \
  --region us-east-1

AWS_PROFILE=dev aws secretsmanager put-secret-value \
  --secret-id edl/sources/sage/x3/credentials \
  --secret-string '{
    "base_url":      "https://x3.yourcompany.com",
    "token_url":     "https://x3.yourcompany.com/auth/token",
    "client_id":     "<your-client-id>",
    "client_secret": "<your-client-secret>",
    "folder":        "<your-x3-folder, e.g. SEED or PROD>"
  }' \
  --region us-east-1

# 2. Seed entity configs to DynamoDB (all 6 Sage entities across both products)
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
AWS_PROFILE=dev python scripts/run_sage_connector_local.py \
  --entity-id sage-x3-customer --dry-run
AWS_PROFILE=dev python scripts/run_sage_connector_local.py \
  --entity-id sage-x3-supplier --dry-run
# sage-intacct-arinvoice / sage-intacct-apbill are not yet in this tool's
# _ENTITY_CONFIG — trigger those via Step Functions directly (below) instead.
```

### Trigger Extraction via Step Functions
```bash
# Sage Intacct Customer (incremental, contributes to company entity resolution)
AWS_PROFILE=dev python scripts/trigger_extraction.py \
  --source-id sage --entity-id sage-intacct-customer \
  --environment dev --region us-east-1 \
  --state-machine-arn arn:aws:states:us-east-1:087972550871:stateMachine:EdlExtractionPipeline \
  --param sage_product=intacct \
  --param object_path=accounts-receivable/customer

# Sage Intacct Vendor (incremental, contributes to supplier entity resolution)
AWS_PROFILE=dev python scripts/trigger_extraction.py \
  --source-id sage --entity-id sage-intacct-vendor \
  --environment dev --region us-east-1 \
  --state-machine-arn arn:aws:states:us-east-1:087972550871:stateMachine:EdlExtractionPipeline \
  --param sage_product=intacct \
  --param object_path=accounts-payable/vendor

# Sage Intacct AR Invoice
AWS_PROFILE=dev python scripts/trigger_extraction.py \
  --source-id sage --entity-id sage-intacct-arinvoice \
  --environment dev --region us-east-1 \
  --state-machine-arn arn:aws:states:us-east-1:087972550871:stateMachine:EdlExtractionPipeline \
  --param sage_product=intacct \
  --param object_path=accounts-receivable/invoice

# Sage Intacct AP Bill
AWS_PROFILE=dev python scripts/trigger_extraction.py \
  --source-id sage --entity-id sage-intacct-apbill \
  --environment dev --region us-east-1 \
  --state-machine-arn arn:aws:states:us-east-1:087972550871:stateMachine:EdlExtractionPipeline \
  --param sage_product=intacct \
  --param object_path=accounts-payable/bill

# Sage X3 Customer (incremental, contributes to company entity resolution)
AWS_PROFILE=dev python scripts/trigger_extraction.py \
  --source-id sage --entity-id sage-x3-customer \
  --environment dev --region us-east-1 \
  --state-machine-arn arn:aws:states:us-east-1:087972550871:stateMachine:EdlExtractionPipeline \
  --param sage_product=x3 \
  --param object_path=BPCUSTOMER

# Sage X3 Supplier (schedule_enabled: false in seed config — manual trigger only)
AWS_PROFILE=dev python scripts/trigger_extraction.py \
  --source-id sage --entity-id sage-x3-supplier \
  --environment dev --region us-east-1 \
  --state-machine-arn arn:aws:states:us-east-1:087972550871:stateMachine:EdlExtractionPipeline \
  --param sage_product=x3 \
  --param object_path=BPSUPPLIER
```

### Upload Field Mappings After Config Change
```bash
AWS_PROFILE=dev python scripts/seed_field_mappings.py \
  --environment dev --region us-east-1 --source-id sage
```

---

## 5. Adding a New Sage Product (e.g. Sage 100)

Sage X3 is the working reference implementation of this recipe — see
`connector_runtime/adapters/sage/products/x3/` (`x3_auth.py`, `x3_metadata_client.py`,
`x3_query_engine.py`), the `"x3"` entries in `sage_product_registry.py` and
`sage_connector.py`'s `_PRODUCT_REQUIRED_CREDENTIAL_KEYS`, and the `sage-x3-customer` /
`sage-x3-supplier` entity configs in `seed_entity_config.py`.

1. Create `connector_runtime/adapters/sage/products/100/` with:
   - `<product>_auth.py` — implements `SageAuthProtocol`
   - `<product>_metadata_client.py` — implements `SageMetadataProtocol`
   - `<product>_query_engine.py` — implements `SageQueryProtocol`

2. In `sage_product_registry.py`:
   - Add the product name to `SUPPORTED_SAGE_PRODUCTS`
   - Register the triple with `_register_product(<name>, SageProductStrategies(...))`

3. In `sage_connector.py`:
   - Add `<name>: frozenset({...})` to `_PRODUCT_REQUIRED_CREDENTIAL_KEYS`
   - If the product's query protocol isn't a JSON-POST body like Intacct's, add a discriminant
     check in `execute_extraction()` and a dedicated `_execute_<product>_extraction()` method,
     following the X3 OData GET pattern

4. Create entity configs in `seed_entity_config.py` with `"sage_product": <name>`

5. Create field mappings in `config/field_mappings/sage/sage-<name>-{entity}/v1.json`

6. If the new entity introduces a genuinely new entity type (not `company`/`supplier`/etc.),
   add match rules + survivorship policy under `config/entity_resolution/<entity_type>/` and
   register it in `entity_resolution/entity_type_registry.py` — see the `ar_invoice`/`ap_bill`
   addition for a template.

No changes to `SageConnector`'s constructor, `ConnectorInterface`, or the extraction pipeline
handler — confirmed in practice by the X3 addition, which touched none of these.

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

**Consequence:** The S3 raw path includes `{sage_product}` folded into a single hyphenated
segment (`sage-{sage_product}`) to prevent cross-product collisions:
```
s3://edl-raw-087972550871/{tenant_code}/sage-intacct/sage-intacct-customer/extraction_date=.../
s3://edl-raw-087972550871/{tenant_code}/sage-x3/sage-x3-customer/extraction_date=.../
```
