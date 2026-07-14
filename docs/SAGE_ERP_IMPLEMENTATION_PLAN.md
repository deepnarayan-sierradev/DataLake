# Sage ERP — Implementation Reference

> Platform: Enterprise Data Lake
> Scope: Sage Intacct + Sage X3 connector layer, entity resolution, and field mappings
> Last updated: 2026-07-14

---

## 1. Current State

Both Sage Intacct and Sage X3 connectors are fully implemented and unit-tested — auth, metadata
discovery, query engine, and raw-layer writing for both products (`connector_runtime/adapters/sage/`),
entity extraction configs for all six Sage entities (four Intacct, two X3, seeded in
`scripts/seed_entity_config.py`), field mappings, and entity-resolution wiring into the golden-record
types below. Neither product has live credentials populated in any environment yet — see
`docs/PLATFORM_STATUS.md` for current connection status. Sage 100, Sage 200, Sage 300, and Sage
Accounting remain commented-out placeholders in `SUPPORTED_SAGE_PRODUCTS` — genuinely not
implemented (see §2, Scalability).

**Entity coverage (`entity_resolution/entity_type_registry.py`):**

| Entity ID | Product | Entity type | Golden-record wiring |
|---|---|---|---|
| `sage-intacct-customer` | Intacct | `company` | `salesforce` → `netsuite` → `sage-intacct-customer` → `sage-x3-customer` |
| `sage-x3-customer` | X3 | `company` | same `company` survivorship policy |
| `sage-intacct-vendor` | Intacct | `supplier` | own match rules + survivorship (`config/entity_resolution/supplier/`) |
| `sage-x3-supplier` | X3 | `supplier` | same `supplier` survivorship policy |
| `sage-intacct-arinvoice` | Intacct | `ar_invoice` | own match rules + survivorship (`config/entity_resolution/ar_invoice/`) |
| `sage-intacct-apbill` | Intacct | `ap_bill` | own match rules + survivorship (`config/entity_resolution/ap_bill/`) |

`sage-x3-supplier` is seeded with `schedule_enabled: false` (no live X3 environment validated
against yet); the other five are schedule-enabled.

**Certification status:** `ConnectorCertificationChecklist` is generic infrastructure whose own
test suite exercises it against a fake connector, not against `SageConnector`. By inspection
`SageConnector` would pass every automated check, but no one has actually called
`checklist.certify()` against it and captured the report anywhere durable — treat that as "should
pass if run," not "certified."

---

## 2. Open Items

These are the Sage-specific gaps still open, roughly ordered by severity. Platform-wide gaps
(not specific to Sage) are tracked separately in `docs/KNOWN_GAPS_AND_ROADMAP.md`.

### Correctness

- **Credential cache doesn't invalidate on a rejected-credentials auth failure.**
  `SageCredentialManager` has a public `invalidate_cache()` method, but neither
  `IntacctAuthClient._refresh_token()` nor `X3AuthClient._refresh_token()` calls it when the token
  endpoint rejects the credentials. If Secrets Manager rotation fires mid-run, both products retry
  with a stale `client_secret` for up to the 3600s cache TTL. Fix: call
  `self._credentials.invalidate_cache()` in both `_refresh_token` methods before re-raising on a
  rejected-credentials response.
- **X3's `is_active` field mapping may reference the wrong field name — needs verification against
  a live instance.** Both `sage-x3-customer/v1.json` and `sage-x3-supplier/v1.json` map
  `IPTFLG_0` → `is_active` (cast to boolean), but `X3MetadataClient`'s curated static fallback
  schema for `BPCUSTOMER`/`BPSUPPLIER` lists `ENAFLG_0` ("Active Flag (1=active, 2=inactive)") —
  there is no `IPTFLG_0` in either static schema. If live `$top=1` sampling succeeds, whatever
  field names the real X3 API returns win (so `IPTFLG_0` may well be correct there); but if the
  endpoint is empty and the static fallback is used, `IPTFLG_0` is absent and `is_active` silently
  drops (`missing_field_behavior: "drop_field"`). Also, the static schema's `ENAFLG_0` is typed
  `"integer"` (1/2), not boolean, so a direct `cast: boolean` on it wouldn't work correctly either
  (2 is still truthy). This needs resolving against a real X3 instance before go-live, not just a
  naming fix.
