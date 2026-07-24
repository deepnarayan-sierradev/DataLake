# Known Gaps and Roadmap

**Last verified:** 2026-07-14, against the actual code (not inferred from older planning docs).

This is the single place that tracks what's still missing, broken, or deferred in this platform.
It replaces the old `architecture/GAP_ANALYSIS_FINDINGS.md` / `IMPROVEMENT_PLAN.md` /
`MULTI_TENANT_ROLLOUT_PLAN.md` three-document ID-tracking scheme (`ARCH-n`, `SEC-n`, `PERF-n`,
`OBS-n`, `DUP-n`, `INFRA-n`) — that scheme had accumulated ~30 IDs across three documents that
mostly restated the same underlying facts three different ways. Everything below is plain
language, deduplicated, and re-verified against the current codebase rather than carried forward
from old status tables.

For what's already built and working, see `docs/PLATFORM_STATUS.md` (current deployed state) and
`docs/PIPELINE_FLOW.md` (pipeline architecture and the canonical tenant-isolation model). This
document only covers what's **not** done yet.

Ordered roughly by severity — security/correctness gaps first, then performance/scale risks, then
smaller polish items.

---

## Security and correctness

### 1. No IAM-enforced tenant boundary anywhere

Tenant isolation for S3, Secrets Manager, and DynamoDB is entirely an application-level
naming/prefix convention today — not backed by IAM. `infrastructure/modules/iam/main.tf` scopes
every S3, DynamoDB, and Secrets Manager policy statement to the resource/bucket/table ARN only;
there is no `Condition` block anywhere tying a principal to its own tenant's data. A bug in path
construction, a compromised dependency, or a malformed request from one tenant could in principle
read or write another tenant's data, or fetch another tenant's source credentials. This is the
platform's single largest blocker to a credible multi-tenant security guarantee. Fixing it
properly is design-sized work — per-tenant IAM roles or resource-tag/prefix `Condition`s across
three services, phased carefully so the existing default tenant doesn't break.

### 2. Secrets Manager holds one shared credential per connector type, not per tenant

Every tenant using, say, the Salesforce connector shares the same Secrets Manager entry
(`edl/sources/{source_id}/credentials`) today. This only becomes a real problem once two tenants
need *different* credentials for the same connector type — not yet exercised, since dev only runs
one tenant. `tests/test_tenant_isolation.py` tracks this via a deliberately skipped placeholder
test rather than a silent gap. Needs a per-tenant credential path convention plus a migration for
any tenant already using shared credentials.

### 3. The `entity-extraction-config` DynamoDB table isn't tenant-scoped at the key level

Its partition key is still bare `source_id`; `tenant_code` is only a plain attribute, checked
after the read by an application-level guard
(`connector_runtime/configuration_repository/configuration_repository.py::_enforce_tenant_match`).
Because the guard fails closed, the practical symptom today is a 409 conflict blocking a second
tenant's onboarding if they pick the same connector/entity combination as an existing tenant, not a
live data leak. The same tenant-scoped-key pattern already exists twice elsewhere in the codebase
(`watermark_repository.py`, `entity_type_registry.py`) to copy from — a contained, well-understood
fix plus a companion GSI.

### 4. Serving store has no network path for BI tools to reach it

