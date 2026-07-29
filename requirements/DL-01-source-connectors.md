# DL-01 — Source Connectors and Integration Coverage

**SOW clauses:** §3.1, §3.2, §3.4, §3.8, §14, §18 · **Priority:** P0 · **Owner repo:** DataLake

---

## Objective

Deliver working, scheduled, incrementally-synchronised extraction for the ten source systems named
in `Evive Data Sources - Data Lake.xlsx`, plus a connector framework that absorbs future and
acquired source systems without platform change.

## Current state (verified 2026-07-28)

Four adapter families exist under `connector_runtime/adapters/`: `salesforce`, `mysql_rds`,
`netsuite`, `sage` (Intacct + X3). The plugin framework is sound — `connector_registry` with
`register()` / `register_builder()` / `register_params_model()`, a `ConnectorInterface` contract,
per-source Pydantic params models, credential retrieval from Secrets Manager, and a shared
`RawLayerWriter`.

**Against the required source list, exactly one of twelve rows has a connector (Sage Intacct), and
its Secrets Manager entry is an empty shell — zero of twelve have ever extracted a row.** Three of
the four built adapters (Salesforce, MySQL RDS, NetSuite, Sage X3) do not appear on the customer's
list at all.

| # | Source | Brand | Department | Integration | Notes from source list |
|---|---|---|---|---|---|
| 1 | Maid Central | Maid Brigade | Operations | API | API available, some bugs, cooperative vendor |
| 2 | ServMan Pro | Pacific Lawn & Sprinklers | Operations | API | CRM + call-centre; API being built out |
| 3 | WellSky | Executive Home Care | Operations | API | ~50+ tables, API rate limits |
| 4 | Sage Intacct | Evive | Finance | API | 100+ tables, complex reporting |
| 5 | HubSpot | Evive | Operations | API + **bi-directional** | Franchise Management System, critical |
| 6 | Google Ads / Analytics | Evive | Marketing | API | — |
| 7 | Meta Ads | Evive | Marketing | API | — |
| 8 | DialPad | Brothers Gutters | Operations | API | vendor switch, source not yet established |
| 9 | HouseCall Pro | Shine | Operations | API | BI Pro API connection |
| 10 | HubSpot | Brothers Gutters | Operations | API | migrating from Service Bridge |
| 11 | HubSpot | Grasons | Operations | API | migrating from IFX |
| 12 | SeniorPlace | Assisted Living Locators | Operations | API | currently OData to ALL IN |
| 13 | ServiceBridge | Brothers Gutters | Operations | API | outgoing system behind row 10's migration; history source |
| 14 | BePro | — | Performance | API | added 2026-07-29 from customer-supplied documentation |

### Corrections applied 2026-07-29 from the customer's supplementary API documentation

The customer supplied vendor documentation for MaidCentral, ServMan Pro and WellSky, plus doc URLs
for SeniorPlace, ServiceBridge, BePro, DialPad, HubSpot and Sage Intacct. Reading them against the
shipped specs found four material deviations, all now closed under `DL-CONN-20`:

| Source | What the spec claimed | What the vendor documents |
|---|---|---|
| MaidCentral | `X-Api-Key`, `/v1/{entity}`, `data` envelope, `offset`/`limit`, 7 entities | OAuth 2.0 `POST /token`, `/api/v1/reporting/{entity}`, `Result.Items`, `skipCount`/`maxResultCount` (max 1000), 13 entities, 1000 req/hour |
| WellSky | 50 entities at `/api/v2/...`, keyset paging, `data.records` | FHIR-shaped Home Connect API at `connect.clearcareonline.com/v1/`; `POST /_search/`, `_page`/`_count` (max 100), `entry[].resource`. The "~50 tables" is **WellSky Insights**, a separate warehouse product |
| SeniorPlace | OData at `/odata/{Set}`, `$filter`, bearer | REST at `app.seniorplace.com/api/v1`, `Authorization: ApiKey <key>`, `updatedAfter` on `/clients` only, no documented paging. The OData contract belongs to **ALL IN**, the downstream system |
| DialPad | token bucket refilling at 2 req/s | 20 req/s per company — the spec was throttling to a tenth of the allowance for no stated reason |

