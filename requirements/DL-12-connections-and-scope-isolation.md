# DL-12 — Source Connections and Scope-Unit Data Isolation

**SOW clauses:** §3.1, §3.2, §8, §14, §23.4 · **Priority:** P0 · **Owner repo:** DataLake

---

## Objective

Two coupled capabilities that one change unlocks:

1. **Many connections of the same connector type within one tenant** — a portco whose 10–12
   franchisees each run their own CRM instance, or a non-franchise tenant with two Salesforce orgs
   after an acquisition.
2. **Sub-tenant data isolation** — one franchisee cannot see another's data, while tenant-level
   roles see everything in the tenant. Generalised so it applies to any tenant, franchise or not.

## Why one change unlocks both

A **source connection** carries the identity of who owns it. When franchisee data arrives through
franchisee-owned connections, the owning scope unit is **provenance-derived** — established by which
connection extracted the row, not by trusting a field in source data. That is both the more accurate
attribution and the cheaper one.

---

## Current state (verified 2026-07-28, against source)

**There is no source-instance dimension anywhere.** Every identity in the platform is
`(tenant_code, source_id, entity_id)`:

| Layer | Key | Evidence |
|---|---|---|
| Entity config | PK `tenant_scoped_key(tenant, source_id)`, SK `entity_id` | `configuration_repository.py:9-12` |
| Watermarks | same composite | `WatermarkRepository` |
| Raw S3 | `{tenant_code}/{source}/{entity_id}/extraction_date=…/run_id=…` | `raw_layer_writer.py:16-19` |
| Schedules | `{tenant_code}--{source_id}--{entity_id}` | `extraction_schedule_client.py:183` |
| Curated Glue table | `{tenant_code}_{entity_id}_{domain}_curated` | `transformation_pipeline.py` |

Two franchisees under one tenant both on HubSpot therefore collide:

- **Entity config** — both target PK `evive#hubspot`, SK `hubspot-company`. The
  `attribute_not_exists(source_id) AND attribute_not_exists(entity_id)` condition
  (`configuration_repository.py:177`) rejects the second. Fails closed, but **blocks onboarding**.
- **Watermarks** — one shared watermark across two franchisees' data; incremental state corrupts.
- **Raw S3** — both write to `evive/hubspot/hubspot-company/`. Distinct `run_id` prevents overwrite,
  but the two franchisees' records interleave under one prefix **with attribution unrecoverable**.
- **Schedules** — `create_or_update_schedule()` is update-first, so one franchisee's schedule
  **silently clobbers** the other's.

**Entity resolution merges across all contributing sources within a tenant.**
`EntityTypeRegistryClient.get_contributing_sources(entity_type)` returns the `(source_id, entity_id)`
pairs feeding an entity type (`entity_type_registry.py:79-81`) and the resolution handler scans all
of them (`entity_resolution_pipeline_handler.py:229`). Output golden records carry
`contributing_source_records` (`canonical_record_publisher.py:209`) and a queryable
`field_provenance` map (`survivorship_policy.py:88-89`).

So one customer present in two franchisees' CRMs produces **one golden record containing both
franchisees' data**, with provenance naming both. **No row-level filter can repair this** — the
record *is* the merge, and `field_provenance` is queryable directly in Athena via
`json_extract_scalar`.

**No scope dimension exists below tenant.** `tenant_code` is the only isolation key in the data
model. `DL-SEC-11` specifies franchise row-level security as an enforcement predicate, but there is
no attribute for it to filter on and no guarantee the underlying record belongs to one scope unit.

---

## Core design decisions

### D1 — Generalise the dimension; never make enforcement conditional

Not every tenant is a franchise business. The wrong way to honour that is a boolean that switches
enforcement on or off — the safety of every franchise tenant would then rest on one flag being
correct at onboarding, in a migration, and in every Terraform default. A wrong flag produces open
access with no error anywhere.

Instead the tenant declares a **partition model**, and the predicate is always applied:

| `partition_model` | Scope units | Predicate |
|---|---|---|
| `single` | exactly one implicit unit (`__tenant__`) | Applied; matches everything. Degenerate by construction |
| `partitioned` | many, with a declared `partition_kind` | Applied; filters |

`partition_kind` ∈ `franchise` \| `region` \| `subsidiary` \| `legal_entity` \| `business_unit`. The
kind is a **label** driving terminology in the console; the mechanism is identical. This matters
beyond franchising: SOW §14 commits to integrating acquisitions, and an acquired business is a
natural scope unit for a tenant that is not a franchise operation at all.