The serving store's per-tenant credential isolation is implemented correctly — one MySQL database
per tenant, one schema per tenant for PostgreSQL/SQL Server/Azure SQL/Redshift, a dedicated
read-only reader role scoped to only that tenant's container. But the RDS instance itself
(`infrastructure/modules/serving_store_database/main.tf`) is `publicly_accessible = false`, sits in
private subnets only, and its security group's only inbound rule is from the loader Lambda's own
security group. There is no VPN, PrivateLink, or bastion anywhere in
`infrastructure/modules/networking/` — so as designed today, an external Power BI or Tableau
connection cannot reach the database at all. As of 2026-07-24 the serving store's Terraform **has
been applied in dev** (`edl-serving-store-mysql-dev`), so this is now a live gap, not a hypothetical
one — though the dev instance is still empty (no tenant onboarded, see `docs/PLATFORM_STATUS.md`),
so nothing needs to reach it yet. Onboarding a tenant/entity is now a first-class command
(`scripts/seed_serving_store_config.py`), but there is still no script or API to hand a tenant its
auto-created reader credential once the loader provisions it. **The same gap applies to the new
Redshift Serverless engine** (`infrastructure/modules/serving_store_redshift/main.tf`,
`publicly_accessible = false`, enhanced VPC routing, ingress only from the loader Lambda's SG) —
its credential/GRANT isolation is correct, but no BI-reachable network path exists for it either.

**Needs a design decision** among: (a) **AWS Client VPN** with per-tenant client
certificates, paired with each tenant running Power BI's On-premises Data Gateway or Tableau
Bridge as the VPN client — keeps the database fully private, matches how these BI tools already
expect to reach private data, recommended default; (b) site-to-site VPN/Direct Connect per
enterprise tenant — too heavy for self-service onboarding, only worth it for large tenants who
already run this; (c) AWS PrivateLink (NLB + VPC endpoint service) — good add-on for AWS-native
tenants, doesn't by itself help a laptop-based BI Desktop connection; (d) a publicly reachable
instance/proxy gated by per-tenant IP allowlists — fastest to build but widens the attack surface
and is fragile against BI vendors' dynamic cloud egress IPs. This is new infrastructure work, not a
config change; the serving store's Terraform is now deployed in dev, so this network path is the
next blocker before any BI tool can consume the serving store.

### 5. Glue/Athena analyst access is a wildcard grant across every tenant's data

`infrastructure/modules/glue/main.tf` defines exactly two shared Glue databases (`edl_curated`,
`edl_analytics`) for the whole platform. Tenant separation there is table-naming-convention only
(`{tenant_code}_{entity_type}`) — there are no per-tenant Glue databases, no LF-Tags, no data-cell
filters, and no `tenant_code` partition column. Three real IAM principals are currently configured
in dev's `terraform.tfvars` (`analytics_reader_principals`) with a Lake Formation grant of
`SELECT`+`DESCRIBE` and `wildcard = true` across the entire database — meaning each of those
principals can query every tenant's curated/analytics tables, not just one, if this has been
applied. This is a separate, smaller-blast-radius concern than item 4 above (it's about internal
AWS analyst/admin principals, not end-tenant BI access) but should be resolved with per-tenant
LF-Tags or data-cell filters before a second real tenant's data lands in a shared environment.

### 6. Tenant provisioning has no admin-level authorization check

`connector_runtime/api/control_plane_handler.py`'s `POST /tenants` route accepts any authenticated
caller — the handler's own docstring says so explicitly, because there's no existing tenant to
authorize against yet at that point in the flow. Once self-service multi-tenancy is actually live,
any authenticated user from any existing tenant could provision new tenants. Needs an admin-scoped
Cognito claim/authorizer that doesn't exist yet — small in code size, blocked on a design decision
about how platform-admin identities get established.

### 7. The control-plane API was built with no WAF

The original design called for an API Gateway + WAF (managed rule sets, per-tenant/per-IP rate
limiting) in front of the control plane. No `aws_wafv2` resource and no WAF module exist anywhere
in the repo — only the Cognito/JWT authorizer piece was built. Today the control plane's only
defense against abusive or malformed traffic is the authorizer itself, with no rate limiting.
Contained addition: a new WAF module associated with the existing API Gateway.

### 8. The control plane's live authentication path has never been exercised end-to-end

The control-plane API (routes, Cognito, JWT authorizer) is code-complete and
`terraform validate`-clean, but no login/token round-trip has been run against the actually-deployed
Cognito pool to confirm which claims shape (`authorizer.claims` vs. `authorizer.jwt.claims`) API
Gateway actually populates at runtime. The handler defensively checks both, so it fails closed
either way — but the assumption is unverified against a real deployment. It also appears dormant in
practice: no code-level evidence of any real traffic since it was deployed.

### 9. Two known, currently-broken tenant configs (found during a config audit, not fixed)

- The NetSuite entity configuration is missing a required `record_type` value in
  `connector_params` — extraction for this entity will fail as configured today.
- The `supplier` entity's entity-resolution configuration uses match-rule keys (`similarity`,
  `confidence_threshold`) that don't match what the resolution engine actually reads
  (`similarity_kind`, `match_threshold`) — this config is currently a no-op / silently wrong rather
  than an error.