**ServMan Pro is not corrected here because it is not a REST source.** The two documents supplied
(`servmandatadictionary_2026.pdf`, `servmantablerelationships_2026.pdf`) are a 346-table *database*
schema for a Microsoft SQL Server / Codebase installation, not an API reference. The REST spec is
retained as-is with its `PENDING_VENDOR_ENTITIES` guard, and the database path is recorded as an
open decision rather than silently modelled. See `docs/SOURCE_API_FIDELITY_AUDIT.md`.

---

## Functional requirements

### Connector delivery

- **DL-CONN-01** Deliver a **HubSpot** connector covering CRM objects (companies, contacts, deals,
  tickets, engagements, owners, pipelines, line items) and custom objects discovered at runtime.
  Highest priority: it serves rows 5, 10, 11 of the source list.
- **DL-CONN-02** Extend the HubSpot connector with a **bi-directional write path** for the Franchise
  Management System use case (§3.8). Writes are opt-in per entity, idempotent (external-id upsert),
  rate-limit aware, and audited to `EdlRunAuditLog` with a distinct `stage` value. A write path must
  never be enabled by the same config flag that enables reads.
- **DL-CONN-03** Deliver a **Maid Central** connector.
- **DL-CONN-04** Deliver a **ServMan Pro** connector. Vendor API is still under construction —
  the adapter must tolerate endpoint absence and surface a distinguishable
  `SourceCapabilityUnavailable` error rather than a generic failure.
- **DL-CONN-05** Deliver a **WellSky** connector with entity selection over its ~50-table model, and
  an adaptive rate limiter (see DL-CONN-11).
- **DL-CONN-06** Deliver **Google Ads** and **Google Analytics 4** connectors as two registered
  sources sharing one OAuth credential client. Report-style APIs, not row APIs: the query builder
  emits a metric/dimension/date-range request, and the raw layer stores the returned report rows.
- **DL-CONN-07** Deliver a **Meta Ads** connector (Marketing Insights API), same report-style shape
  as DL-CONN-06, with async job polling for large date ranges.
- **DL-CONN-08** Deliver a **DialPad** connector (call records, call logs, users, call centres).
- **DL-CONN-09** Deliver a **HouseCall Pro** connector.
- **DL-CONN-10** Deliver a **SeniorPlace** connector. If only OData is available initially, implement
  it via the OData query engine already proven in `adapters/sage/products/x3/x3_query_engine.py`
  rather than a new OData implementation.
- **DL-CONN-12** Activate **Sage Intacct**: populate credentials, seed entity configs, seed field
  mappings, enable schedules, and complete a verified first extraction. No new code expected.
- **DL-CONN-18** Deliver a **ServiceBridge** connector. Row 10's "migrating from Service Bridge"
  makes this a *history* source: the pre-migration operational record for Brothers Gutters. Its
  documented quota — 50 req/s and 60000 req/hour — is **per IP address, not per token**, so its
  rate-limit policy must be `shared_across_connections`; a per-connection policy would let N
  concurrent extractions each believe they own the whole budget. Its session-key credential travels
  in the query string, so the HTTP layer must never log a query string (OWASP A09) and must
  re-acquire the key on its 30-minute sliding expiry.
- **DL-CONN-19** Deliver a **BePro Data API** connector over the vendor's published OpenAPI 3.1
  surface (20 endpoints across `meta`, `data`, `external`, `video`). The API exposes **no
  modification timestamp on any endpoint**, so the connector must decline the `incremental`
  capability and run full loads rather than advancing a watermark against data it never filtered
  on. Its quota is two-tier (1000 req/min sustained, 100 req/s burst) and must be expressed as a
  token bucket, not a fixed window. Endpoints requiring a `match_id` are declared with
  `required_run_parameters` and fail closed as a configuration error until a match-scoped fan-out
  exists.
- **DL-CONN-21** **Config-declared entities for REST sources.** A REST source must accept an
  entity the configuration console declared, exactly as Salesforce (`object_name`), MySQL
  (`table_name`) and NetSuite (`record_type`) always have. Adding an entity in the console must
  not require a code change in this repository. The console supplies `entity_path` plus any of
  `entity_records_json_path`, `entity_watermark_field`, `entity_natural_key_field`,
  `entity_pagination_strategy`, `entity_record_unwrap_field`, `entity_read_method`; everything
  omitted is inherited from the source's declared conventions. Constraints that must hold:
  a spec-declared entity always wins over configuration (config cannot redirect a curated
  endpoint); the path is rejected on traversal, protocol-relative form, or any character outside
  the safe set; the host allowlist still applies at call time; the read verb is GET or POST only;
  and **write-back is never settable from configuration** — enabling a read must not be able to
  enable a source mutation. An unknown entity with no `entity_path` fails as
  `DETERMINISTIC_INVALID_CONFIGURATION` naming exactly what to supply, not as a bare `KeyError`.
  Added 2026-07-30: the spec-driven REST family was the only adapter family that required a code
  change to onboard an entity, which contradicted the platform's configuration-driven premise.
