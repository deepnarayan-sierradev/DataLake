# Cross-Repo Interface Contract

**Authoritative for both repositories. An identical copy lives at
`enterprise-platform/requirements/CROSS_REPO_INTERFACE_CONTRACT.md`. Changing one without the other
is a defect.**

**Purpose:** let the `DataLake` and `enterprise-platform` implementations proceed in **separate
sessions, separate VS Code instances, separate teams** without either needing to read the other's
source. Everything a session needs to know about the other side is in this document.

**Version:** 1.0 · **Last verified against source:** 2026-07-28
**Contract package:** `edl-shared-contracts >= 0.2.0`

---

## 0. Ownership rule

| Concern | Owner | The other side |
|---|---|---|
| **Tenants, users, roles, permissions** | **Identity API** (consumed by enterprise-platform) | DataLake **only validates a verified claim** — it never creates or administers any of them |
| Table schemas, key construction, S3 layouts, secret paths | **DataLake** | Consumes; never invents |
| Config *payload* shapes per capability | **DataLake** (contract package) | Validates against them |
| Config *authoring*, approval, publish lifecycle | **enterprise-platform** | Never writes config itself |
| Pipeline execution, runtime consumption of config | **DataLake** | Reads status only |
| Dashboards, reports, chat UI, Excel, applications | **enterprise-platform** | Provides data via API only |

**The DataLake never reads enterprise-platform code. The enterprise-platform never reads DataLake
code.** Both read this document and the published contract package.

**The DataLake is a standalone, configuration-driven processing system.** It has no
tenant-provisioning, user, role, or permission endpoint, and must never be given one: the
Identity API owns those, and a second source of truth for who exists is precisely the coupling
this split removes. A tenant's DataLake-side records — partition profile, scope units,
connections, entity configuration — are written **by the enterprise-platform** through the
shared contract package into DataLake-owned tables. DataLake owns the schema; the
enterprise-platform owns the authoring.

> **Correction, 2026-07-28.** §6 previously listed `POST /tenants` as an existing DataLake
> endpoint. That was wrong and the endpoint has been removed from the DataLake control plane.
> Per §10, **the enterprise-platform copy of this document owes the identical edit** — the two
> copies must remain byte-identical, and a session working in that repository should apply it.

---

## 1. Identifier rules — normative

Source of truth: `edl_shared_contracts.identifier_policy`. **Never re-derive these regexes.**

```
STABLE_ID_PATTERN      ^[a-z][a-z0-9\-]{1,63}$      source_id, entity_id, connection_id
ENTITY_TYPE_PATTERN    ^[a-z][a-z0-9_\-]{1,63}$     entity_type (underscores allowed: ar_invoice)
TENANT_CODE_PATTERN    ^[a-z][a-z0-9\-]{1,47}$      tenant_code
RUN_ID_PATTERN         same charset, up to 100 chars
DEFAULT_TENANT_CODE    "demo"
```

```python
tenant_scoped_key(tenant_code, key) -> f"{tenant_code}#{key}"
strip_tenant_prefix(tenant_code, scoped_key) -> plain key, or unchanged if unprefixed
```

**`tenant_code` is always prefixed, including `"demo"`.** There is no unprefixed legacy mode. Bare
integer `run_id`s are rejected — enumeration defence.

### The identifier trap

The Identity API's JWT carries **`tenant_id`** (its internal identifier). The DataLake understands
only **`tenant_code`** (human-readable, assigned at tenant creation). They are the same tenant under
two names in two systems.

**Resolve `tenant_id` → `tenant_code` before any DataLake write. Never write using `tenant_id`.**
The DataLake has a documented history of tenant-collision bugs from exactly this class of mistake.

---

## 2. DynamoDB tables the enterprise-platform touches

All `Edl`-prefixed PascalCase, no environment prefix — each environment is a separate AWS account.
Five carry `lifecycle { prevent_destroy = true }`; never create any by hand.