- No entity today has a quality policy actually wired in — the quality-evaluation feature exists in
  code but isn't attached to any real entity's configuration yet.

These need a config-data fix (updating the seeded JSON), not a code change, and are separate from
the code-level gaps elsewhere in this document.

### 10. Lineage records and quality reports carry no tenant boundary in their S3 key

`governance/lineage_record.py` writes to `lineage/{entity_id}/{run_id}/{stage}-lineage.json` with
no `tenant_code` segment and no tenant check on read at all. `transformation/transformation_pipeline.py`'s
quality-report writer has the same gap
(`quality-reports/{source_id}/{entity_id}/{run_id}/quality-report.json`). Because `run_id` is
globally unique, two tenants' records land at different paths and don't overwrite each other — but
they're interleaved under one shared, unscoped prefix with nothing an IAM policy could rely on as a
boundary. Contained fix: add the `{tenant_code}/` prefix, matching every other data layer's
existing pattern.

---

## Performance and scale

These are all fine at today's single-digit-tenant, dev-scale data volumes and become real risks as
tenant/entity count or data volume grows.

### 11. Two pipeline stages can exhaust Lambda memory on a large enough entity

`transformation/transformation_pipeline.py`'s `_load_raw_records` fully materializes a raw S3
prefix into one Python list on the path taken whenever any quality policy, PII masking, or SCD
accumulator is configured — which is the common case, not an edge case. Separately, the entity
resolution handler's cross-source loader streams each source via DuckDB but still concatenates
every source's full record list into one combined list before matching, because the matching
engine's public contract requires a plain list rather than an iterator. Moderate refactor: a
streaming-friendly matching engine interface and batch-based raw record iteration.

### 12. The analytics publisher holds two full in-memory copies of every golden record

It loads all golden records into one list, then builds a second full list comprehension to strip
internal entity-resolution fields before writing — two complete copies resident at once in a
512MB Lambda. Fixable with a streaming/batched strip-and-write pass.

### 13. Tenant-scoped list queries are full DynamoDB table scans

Both `configuration_repository.py::list_configs_for_tenant` and
`control_plane_handler.py::_handle_list_runs` do a full `.scan()` with a tenant filter, because
neither table has a tenant-keyed GSI. Cost and latency scale with total table size across *all*
tenants, not the calling tenant's slice. Fix: a `tenant_code`-keyed GSI on each table plus a switch
from `Scan` to `Query`.

### 14. The watermark table's only GSI is a three-value hot partition

`environment-watermark-index` is hash-keyed purely on `environment` (`dev`/`staging`/`prod`), so
every watermark row in an environment lands in one GSI partition regardless of tenant or entity
count, even though the base table's own primary key is correctly tenant-scoped. Needs a better GSI
key shape — design-sized since changing a GSI key requires care around existing data.

### 15. EventBridge schedules have zero jitter

`orchestration/event_bridge/extraction_schedule_client.py` hardcodes `FlexibleTimeWindow` to
`{"Mode": "OFF"}` for every schedule, so every tenant/source/entity sharing a common cron boundary
fires at the exact same instant — a thundering-herd risk once many tenants share schedule times.
Small fix: widen the flexible time window setting.

### 16. No true checkpoint-and-resume for Lambda's 900-second timeout