- **DL-CONN-20** **Specification fidelity.** Every source spec's endpoint paths, auth kind,
  response envelope, pagination parameters, and rate limits must be traceable to that vendor's
  published documentation, and asserted against it in
  `connector_runtime/tests/test_documented_source_fidelity.py`. Added 2026-07-29 after an audit
  against the customer-supplied API documentation found MaidCentral, WellSky and SeniorPlace each
  specified against an imagined API — every one of them would have failed on its first request
  while passing the entire substrate test suite.

### Framework capabilities the new connectors require

- **DL-CONN-11** **Adaptive rate limiting.** A reusable `RateLimitPolicy` in `connector_runtime/`
  supporting fixed-window, token-bucket, and `Retry-After`-driven backoff, configured per source in
  the params model. On sustained 429s the connector must checkpoint and exit cleanly rather than
  burn the Lambda budget — reuse the existing `LambdaTimeoutWarning` checkpoint path.
- **DL-CONN-13** **Change Data Capture where available** (§3.4). A `SyncStrategy` abstraction with
  three implementations: `WatermarkPolling` (current behaviour), `WebhookIngest` (HubSpot, DialPad
  and HouseCall Pro support webhooks), and `LogBasedCdc` (MySQL binlog via DMS for the RDS source).
  Configuration selects the strategy; the watermark repository remains the resume point for all
  three so a webhook gap can always be back-filled by a polling run.
- **DL-CONN-14** **Webhook receiver.** An API Gateway + Lambda endpoint per webhook-capable source
  that verifies the provider signature (OWASP A08 — integrity), enqueues to the existing SQS FIFO
  path with `MessageGroupId = tenant_code#source_id#entity_id`, and never processes inline.
  Replay-protected by event id in a short-TTL DynamoDB table.
- **DL-CONN-15** **Pagination strategies as a registered concern.** Offset/limit, cursor, keyset,
  and link-header pagination behind one `PaginationStrategy` interface. This also closes gap 17 —
  NetSuite keyset pagination becomes an implementation of the interface rather than a special case.
- **DL-CONN-16** **Connector scaffolding parity.** Every new connector is generated through the
  existing `/new-connector` skill so interface, credential client, raw-layer writer, query builder,
  params model, and Terraform secret are produced in one consistent shape.
- **DL-CONN-17** **Source-capability declaration.** Each adapter declares supported capabilities
  (`incremental`, `soft_delete`, `webhooks`, `bulk_export`, `writeback`, `schema_discovery`) in a
  frozen dataclass exposed through the registry, so the configuration console (EP-04) can render
  only what a source actually supports instead of hardcoding source names.

---

## Data model

No new pipeline layers. Additions:

| Store | Change |
|---|---|
| `EdlEntityExtractionConfig` | new attributes: `sync_strategy`, `rate_limit_policy`, `pagination_strategy`, `writeback_enabled` |
| `EdlWebhookEventDedup` (new) | PK `tenant_code`, SK `provider_event_id`, TTL 48h — replay protection |
| `EdlRunAuditLog` | new `stage` values `webhook_ingest`, `writeback` |
| Secrets Manager | one shell per new source at `edl/sources/{source_id}/credentials` |

---

## Interfaces

```python
class SyncStrategy(ABC):
    @abstractmethod
    def plan(self, config: EntityExtractionConfig, watermark: Watermark | None) -> ExtractionPlan: ...

class RateLimitPolicy(ABC):
    @abstractmethod
    def acquire(self) -> None: ...
    @abstractmethod
    def observe(self, response_headers: Mapping[str, str]) -> None: ...

class PaginationStrategy(ABC):
    @abstractmethod
    def pages(self, request: SourceRequest) -> Iterator[SourcePage]: ...
```

All three register into `connector_registry` alongside the existing connector and builder
registries. No adapter imports another adapter.

---

## Design and patterns

- **Registry + Factory** — unchanged; new strategies join the same registry rather than creating a
  parallel lookup mechanism.