| Table | PK | SK | Notes |
|---|---|---|---|
| `EdlEntityExtractionConfig` | `source_id` = `tenant_scoped_key(tenant, source_id)` | `entity_id` | Plain `source_id` restored on read. **Changing under DL-12 — see §7** |
| `EdlWatermarkRepository` | `source_id` = tenant-scoped composite | `entity_id` | **Changing under DL-12** |
| `EdlRunAuditLog` | `run_id` | `stage` | GSI `source-entity-time-index` hash = `{tenant}#{source_id}#{entity_id}` |
| `EdlEntityTypeRegistry` | `tenant_code` | `sk` | `entity_type#{type}` items carry `pk_field`, `contributing_sources` |
| `EdlServingStoreConfig` | `tenant_code` | `entity_type` | Keyed by analytics entity_type, **not** source-level entity_id |
| `EdlSemanticModel` | `tenant_code` | `model_version` | `$latest` pointer item names the active version |
| `EdlSavedQuery` | `tenant_code` | `query_id` | |
| `EdlTwinIndex` | `tenant_code` | `sk` | Read-only from EP |
| `EdlSourceOnboardingRegistry` | `source_id` | — | Per-source, **not** per-tenant. No `prevent_destroy` |

### 2a. Declaring a REST entity the DataLake has never heard of (DL-CONN-21)

Added 2026-07-30, **additive** — no existing payload changes.

Salesforce, MySQL and NetSuite have always taken their entity from configuration
(`connector_params.object_name` / `table_name` / `record_type`). The spec-driven REST family did
not, so onboarding a REST entity meant a code change in DataLake. It no longer does.

For a REST source, write these into `EdlEntityExtractionConfig.connector_params`:

| Key | Required | Meaning |
|---|---|---|
| `entity_id` | yes | The entity, as always |
| `entity_path` | **only for an entity DataLake does not declare** | Endpoint path, e.g. `/api/v2/quotes` |
| `entity_records_json_path` | no | Dotted path to the record array, e.g. `Result.Items`. Empty string = the body *is* the array. Omit to inherit the source's convention |
| `entity_watermark_field` | no | Field carrying the modification timestamp |
| `entity_natural_key_field` | no | Defaults to `id` |
| `entity_pagination_strategy` | no | `offset_limit` \| `cursor` \| `keyset` \| `link_header` \| `page_number` \| `single_request` |
| `entity_record_unwrap_field` | no | For an envelope that nests each row, e.g. FHIR's `resource` |
| `entity_read_method` | no | `GET` (default) or `POST` |
| `page_size` | no | 1–1000; otherwise the source's default |

Rules the DataLake enforces, which the console should surface rather than fight:

- **A DataLake-declared entity always wins.** Sending `entity_path` for a declared entity is
  ignored, so configuration can never redirect a curated endpoint elsewhere.
- **The path is validated**: leading `/`, no traversal segment, no protocol-relative `//`, no
  query string or fragment, safe characters only. The source's host allowlist still applies at
  call time.
- **Write-back is not settable from configuration.** Enabling a read must never enable a source
  mutation; write-back stays spec-declared plus the entity's own `writeback_enabled` flag.
- An unknown `entity_id` with no `entity_path` fails as `DETERMINISTIC_INVALID_CONFIGURATION`
  with a message naming what to supply. It is not retried.

`extra="forbid"` still applies: any key not in the table above is rejected at validation.

---

## 3. S3 config layouts

```
Entity config (S3 backend)   {tenant_code}/{source_id}/{entity_id}/config.json
Field mappings               field-mappings/{source_id}/{entity_id}/           (curated bucket)
Entity resolution            {tenant_code}/entity-resolution/{entity_type}/match_rules_{v}.json
                             {tenant_code}/entity-resolution/{entity_type}/survivorship_{v}.json
                             {tenant_code}/entity-resolution/{entity_type}/latest.json   ← pointer
Relationship rules           {tenant_code}/relationship-rules/{entity_type}/{v}.json
                             {tenant_code}/relationship-rules/{entity_type}/latest.json
Semantic models              {tenant_code}/semantic-models/{version}.json       (planned, DL-SEM)
```

