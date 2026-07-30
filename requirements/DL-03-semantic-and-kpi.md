# DL-03 — Semantic Model and Enterprise KPI Definitions

**SOW clauses:** §4, §5.3, §18 · **Priority:** P0 · **Owner repo:** DataLake

---

## Objective

Author the actual enterprise semantic content — entities, dimensions, measures, metrics, joins,
ownership — so that every dashboard, report, chat answer, and reconciliation computes the same
number the same way. The engine exists; the business model does not.

## Current state (verified 2026-07-28)

`semantic/` contains a complete, tested engine: `SemanticModel` / `SemanticEntity` / `Dimension` /
`Metric` (`semantic_model.py`), a `QueryCompiler` that emits parameterised SQL with per-field access
tags and never accepts raw SQL (`query_compiler.py`), `SavedQuery` + repository,
`SemanticModelRepository` over `datalake-semantic-model-dev`, and `SemanticQueryService`. Control-plane routes
exist for `POST /tenants/{tc}/semantic/query` and saved-query CRUD.

**Zero business content is authored.** None of the SOW §4 named definitions exist: Sales, Revenue,
Collected Revenue, Royalties, Leads, Opportunities, Conversions, Customer Acquisition, Franchise
Performance, Operational KPIs, Marketing performance.

Engine gaps that block authoring real content:

- Saved-query **filters are deferred** (metrics + dimensions only) — noted in
  `docs/PLATFORM_EVOLUTION_PROGRESS.md`.
- No **join declaration** between semantic entities (FR-2.2 unimplemented) — the compiler resolves a
  single entity at a time.
- No **time-grain** handling, so period-over-period metrics cannot be expressed.
- No **metric lineage** (FR-2.6) and no **maker-checker publish** (FR-2.7).
- No **data-ownership** attribute on entities or metrics (§4 requires it).

---

## Functional requirements

### Engine completion

- **DL-SEM-01** **Joins.** Declare typed joins between semantic entities, aligned with the twin
  layer's resolved edges, so a request may span entities without the caller expressing a physical
  join. Join paths are validated at publish; ambiguous paths are rejected, not guessed.
- **DL-SEM-02** **Time dimension and grain.** A first-class time dimension per entity with declared
  grain (`day`/`week`/`month`/`quarter`/`year`), fiscal-calendar support, and derived comparison
  operators (prior period, prior year, period-to-date). Fiscal calendar is tenant configuration, not
  a constant — franchise finance calendars differ from the Gregorian year.
- **DL-SEM-07** **Filters on saved queries and requests**, including relative date ranges, `IN`
  lists, and null handling. Values parameterised; identifiers allowlisted from the model.
  *Closed 2026-07-29.* `connector_runtime/api/models.py::SemanticQueryShape` is the request surface
  and `semantic/saved_query.py::SavedQuery` persists it; both carry filters, joined dimensions,
  fiscal grain, period comparison, time range, and row limit. Until then the compiler could express
  a filter and neither the API nor a saved query could carry one, so the requirement was recorded as
  met on the strength of half of it — see the DL-SEM-07 note in `requirements/WAIVERS.md`.
- **DL-SEM-09** **Derived and ratio metrics** with explicit null/zero-denominator semantics, so
  conversion rates and per-franchise averages are defined once.
- **DL-SEM-10** **Metric lineage**: each metric records the physical columns, joins, and filters it
  derives from, queryable through the API for the data-dictionary and impact analysis.
- **DL-SEM-11** **Versioning and maker-checker publish**: semantic definitions are high blast radius;
  publish requires approval, prior versions are retained, and a rollback is one API call. Propagation
  of a published version to the runtime, and the restatement notice a definition change triggers, are
  specified in **DL-11** (`DL-CFG-09`, `DL-CFG-13`) — a semantic change is *apply-forward but
  restatement-flagged*, because definitions are evaluated at read time and therefore silently
  restate every historical figure unless announced.
- **DL-SEM-12** **Result caching** keyed on `(tenant, model_version, compiled_query_hash, access_tags)`
  with explicit invalidation on model publish and on analytics partition change.

### Business content

- **DL-SEM-03** **Author the enterprise entity model**: Company/Account, Person/Contact, Franchisee,
  Brand, Location, Lead, Opportunity, Contract, Contract Term, Invoice (AR), Bill (AP), Payment,
  Royalty, Campaign, Ad Group, Call, Job/Work Order, Employee. Each mapped to golden records and
  each carrying a declared business definition and owner.
- **DL-SEM-04** **Author the enterprise KPI set** named in SOW §4, each with an unambiguous
  calculation, grain, filters, and owner:

  | KPI | Definition obligation |
  |---|---|
  | Sales | Gross booked value; state inclusion of tax, discounts, cancellations |
  | Revenue | Recognised revenue; state the recognition basis and period |
  | Collected Revenue | Cash received; distinguish from recognised revenue explicitly |
  | Royalties | Franchisee royalty per contract terms; state the rate source and base |
  | Leads | Qualified vs. raw; state the qualifying event and dedup rule |
  | Opportunities | Stage-gated pipeline; state which stages count |
  | Conversions | Numerator/denominator and attribution window |
  | Customer Acquisition | New-customer test and the cost basis for CAC |
  | Franchise Performance | The composite and its weights |
  | Operational KPIs | Per-department, defined with each department owner |
  | Marketing performance | Spend, CPL, CPA, ROAS across Google and Meta |

  A KPI is not "done" until the named business owner has signed the definition. Unvalidated
  definitions publish to a `draft` model version only.