**One code path, one test surface, and no configuration in which enforcement is skipped.**

### D2 — Attribution has two modes, and unattributable is not "public"

| Mode | Applies to | Trust |
|---|---|---|
| **Provenance-derived** | Connections owned by a scope unit (franchisee's own CRM) | High — established by which connection extracted the row |
| **Field-derived** | Tenant-owned connections (portco Sage Intacct, portco HubSpot) | Depends on a mapped column; may be null or wrong |

A row that cannot be attributed gets `scope_unit_id = null`, which resolves to **tenant-level
visibility — visible only to tenant-scoped roles, never to all scope units**. Fail closed. An
unattributable-row rate above a configured threshold is a quality violation (DL-DQ-14), not a
silent condition.

### D3 — Resolution scope is configuration, with defaults by partition model

| Partition model | Default resolution scope | Rationale |
|---|---|---|
| `single` | `tenant` | Normal consolidation — merging across sources is the point of the platform |
| `partitioned` | `scope_unit` | Separate businesses; merging leaks |

Overridable per entity type, because the correct answer differs by entity class even within one
tenant. Working assumption, to be confirmed per tenant with the business owner:

| Entity class | Scope | Reasoning |
|---|---|---|
| Customers, contacts, leads, opportunities, jobs, calls | `scope_unit` | Franchisees are separate businesses; a shared customer view would leak commercial data |
| Vendors, chart of accounts, brands, products, franchise master | `tenant` | Shared reference data; merging is intended |
| AR invoices, AP bills, payments | `scope_unit` where franchisee-billed, `tenant` where portco-billed | Follows who owns the receivable |

**Open question for the customer, flagged not blocking:** if a portco genuinely wants one unified
customer view across franchisees, that requires a per-scope-unit *projection* over a tenant-scoped
golden record — materially more complex, and it changes the design. Specified as out of scope below
until confirmed.

---

## Functional requirements

### Connection model

- **DL-SCOPE-03** **Source connection as a first-class entity.** `EdlSourceConnection` holds
  `connection_id`, `source_id`, `owner_type` (`tenant` \| `scope_unit`), `owner_id`,
  `credential_path`, capability overrides, and lifecycle state. A connection is an instance of a
  connector bound to credentials and an owner.
- **DL-SCOPE-04** **`connection_id` replaces `source_id` in every composite key**:

  | Layer | New form |
  |---|---|
  | Entity config PK | `tenant_scoped_key(tenant_code, connection_id)`, SK `entity_id` |
  | Watermark PK | same composite |
  | Raw S3 | `{tenant_code}/{source_id}/{connection_id}/{entity_id}/extraction_date=…/run_id=…` |
  | Curated / analytics | `{tenant_code}/…/{connection_id}/…` where per-connection separation is required |
  | Schedule name | `{tenant_code}--{connection_id}--{entity_id}` |
  | Credentials | `edl/tenants/{tenant_code}/connections/{connection_id}/credentials` |
  | Glue curated table | `{tenant_code}_{connection_id}_{entity_id}_{domain}_curated` |

  `source_id` is retained as an attribute for browsing, routing to the adapter, and catalog display
  — it stops being an identity component. The existing `_build_schedule_name()` hash-collapse for
  names over 64 characters applies unchanged and becomes more load-bearing.

- **DL-SCOPE-05** **Migration.** DynamoDB partition-key changes require a new table plus a
  backfill. Follow the pattern already proven by
  `scripts/migrate_entity_config_to_tenant_scoped_key.py`: dry-run default, `--apply` to execute,
  run **before** the new code deploys to each environment. Existing single-connection sources
  migrate to a default `connection_id` equal to their `source_id`, so the change is
  non-destructive and reversible.
- **DL-SCOPE-06** **Per-connection credentials.** Supersedes `DL-SEC-05`'s per-tenant path — the
  correct grain is per connection, since twelve franchisees on HubSpot need twelve credential sets
  for one `source_id`. Read and write-back credentials remain separate secrets (`DL-CONN-02`).
- **DL-SCOPE-07** **Per-connection rate limiting and quotas.** Twelve HubSpot connections hit twelve
  independent tenant quotas at the provider, so `DL-CONN-11`'s `RateLimitPolicy` binds to the
  connection, not the source type. A shared-quota provider declares that in its capability set.
- **DL-SCOPE-08** **Connection health and lifecycle.** Connections have states — `pending`,
  `active`, `failing`, `suspended`, `retired` — with credential validity checks, last-successful-run
  tracking, and a retirement path that stops schedules and retains data without deleting it. With
  four brands mid-vendor-switch, retirement is a near-term operational need, not a future one.

### Scope model

- **DL-SCOPE-01** **Scope unit as a first-class dimension.** `EdlScopeUnit` per tenant:
  `scope_unit_id`, `partition_kind`, display name, external reference (franchisee number, legal
  entity code), parent for hierarchy, and effective date range.
- **DL-SCOPE-02** **Tenant partition profile.** `partition_model` and `partition_kind` on the tenant
  record. `single` tenants get exactly one implicit unit. Changing `single` → `partitioned` is a
  governed migration re-attributing existing rows; `partitioned` → `single` is a **widening**
  operation that exposes data across units and requires approval plus an audit record.
- **DL-SCOPE-09** **Stamp `scope_unit_id` at ingestion**, from the connection owner for
  scope-unit-owned connections, or from a declared mapping for tenant-owned connections. The
  attribute flows unchanged through raw → curated → golden → analytics → twin → serving. It is never
  recomputed downstream, so there is exactly one place attribution can be wrong.
- **DL-SCOPE-10** **Scope hierarchy.** A multi-unit owner holding several franchises, or a region
  containing subsidiaries, resolves to a set of leaf units. Grants are expressed against nodes and
  expand to leaves at claim-issuance time, not at query time.
- **DL-SCOPE-11** **Time-bounded scope.** Units and grants carry effective dates so a transfer,
  closure, or sale is representable. Whether a new owner inherits the prior owner's history is a
  **configurable policy, defaulting to no** — it is a contractual question, not a technical one, and
  the platform must not decide it silently.

### Isolation enforcement

- **DL-SCOPE-12** **Resolution scope per entity type** per D3, with defaults by partition model.
  Scope-unit resolution adds `scope_unit_id` to the blocking key so records from different units can
  never cluster, and every resulting golden record carries exactly one `scope_unit_id`. Changing an
  entity type's resolution scope is **reprocess-eligible** under `DL-CFG-10` and requires
  re-resolution of history — the mechanism already exists in `DL-CFG-11`.
- **DL-SCOPE-13** **Twin edges respect the boundary.** **Implemented 2026-07-29.** `Twin` and
  `TwinEdge` carry `scope_unit_id`; the twin builder takes the node's unit from the golden record's
  stamped column and the edge's unit from `to_scope_unit_id`, which the relationship resolver now
  selects from the target side of the join. The API filters nodes by direct attribute access — not
  `getattr(..., None)`, so a missing field is a type error — returns 404 rather than 403 for a
  foreign unit's twin, and filters edges by the target's owning unit, reporting the suppressed
  count as `edges_hidden_by_scope`.

  **Correction to the 2026-07-28 record, which claimed this was partially done.** The API filter
  was described as working with only edge fan-out open. In fact no part of it worked: the model
  carried no `scope_unit_id` at all, so the node filter always evaluated `matches(None)` —
  match-all for a `single` tenant, deny-all for a partitioned one. Two mechanisms now prevent a
  repeat: gate **G7** (`scripts/check_security_column_writers.py`) fails when a filtered column has
  no declaration or no writer, and `connector_runtime/tests/test_twin_scope_isolation.py` drives
  both routes with two sibling franchisees instead of the single-partition `demo` tenant whose
  claim contains `__tenant__` and therefore cannot fail a scope check.

  Twins written before this change carry no unit and stay invisible to a unit-scoped caller
  (fails closed); the entity's next twin build restamps them, so no backfill script is needed.
  The edge query now requires `scope_unit_id` on analytics datasets, which is governed by the
  pending scope-attribution backfill decision. An edge between two scope-unit-scoped entities
  is permitted only within one unit. Edges to tenant-scoped entities (a shared vendor) are permitted
  from any unit, but traversal from a tenant-scoped node must not enumerate other units' entities —
  the fan-out itself discloses existence.
- **DL-SCOPE-14** **Single predicate builder.** One server-side `scope_predicate(claims)` used by the
  semantic compiler, serving-store view generation, exports, reports, and the twin API. Never
  caller-supplied, applied before every other filter. **An empty scope set means no access, never
  unrestricted access** — tenant-wide access is an explicit affirmative grant, never an absent or
  empty claim. This is the single most likely implementation defect in this document and warrants a
  dedicated test.
- **DL-SCOPE-15** **Serving-store isolation by engine capability.** MySQL has **no native row-level
  security**, only views — its current model is database-per-tenant, which provides nothing *within*
  a tenant. For `partitioned` tenants: PostgreSQL, SQL Server, and Redshift use native RLS policies;
  MySQL requires schema-per-scope-unit or is declared unsuitable. `single` tenants are unaffected and
  keep the existing model. This is an engine-selection constraint on `DL-SERV-01`, not an
  implementation detail.
- **DL-SCOPE-16** **Aggregate and benchmark protection.** Peer comparison is a normal franchise
  reporting feature that computes over data the viewer cannot see. Enforce a minimum cohort size,
  suppress small cells, and reject rank-plus-average combinations that permit back-computation of an
  individual unit's figures. Without this, a benchmark widget is a data-exfiltration path with a
  friendly UI.
- **DL-SCOPE-17** **Uniform enforcement across every consumption surface** — semantic query, Athena,
  serving store direct connections, exports, scheduled reports, Excel add-in, twin traversal,
  drill-through, and aggregates. A surface that bypasses the predicate builder is a defect.
- **DL-SCOPE-18** **Adversarial isolation tests.** Extend `tests/test_tenant_isolation.py` with a
  scope dimension and test it **as an attacker**, not as a filter: attempt cross-unit reach via each
  surface in `DL-SCOPE-17`, via a crafted claim, via an empty scope set, via drill-through, via a
  merged golden record, via `field_provenance` in Athena, and via a small-cohort aggregate. Include
  a `single`-tenant case proving the degenerate predicate matches everything.

---

## Data model

| Store | Key | Purpose |
|---|---|---|
| `EdlSourceConnection` (new) | PK `tenant_code`, SK `connection_id` | source, owner, credential path, capabilities, health state |
| `EdlScopeUnit` (new) | PK `tenant_code`, SK `scope_unit_id` | kind, display name, external ref, parent, effective dates |
| Tenant record | — | `partition_model`, `partition_kind` |
| `EdlEntityExtractionConfig` | PK → `tenant#connection_id` | **key change, requires migration** |
| `EdlWatermarkRepository` | PK → `tenant#connection_id` | **key change, requires migration** |
| All data layers | — | `scope_unit_id` column, nullable = tenant-level |
| `EdlEntityTypeRegistry` | — | `resolution_scope` per entity type |

## Design and patterns

- **Provenance over declaration** — attribution derived from the extracting connection, not from
  trusting source data.
- **Degenerate rather than absent** — the non-franchise case is one scope unit, not a disabled
  feature. This is the central security decision in this document.
- **Single enforcement point** — one predicate builder, consumed everywhere; matches how
  `tenant_code` is already injected server-side.
- **Policy as data** — partition model, resolution scope, attribution mapping, and inheritance
  policy are all validated configuration, not conditionals in handlers.
- **Defence in depth** — application predicate *and* database RLS or schema separation *and*
  (per `DL-SEC-01`) IAM conditions. Any one failing must not expose data.
- Deliberately **not**: a boolean franchise flag (D1); scope encoded in `entity_id` naming (identity
  by string parsing is what `identifier_policy.py` exists to prevent); a separate AWS account per
  scope unit (operationally untenable at 12 units per tenant).

## Performance

- `scope_unit_id` is a partition column on curated and analytics where cardinality permits, so the
  security predicate **improves** query performance through partition pruning rather than degrading
  it.
- Scope-unit-scoped resolution is cheaper than tenant-scoped: the blocking key is narrower, so
  candidate sets shrink and matching cost falls.
- Twelve connections per tenant means twelve times the schedules, watermarks, and runs — schedule
  jitter (`DL-OPS-11`, gap 15) stops being a future concern and becomes immediately necessary.
- Per-connection concurrency caps prevent one franchisee's large backfill starving the others.
- Grant expansion (hierarchy → leaf units) happens at claim issuance and is cached, not recomputed
  per query.

## Security and OWASP

- **A01 Broken access control** — the whole document. Empty scope set means deny (`DL-SCOPE-14`);
  tenant-wide is an affirmative grant; the predicate is server-injected from verified claims and
  applied before every other filter.
- **A02** — one credential set per connection, isolated per scope unit; a franchisee's CRM
  credentials are never readable by another unit's operators.
- **A03** — scope predicates are compiler-generated with allowlisted identifiers, never
  caller-supplied SQL.
- **A04 Insecure design** — three design-level controls: degenerate-not-conditional enforcement,
  scope-partitioned entity resolution (a filter cannot repair a merged record), and k-anonymity on
  aggregates.
- **A05** — a `partitioned` tenant with an entity type lacking an attribution policy fails closed,
  not open. Unattributable rows are tenant-level, never universal.
- **A09** — every scope grant, expansion, cross-unit denial, and connection credential access is
  audited. `CrossScopeAccessAttempts` is a paging alarm.
- **Existence disclosure** is a distinct risk here and is easy to miss: a denied query, an empty
  drill-through, a twin traversal fan-out, or a suppressed small cell must not reveal that another
  unit's data exists.

## Observability

`CrossScopeAccessAttempts`, `ScopePredicateApplied{surface}`, `UnattributedRowRate{entity}`,
`ScopeGrantExpansions`, `EmptyScopeDenials`, `ConnectionHealth{connection_id}`,
`ConnectionCredentialFailures`, `ConnectionsPerTenant`, `ResolutionScopeViolations`,
`AggregateSuppressions`, `BenchmarkCohortSize` — all alarmed.

`CrossScopeAccessAttempts` and `ResolutionScopeViolations` page. `UnattributedRowRate` trending up
means a mapping has broken and rows are silently becoming tenant-level.

## Reuse and redundancy

- One predicate builder for seven consumption surfaces.
- One connection model serving both franchise multi-CRM and non-franchise multi-org needs — this is
  why it is not named "franchise connection".
- Reuses the existing tenant-key migration pattern, the `_build_schedule_name()` hash-collapse, the
  `DL-CFG-11` reprocessing engine for resolution-scope changes, and the `DL-DQ-14` exception store
  for unattributable rows.
- `tests/test_tenant_isolation.py` stays the single cross-cutting isolation test; scope becomes a
  dimension within it, not a parallel file.

## Acceptance criteria

1. Twelve franchisees on the same connector type onboard under one tenant with no key collision, no
   schedule clobber, and no data interleaving.
2. A franchise user cannot reach another unit's data via semantic query, Athena, direct serving-store
   connection, export, scheduled report, Excel add-in, twin traversal, drill-through, or aggregate —
   each proven independently and adversarially.
3. A tenant-admin role sees all units' data in one view.
4. A `single`-tenant workload behaves identically to today, with the predicate applied and matching
   everything — proven by test, not by inspection.
5. An empty scope claim denies all access.
6. Two units' identical customer produces **two** golden records under scope-unit resolution, each
   with one `scope_unit_id` and provenance confined to its own unit.
7. A peer benchmark below the minimum cohort size is suppressed and cannot be back-computed.
8. A `single` → `partitioned` migration re-attributes existing rows correctly; the reverse requires
   approval and is audited.
9. A franchise transfer applies the configured history-inheritance policy, defaulting to no
   inheritance.
10. Migration scripts run dry-run-first and are reversible.

## Dependencies and sequencing

**Must precede the connector wave (Phase 2).** Ten connectors written against a connection-aware
model cost almost nothing extra; retrofitting fourteen later is expensive. The same timing argument
applies more strongly than it did for config pinning:

| Factor | Now | After production |
|---|---|---|
| DynamoDB PK change | New table, migrate 36k dev rows | New table, migrate live multi-tenant data |
| S3 path change | Re-extract two sources | Re-extract or dual-read every partition |
| Resolution scope | No golden records to unwind | Re-resolve all history, restating downstream figures |
| Connectors touched | 4 | 14 |

Related: `DL-SEC-01` (IAM conditions should account for scope), `DL-SEC-11` (superseded in grain by
`DL-SCOPE-14`), `DL-SERV-01` (engine constraint), `DL-CFG-10/11` (reprocessing), `EP-13` (the
administration and connection-management surface).

## Out of scope

- **Cross-unit unified customer view.** A per-scope-unit projection over a tenant-scoped golden
  record is materially more complex and is deferred pending the customer's answer on whether a
  portco wants one customer view across franchisees.
- Scope-unit-level AWS account separation.
- Cross-tenant scope units — a scope unit belongs to exactly one tenant.