**Versioning rule — normative.** Publishes **always write a new version and repoint `latest.json`**.
Never overwrite an existing version in place. The DataLake runtime caches configs keyed by version
(`resolution_config_registry.py:138`), so an in-place overwrite is served stale from warm Lambda
containers indefinitely. `DL-CFG-05` makes this a contract test on the DataLake side.

---

## 4. Secrets Manager

```
Current    edl/sources/{source_id}/credentials
Under DL-12  edl/tenants/{tenant_code}/connections/{connection_id}/credentials
```

Read and write-back credentials are **separate secrets**. Values are **write-only** from the
enterprise-platform — never rendered, returned, or logged; only metadata, age, and validity.

---

## 5. EventBridge Scheduler

```
Schedule name   {tenant_code}--{source_id}--{entity_id}
                (double hyphen throughout — a single hyphen lets two tenants collide
                 when either field itself contains a hyphen)
Over 64 chars   truncated prefix + SHA-256 content hash, never a naive slice
Sync            create_or_update_schedule() is update-first; delete on disable
```

Called via `edl_shared_contracts.ExtractionScheduleClient`. **Downstream effects fire on publish
only, never on draft save** — this rule is normative for every capability, not just schedules.

---

## 6. HTTP endpoints the enterprise-platform consumes

Base: the DataLake control-plane API Gateway, Cognito/JWT authorizer.

```
Existing
  GET    /tenants/{tc}/entities            POST /tenants/{tc}/entities
  POST   /tenants/{tc}/pipelines/trigger
  GET    /tenants/{tc}/runs                GET  /tenants/{tc}/runs/{run_id}
  GET    /tenants/{tc}/twins/{entity_type}[/{golden_id}]
  POST   /tenants/{tc}/semantic/query
  GET/POST /tenants/{tc}/saved-queries     POST /tenants/{tc}/saved-queries/{id}/run

Planned — DL-11
  GET    /tenants/{tc}/config/effective[/{capability}/{key}]
  POST   /tenants/{tc}/config/{capability}/{key}/rollback
  POST   /tenants/{tc}/config/{capability}/{key}/reprocess
  GET    /tenants/{tc}/config/restatements

Planned — DL-SEM
  GET    /tenants/{tc}/semantic/model[/versions]
  GET    /tenants/{tc}/semantic/metrics/{name}/lineage
```

**The read path is already a proper service boundary.** Dashboards, reports, Excel, and exploration
go through `POST /semantic/query` — structured requests only, never SQL, never direct table access.

---

## 6a. Scope enforcement — which side applies the row filter

Normative, added 2026-07-28 after an audit found four consumption surfaces reading data with no
scope filter applied.

**This system applies the row filter. The enterprise-platform must not attempt to.** Every
read path here builds the predicate from the *verified authorizer claims* on the request —
`custom:scope_units` and `custom:scope_tenant_wide` — never from a request body, query string, or
header the caller controls. An empty grant is a `403`, never "no filter".

| Consumption surface | Enforced by | How |
|---|---|---|
| `semantic_query` | DataLake | `_scope_predicate_for(...)` on every `POST /tenants/{t}/semantic/query` |
| `drill_through` | DataLake | Same predicate, re-applied per query; a drill-through cannot widen |
| `twin_traversal` | DataLake | Twin list and get filter on the twin's `scope_unit_id`; a foreign twin is `404`, not `403` |
| `serving_store` | DataLake | Native row-level-security policies applied at provisioning |
| `export` | DataLake | Predicate required on `ExportService.execute` |
| `aggregate` | DataLake | Small-cohort suppression before rows are returned |
| **`scheduled_report`** | **DataLake, inherited** | The enterprise-platform's report scheduler calls the semantic query API and inherits its enforcement. EP must not read curated/analytics S3 or the serving store directly for reports. |
| **`excel_addin`** | **DataLake, inherited** | Same: the add-in is an API client. There is no separate data path for it. |
| `athena` | Infrastructure | Lake Formation `scope_unit` LF-Tag — the Python predicate never runs on a direct Athena query |