- **`is_active` is mapped as a raw string, not a boolean, for Sage Intacct customer.**
  `config/field_mappings/sage/sage-intacct-customer/v1.json` — Intacct's
  `status = "active"|"inactive"` string isn't in the boolean-cast truthy set, so the mapping routes
  it to a separate `customer_status` string field and drops `is_active` from Sage Intacct customer
  records. No `value_map` transformation type exists yet in
  `FieldMappingApplicator`/`field_mapping_registry.py` to fix this properly.
- **Dot-notation nested field handling is untested against a live API.** It's still unverified
  against a real Intacct instance whether dot-notation fields (e.g. `auditInfo.modifiedAt`) arrive
  flat or nested. Relatedly, `field_mapping_registry.py`'s `_FIELD_NAME_PATTERN` allows dots in
  field names with no comment explaining why — worth documenting once the live behavior is
  confirmed.

### Performance

- **Page size is hardcoded, not configurable per entity.** `intacct_query_engine.py`
  (`PAGE_SIZE = 4_000`) and `x3_query_engine.py` (`X3_PAGE_SIZE = 1_000`) both use their
  platform's maximum as a correct default, but neither is exposed as a `connector_params` override.
- **No record-count circuit breaker for either product**, so a very large dataset can risk a Lambda
  timeout. More relevant now that the higher-volume Intacct AR invoice/AP bill entities exist.
- **Metadata caching is per-Lambda-instance only.** Neither `IntacctMetadataClient` nor
  `X3MetadataClient` persists its discovered schema to `schema_snapshot_repository` for
  cross-invocation reuse — both cache in-memory per instance only. X3's live `$top=1` sampling
  makes this marginally more expensive per cold start than Intacct's Models call, since it also
  executes the live sample query described above.
- **`SageHttpClient` uses default `requests.Session()` connection pool settings** for both
  products — never tuned.

### Scalability

- **Only Intacct and X3 are implemented.** Remaining Sage products:

  | Product | Auth | Query | Status |
  |---------|------|-------|--------|
  | Sage X3 | OAuth 2.0 client_credentials | OData v4 | Done |
  | Sage 100 | SQL Server ODBC | Direct SQL | Not started |
  | Sage 200 | REST (Sage 200 API) | OData v4 | Not started |
  | Sage Accounting | REST (Sage Accounting API) | REST GET | Not started |

  Each remaining product only needs the three strategy classes plus a registry entry — confirmed
  by the X3 addition, which touched none of `SageConnector`, `ConnectorInterface`, or the
  extraction pipeline handler. See §4 for the recipe.
- **Each Sage entity requires its own scheduled Step Functions execution** (one entity per
  execution, six entities today). This is a platform-wide convention (EventBridge Scheduler per
  entity), not a Sage-specific gap.

### Maintainability

- **`scripts/run_sage_connector_local.py`'s dry-run tool doesn't cover the AR invoice / AP bill
  entities** — its `_ENTITY_CONFIG` dict only knows about `sage-intacct-customer`,
  `sage-intacct-vendor`, `sage-x3-customer`, and `sage-x3-supplier`. Trigger those two via Step
  Functions directly instead (see §3).
- **No integration test wiring `SageConnector` through `ExtractionWorkflow` end-to-end**, for
  either product. Unit tests exist for all 11 Sage modules (8 shared/Intacct + 3 X3-specific), but
  nothing exercises the full pipeline.
- **The connector certification checklist has never actually been run against `SageConnector`**
  and had its report captured anywhere durable (see §1).

---

## 3. Operational Commands — Sage Intacct and Sage X3

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

## 4. Adding a New Sage Product (e.g. Sage 100)

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

## 5. Architecture Decision Record — Sage `source_id`

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