- **DL-SEM-05** **Calculation methodology documentation** is generated from the model, not
  maintained separately — one artefact per model version, consumed by EP-04 and by training material.
- **DL-SEM-06** **Ownership and governance**: every entity, dimension, and metric carries
  `business_owner`, `steward`, `classification`, and `access_tags`. Ownership is enforced at
  publish — an unowned metric cannot be published.
- **DL-SEM-08** **KPI validation harness**: for each KPI, a stored expected-value test against a
  known period, executed in CI against a fixture dataset and post-deploy against real data as a
  smoke check. This is what makes §9 "KPI validation" a repeatable gate rather than a meeting.

---

## Data model

`datalake-semantic-model-dev` (existing, PK `tenant_code`, SK `model_version`) gains an active-version pointer
row. Model bodies move to S3 at `{tenant_code}/semantic-models/{version}.json` with the DynamoDB row
holding the pointer, hash, status (`draft`/`approved`/`active`/`retired`), and approval metadata —
models will exceed the DynamoDB item limit once joins and full KPI coverage land.

New: `datalake-semantic-approvals-dev` (PK `tenant_code`, SK `{model_version}#{approver}`).

---

## Interfaces

Extends the existing service; no new deployable.

```
GET  /tenants/{tc}/semantic/model                  active model
GET  /tenants/{tc}/semantic/model/versions         version history
POST /tenants/{tc}/semantic/model/validate         cross-record validation
POST /tenants/{tc}/semantic/model/publish          maker step
POST /tenants/{tc}/semantic/model/approve          checker step
POST /tenants/{tc}/semantic/model/rollback
GET  /tenants/{tc}/semantic/metrics/{name}/lineage
POST /tenants/{tc}/semantic/query                  existing, extended with filters/joins/grain
```

## Design and patterns

- **Interpreter/compiler** — the `QueryCompiler` stays the single place SQL is produced. Joins and
  time grain are compiler features, never caller-supplied SQL fragments.
- **Versioned-config repository**, mirroring the entity-resolution config registry already in the
  repo — do not invent a second versioning mechanism.
- **Specification** for filters, composing to a parameterised `WHERE`.
- **Builder** for the compiled query, so dialect differences (Athena vs. Redshift vs. MySQL) are
  isolated in one place per dialect.
- Deliberately **not** adopting Cube or dbt-metrics — the thin custom compiler already fits the
  tenant and serving model; revisit only if metric complexity outgrows it.

## Performance

- Compiled SQL is partition-pruned on `tenant_code`, date partition, and `brand_code` where present.
- Aggregation is pushed to the serving engine; the service never aggregates in Python.
- Every list/query response is paginated with a hard server-side row cap.
- Result cache (DL-SEM-12) targets the dashboard and chat read patterns, which are repetitive.
- Join planning is validated at publish so a pathological join is rejected before it can be run.

## Security and OWASP

- **A01** — per-metric and per-dimension access tags enforced at compile time, in addition to
  database GRANTs. A caller lacking a tag receives `AccessDeniedError`, and the metric is absent
  from model discovery responses so its existence is not disclosed.
- **A03** — structured requests only; values bound as parameters; identifiers allowlisted against
  the published model. The compiler is the injection boundary and is unit-tested as such.
- **A04** — publish is maker-checker; a single compromised account cannot silently redefine revenue.
- **A08** — model bodies are hash-verified against the DynamoDB pointer on load; a tampered S3 object
  fails closed.
- **A09** — every publish, approve, rollback, and access denial is audited.

## Observability

`SemanticQueriesCompiled`, `SemanticQueryLatencyMs{p50,p95,p99}`, `SemanticAccessDenied`,
`SemanticCacheHitRate`, `ModelPublishes`, `ModelValidationFailures`, `KpiValidationFailures` — all
alarmed. Compiled SQL is logged with the correlation id and **without** parameter values, so a
filter on a customer name never lands in CloudWatch.

## Reuse and redundancy

- KPI definitions are the single source consumed by dashboards (EP-05), reports (EP-06), the agent
  (DL-04), reconciliation (DL-02), and ML features (DL-05). No consumer redefines a metric.
- Time-grain logic is shared with the ML feature store rather than duplicated.
- The versioned-config repository is shared with relationship rules and entity-resolution configs.

## Acceptance criteria

1. All §4 KPIs published in an `active` model version with named owners and signed definitions.
2. A cross-entity query (e.g. revenue by brand by franchisee by month) compiles and returns correct
   results without the caller expressing a join.
3. KPI validation harness green in CI and as a post-deploy smoke check.
4. Access-tag enforcement proven: a user without the finance tag cannot see or query Revenue.
5. Rollback of a published model version demonstrated.

## Dependencies

- DL-01 and DL-02 — a KPI over unreconciled data is worse than no KPI.
- EP-04 provides the authoring console; the API contract here must land first.