**What the enterprise-platform owes:** the scope units in the token must be the ones it granted,
and it must include `custom:scope_units` on every token issued for a partitioned tenant. A token
without that claim is treated as an empty grant and denied — which is the correct failure, but it
will read as "the API is broken" if EP omits the claim.

**What this system owes:** it never widens a claim, never reads scope from the request body, and
publishes `ScopePredicateApplied` so an inert control is visible. `ScopeAttributionApplied` and
`ScopePredicateApplied` both carry absence alarms (`*-inert`).

---

## 7. Breaking change in flight — DL-12

`DL-12` introduces `connection_id` as an identity dimension so one tenant can hold many connections
of the same connector type (10–12 franchisee CRMs under one portco).

| Layer | Before | After |
|---|---|---|
| Entity config PK | `tenant#source_id` | `tenant#connection_id` |
| Watermark PK | `tenant#source_id` | `tenant#connection_id` |
| Raw S3 | `{tenant}/{source}/{entity}` | `{tenant}/{source_id}/{connection_id}/{entity}` |
| Schedule name | `{tenant}--{source}--{entity}` | `{tenant}--{connection_id}--{entity}` |
| Secrets | `edl/sources/{source_id}/…` | `edl/tenants/{tenant}/connections/{connection_id}/…` |
| New tables | — | `EdlSourceConnection`, `EdlScopeUnit` |
| New column | — | `scope_unit_id` on every data layer |

**This breaks `entity_extraction_repository.py` and `schedule_repository.py` in the
enterprise-platform.** Existing single-connection sources migrate to `connection_id == source_id`,
so the change is non-destructive.

**Coordination protocol:** DataLake ships the contract package **minor version bump first**, with
both key forms supported. The enterprise-platform upgrades and switches. DataLake then removes the
old form in the following minor version. Neither side flips in a single step.

---

## 8. Contract package and versioning

**Current state: `edl_shared_contracts` v0.2.0 is VENDORED, not published.**
`datalake-config-service/vendor/edl_shared_contracts` with
`pip install -e ./vendor/edl_shared_contracts`, because the CodeArtifact package does not exist yet.

**A vendored snapshot is not independence — it is undetected drift.** Publishing it is the first
task of `EP-INF-05` / `EP-14` and is a precondition for the two sessions running safely in parallel.

Package contents: `identifier_policy`, `entity_configuration_contract`,
`configuration_repository`, `entity_type_registry`, `extraction_schedule_client`,
`serving_store_config_contract`, `serving_store_config_repository`, `observability_contract`,
`structured_logger`.

| Change | Version | Consumer action |
|---|---|---|
| New optional field | patch | none |
| New capability, new table | minor | opt in |
| Key schema, required field, removed field | **minor with both forms supported**, then minor to remove | upgrade before DataLake removes the old form |

Both CIs run the contract test suite (`DL-CFG-15`). A drift check fails the build when the consumed
version diverges from the deployed runtime's supported range (`DL-CFG-14` / `EP-CHG-05`).

---

## 9. What each session may assume about the other

**A DataLake session may assume:** the enterprise-platform writes config only through the shapes in
the contract package; publishes always bump versions; drafts never produce downstream effects; every
write is preceded by `tenant_id` → `tenant_code` resolution.

**An enterprise-platform session may assume:** table schemas, key rules, S3 layouts, and secret paths
are exactly as stated here; the runtime consumes published config at the next run boundary; the
semantic query API is the only supported read path for business data.

**Neither may assume** the other's unreleased work exists. Check the capability-discovery endpoint
(`EP-14`) rather than the other repository's source tree.

---

## 10. Escalation — when this contract must change

A change here is a **two-repo change**. Do not proceed unilaterally in a session:

1. Record the proposed change in both repos' `requirements/` with a rationale.
2. Bump the contract package minor version with both forms supported.
3. Land the consumer side.
4. Remove the old form in a following version.
5. Update **both** copies of this document in the same change.

If a session discovers this document disagrees with source, **the source wins and this document is a
defect** — fix it here, and say so in the session output rather than silently working around it.