Checkpoint detection exists — the extraction workflow can tell it's about to run out of time and
commits a partial watermark cleanly — but nothing automatically re-invokes Step Functions from that
checkpoint; a checkpointed run needs a manual re-trigger today. Genuinely design-sized: needs
either a `Choice`/`Wait` construct or a redesigned input contract, since ASL's `Catch` doesn't feed
error details back into a retried task's parameters.

### 17. NetSuite pagination has a hard ceiling instead of a real fix

SuiteQL offset/limit pagination now fails with an actionable error before requesting an offset past
100,000 (NetSuite's real, undocumented ceiling) instead of failing unpredictably — an improvement,
but the real remedy (keyset pagination on a monotonic column) isn't built. The interim workaround
requires tightening each entity's watermark increment to keep a single run's result set under
100,000 rows.

### 18. DuckDB-accelerated code paths silently never run in any deployed Lambda

The Lambda deployment package's dependency list (the `Makefile`'s `lambda-package` target) doesn't
include `duckdb`, even though it's a declared project dependency. Every DuckDB-accelerated path
(SCD-merge, curated-load, cross-source entity-resolution join) has a graceful fallback to a slower,
fully-materializing Python implementation — so nothing crashes, but several performance
improvements documented elsewhere as complete quietly never execute as designed. Small fix: add
`duckdb` to the Lambda package's pip install list (subject to it having a compatible prebuilt wheel
for the Lambda runtime).

### 19. Secrets Manager credential rotation is wired in Terraform but never activated

`rotation_lambda_arn` variables exist for Salesforce/NetSuite/MySQL RDS in
`infrastructure/modules/secrets/variables.tf`, but no environment sets them, so zero rotation
resources are ever created. What exists instead is a credential-expiry *notification* Lambda (an
SNS alert before expiry) — useful, but not the same as automated rotation. Building the actual
rotation Lambda per connector is design-sized (each source has a different "reset credential" API).

### 20. No per-tenant usage metering or billing capability

Nothing in the codebase tracks records processed per tenant per period. Blocks any
consumption-based billing model. Design-sized: likely a new Lambda subscribed to the existing
CloudWatch metrics stream.

---

## Smaller polish items

- **Two correlation-ID mechanisms coexist.** Extraction/transformation thread `run_id` as an
  explicit keyword argument; entity resolution/analytics publisher rely on `structlog.contextvars`
  instead. Same guarantee, two mechanisms — a future refactor could silently drop one. Small,
  mechanical fix to standardize on one approach across four handlers.
- **No shared helper for Lambda handler boilerplate or test fixtures.** Logger/metrics/X-Ray wiring
  and moto S3/DynamoDB test bootstrap are repeated independently across all four Lambda entrypoints
  and 13+ test files. Not urgent, but maintenance cost scales with every new connector added.
- **Sage's Intacct/X3 query engines remain separate from the shared query-builder pattern** used by
  Salesforce/NetSuite/MySQL. This is a deliberate decision (Sage builds JSON/OData request bodies,
  not SQL text — forcing it through the shared template would be a leaky abstraction), not
  unfinished work. Listed here for visibility only.
- **Lambda memory sizing is flat across all entity types.** A per-entity `lambda_memory_mb`
  override (for DuckDB-heavy merges vs. small entities) was proposed and explicitly deprioritized.
  Low priority at current data volumes.
- **72 pre-existing mypy errors across 16 files** remain unaddressed (re-run the scoped command in
  root `CLAUDE.md` to get the current count — it drifts slightly with incidental fixes in touched
  files). Keeps the CI `typecheck` job red until separately remediated.

---

## Not yet attempted (needs a real deployment, not more code)

- **Pilot tenant onboarding** — running one real second tenant in parallel with the default tenant
  for a full week with no cross-tenant incidents.
- **Load testing** at the target scale (80–100 entities per tenant).
- **Staging and prod environments are not provisioned yet** — only dev has been deployed. Both
  validate cleanly (`terraform validate`) and are ready to bootstrap when needed.