- **Strategy** for sync/pagination/rate-limit, so a new source composes existing behaviour instead
  of subclassing a connector.
- **Adapter** per source system, one module per source, no cross-adapter imports.
- **Template method** for the extraction stage lifecycle, via the shared handler scaffold (FR-F0.4).
- Explicitly **not** a plugin discovery mechanism based on filesystem scanning — registration stays
  import-time and explicit, which is what makes the current registry testable.

## Performance

- Pagination is an iterator end-to-end; a page is written to the raw layer and released. No
  connector accumulates a full result set in memory.
- Report-style connectors (Google, Meta) submit asynchronous jobs and poll with exponential backoff
  rather than holding a Lambda open.
- WellSky's ~50 tables are extracted as independent scheduled entities, never one run.
- Per-entity Lambda memory override is now justified (previously deprioritised in the gap register)
  — report-style sources need more than the flat default.
- Concurrency per source is capped by the rate-limit policy, not by Lambda reserved concurrency
  alone, so one busy source cannot starve another.

## Security and OWASP

- **A01** — every extraction resolves credentials through `credential_client.py` scoped to the
  extraction runtime role; no connector reads a secret path it constructs itself.
- **A02** — new secrets are KMS-encrypted with the existing secrets CMK; write-back credentials are
  a *separate* secret from read credentials so a read-only deployment cannot mutate a source.
- **A03** — all source query construction goes through a query builder with parameter binding;
  no string-concatenated filters. OData and SOQL builders already enforce this.
- **A08** — webhook payload signature verification is mandatory and fails closed; unsigned providers
  are polled, not webhooked.
- **A09** — every extraction and every write-back is audited with tenant, entity, run, actor.
- **A10** — outbound HTTP is restricted to an allowlist of source hostnames resolved from
  configuration, mitigating SSRF via a tampered `base_url`.

## Observability

Metrics per source and entity, all alarmed (no dead alarms — the reconciliation guard test in
`observability/tests/test_alarm_emitter_reconciliation.py` enforces this):

`RecordsExtracted`, `ExtractionDurationMs`, `RateLimitHits`, `RateLimitBackoffMs`,
`PagesFetched`, `WebhookEventsReceived`, `WebhookSignatureFailures`, `WritebackRecords`,
`WritebackFailures`, `SourceApiErrors{status_class}`, `CheckpointedRuns`.

Structured logs bind `run_id`, `tenant_code`, `source_id`, `entity_id`, `correlation_id`. X-Ray
subsegment per source API call.

## Reuse and redundancy

- One credential client, one raw-layer writer, one watermark repository — no per-source copies.
- The OData engine is shared between Sage X3 and SeniorPlace.
- The OAuth client is shared between Google Ads and Google Analytics.
- Signature verification is one function parameterised by algorithm, not one per provider.
- Anything a second connector needs moves into `connector_runtime/` before the second connector
  ships, not after.

## Acceptance criteria

1. All ten required sources registered, credentialed, seeded, scheduled, and each has completed at
   least one successful scheduled extraction in a non-dev environment with non-zero rows.
1a. ServiceBridge (`DL-CONN-18`) and BePro (`DL-CONN-19`) likewise registered and reachable from
   the extraction handler, with their documented quotas encoded and asserted.
1b. Every source spec's documented facts asserted in
   `connector_runtime/tests/test_documented_source_fidelity.py` (`DL-CONN-20`), so a spec and the
   vendor document it came from cannot drift apart silently.
2. HubSpot write-back demonstrated round-trip on a non-production HubSpot portal, with audit records.
3. Rate-limit policy demonstrated against WellSky under a forced 429 without data loss.
4. CDC or webhook ingest live for at least HubSpot, with a polling back-fill proving gap recovery.
5. `tests/test_tenant_isolation.py` still green; a new per-connector isolation test added.
6. Coverage gate held at ≥80%; `ruff`, scoped `mypy`, `bandit` clean.

## Dependencies

- Customer-supplied credentials and API access per §19 — the critical path constraint for every
  connector here.
- DL-SEC-05 (per-tenant credential paths) must land before two tenants share a connector type.
- EP-04 renders `DL-CONN-17` capability declarations; ship the declaration first.

## Out of scope

- Replacing Salesforce/NetSuite/Sage X3 adapters — retained, not on the customer's list but proven.
- File-based and database-direct integrations (§3.1 lists them as *may include*) until a named
  source requires one.
