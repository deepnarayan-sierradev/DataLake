# Known Gaps and Roadmap

**Last verified:** 2026-07-28, against the actual code (not inferred from older planning docs).

**Second pass, 2026-07-28 (wiring).** An audit found that most of the programme's code had no
deployed caller: the scope predicate was never constructed, the ten connectors were unreachable,
DL-11 and DL-06 had no entry point, and a test parameterised over every consumption surface
reported isolation working while four surfaces applied no filter. That has now been fixed, and
**six gates in CI make the class of defect detectable** rather than relying on review:

| Gate | Command | Catches |
|---|---|---|
| G1 reachability | `make reachability` | A production module with no production importer |
| G2 registry | pytest | A source the extraction handler cannot resolve |
| G3 call sites | pytest | A consumption surface that applies no scope predicate |
| G4 fail-open | `make fail-open` | A security parameter defaulting to `None` |
| G5 traceability | `make traceability` | A requirement uncited, unreachable, or falsely waived |
| G6 absence alarms | Terraform | A control that publishes no metric because it never runs |

G1 and G5 **fail on a stale waiver**, so `requirements/WAIVERS.md` cannot drift into fiction.

Items below marked "closed in code" were re-checked against reachability during this pass; where
the code existed but nothing called it, the item now says so.

**Read this first.** The SOW requirements programme (`requirements/`, DL-01…DL-12 except the
deferred DL-04 agent runtime and DL-05 ML platform) landed on 2026-07-28 and closed or changed
many items below. Where an item says **Closed in code**, the code and Terraform exist and the test
suite covers them, but **nothing below has been applied to a live AWS account beyond dev's
pre-programme state** — `terraform apply` per environment is still outstanding, and an
audit-then-enforce control counts as closed only once its enforce mode is switched on. Treat
"closed in code" as "ready to deploy", never as "in force in production".

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

### 1. IAM tenant boundary: the policy was **unsatisfiable**, not merely unapplied (**partially closed 2026-07-29**)

The 2026-07-29 re-assessment found this was worse than "closed in code, awaiting apply". The policy
conditions every statement on `${aws:PrincipalTag/tenant_code}`, and it attached to four Lambda
execution roles that each serve **every** tenant — a role tag holds one value, so the tag was never
set and could not be. Flipping to `enforce` would have broken asymmetrically:

- the S3 statements are guarded by `Null aws:PrincipalTag/tenant_code = false`, never true for an
  untagged principal, so the Deny would not apply and **S3 would have stayed open**;
- the DynamoDB statement compares `dynamodb:LeadingKeys` against an unresolvable variable, which no
  key matches, so it would have **denied every item operation**;
- `secretsmanager:ResourceTag/tenant_code` is absent on every secret, and a negated condition on a
  missing key is true, so it would have **denied every credential read**.

Separately, **no environment passed `data_bucket_arns`, `tenant_scoped_table_arns`, or
`cloudtrail_log_group_name`.** With empty lists the S3 and DynamoDB statements carry no `Resource`,
which IAM rejects outright — so the policy could never have applied successfully anywhere, while
`terraform validate` stayed green because validate does not call IAM.

**What is now closed:**

- `tenancy/tenant_session.py` is the mechanism that can carry the tag: a stage role assumes a
  per-stage data role with `Tags=[{tenant_code}]`, and `sts:TagSession` in the trust policy makes
  the tag authoritative. Credentials cache per *(role, tenant)* — keyed by role alone, a warm
  container would hand one tenant's session to the next tenant it served.
- The boundary attaches to those data roles rather than the shared stage roles.
- All three inputs are wired in dev, staging and prod, and plan-time preconditions fail when the
  boundary would be created with no resources or when `enforce` is set with no CloudTrail.
- An **interlock**: `enforce` requires `tenant_session_tagging_adopted = true`, so the unsafe
  combination is unreachable rather than merely discouraged.
- The audit-stage metric is renamed `IamBoundaryAccessDenied`. It was `CrossTenantAccessAttempts`,
  which the control plane already emits for a different event — so the "sustained zero" that gates
  the flip was satisfiable by an unused API and proved nothing about IAM.

**What is still open, and tracked rather than claimed:** 47 data-plane call sites build clients from
ambient credentials, so they are outside the boundary regardless of the policy. `make
tenant-session-adoption` (G9) lists them; `tests/test_capability_reachability.py` asserts the session
helper is still pending so the waiver cannot go stale; `requirements/WAIVERS.md` records why threading
a session through ~47 repository constructors is design-sized rather than half-done.

### 2. ~~Secrets Manager holds one shared credential per connector type~~ (**closed in code**)

Credentials are now **per connection**:
`edl/tenants/{tenant_code}/connections/{connection_id}/credentials`, resolved through
`connector_runtime/connection_credential_resolver.py::ConnectionCredentialPathResolver`. Write-back
uses a separate `-writeback` secret with **no** legacy fallback, so a read-only deployment cannot
mutate a source. The skipped placeholder in `tests/test_tenant_isolation.py` is gone, replaced by
`TestSecretsManagerConnectionIsolation` with real assertions.

**Outstanding operational step:** the legacy shared path is still read as a fallback *with a
warning* so a partially-migrated environment cannot lose its only credential copy. Per environment:
run `make migrate-credentials` (dry-run by default, then `--apply`), confirm no legacy-path warning
appears in logs, and only then re-run with `--delete-legacy`.

### 3. ~~The `entity-extraction-config` DynamoDB table isn't tenant-scoped at the key level~~ (**closed**)

The partition key now stores `tenant_scoped_key(tenant_code, connection_id)` (migration applied to
dev on 2026-07-24), so two tenants configuring the same source/entity no longer collide. The
application-level `_enforce_tenant_match` guard remains as defence in depth.

The **connection** dimension went in with the same change (DL-12): the key component is
`connection_id`, and for a single-connection source `connection_id == source_id`, which is what
keeps every pre-existing key byte-identical. `scripts/migrate_to_connection_identity.py` (dry-run
default, `--apply`, `--rollback`) registers the default connections and must run **before** the
connection-aware code is deployed to an environment.

What is *not* closed: `list_configs_for_tenant` is still a `Scan` — see item 13. A tenant-scoped
partition key does not make a tenant listing efficient, because DynamoDB cannot prefix-match a
partition key.

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

### 5. Glue/Athena analyst access is a wildcard grant across every tenant's data (**closed in code**)

**Programme status:** `infrastructure/modules/lake_formation/` now defines per-tenant and
per-department LF-Tags and replaces the wildcard grant; `aws_lakeformation_data_lake_settings`
carries `principal = "IAM_ALLOWED_PRINCIPALS"` with empty permissions so nothing is implicitly
granted. **Applying this revokes an existing grant three real dev principals depend on**, so it
needs the account owner to name the principals and confirm before `terraform apply` — do not apply
it as a side effect of another change. Original description follows.


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

### 6. ~~Tenant provisioning has no admin-level authorization check~~ (**closed by deletion, 2026-07-28**)

There is no tenant-provisioning route here any more, and there should never be one. `POST /tenants`,
its handler, its request model, and the platform-admin capability were **deleted**: tenants, users,
roles, and permissions are owned by the **Identity API**, and this repository is a
configuration-driven processing system that only ever *consumes* a verified tenant claim. See the
"System boundary" section of the root `CLAUDE.md`.

`connector_runtime/tests/test_control_plane_handler.py` asserts the route's **absence**, so it
cannot be reintroduced by reflex. The corresponding requirement, DL-SEC-12, is marked **WITHDRAWN**
in `requirements/DL-08-security-tenant-isolation.md`. Claim *validation* stayed — that is consuming
identity, not owning it.

### 7. The control-plane API was built with no WAF (**closed in code, audit mode**)

**Programme status:** `infrastructure/modules/waf/` now exists (managed rule sets plus rate
limiting) and is wired into all three environments, shipping in **audit** ("count") mode so a
false positive cannot lock out the control plane on day one. Not closed operationally until the
mode is switched to enforce after a review of counted matches. Original description follows.


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

### 10. ~~Lineage records and quality reports carry no tenant boundary in their S3 key~~ (**closed**)

Both writers now carry the `{tenant_code}/` prefix, matching every other data layer
(`governance/lineage_record.py`, `transformation/transformation_pipeline.py`), and their tests
assert the tenant-prefixed key rather than the old unscoped one. This was a prerequisite for the
IAM conditions in item 1 — an unscoped prefix is not something an IAM policy can key on.

Historical description, for anyone reading old S3 objects written before the change:

`governance/lineage_record.py` wrote to `lineage/{entity_id}/{run_id}/{stage}-lineage.json` with
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

### 13. ~~Tenant-scoped list queries are full DynamoDB table scans~~ (**closed in code**)

Tenant-keyed GSIs are declared on `EdlEntityExtractionConfig` (`tenant-entity-index`, KEYS_ONLY)
and `EdlRunAuditLog` (`tenant-started-index`), and `list_configs_for_tenant` queries the index when
it exists, falling back to the Scan while an environment has not applied it. **Both tables are
deployed, so adding a GSI is a live-data change and needs explicit approval before apply.**
Original description follows.


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

### 15. ~~EventBridge schedules have zero jitter~~ (**closed**)

`orchestration/event_bridge/extraction_schedule_client.py` now derives the window from
`_flexible_window(self._flexible_window_minutes)` rather than hardcoding `{"Mode": "OFF"}`.

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

### 18. ~~DuckDB-accelerated code paths silently never run in any deployed Lambda~~ (**closed in code**)

`duckdb` is now in the `Makefile`'s `lambda-package` pip install list, so the accelerated paths
execute in a freshly built package. Verify against a real deployment before treating the
performance claims that depend on it as proven. Historical description:

The Lambda deployment package's dependency list didn't include `duckdb`, even though it's a
declared project dependency. Every DuckDB-accelerated path
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

- **~~Two correlation-ID mechanisms coexist~~ (closed in code).** There is now one derivation,
  `observability/stage_execution.py::derive_correlation_id(run_id, replay_of_run_id)`, and a replay
  deliberately inherits the original run's id so one logical operation has one id. Handlers written
  before the scaffold still thread `run_id` explicitly; migrating each remaining one to
  `stage_execution(...)` is mechanical follow-up, not a correctness gap.
- **~~No shared scaffold for Lambda handler boilerplate~~ (closed in code).**
  `observability/stage_execution.py::StageExecution` is the template-method lifecycle: contextvars
  bound then cleared, metrics flushed, stage duration emitted, and a failure record written on both
  an exception and a hard Lambda kill (a `threading.Timer` watchdog fires just before the runtime
  kills the process). Pre-existing handlers have not all been migrated to it yet. Shared *test*
  fixtures are still duplicated across test modules — genuinely still open, and low priority.
- **Sage's Intacct/X3 query engines remain separate from the shared query-builder pattern** used by
  Salesforce/NetSuite/MySQL. This is a deliberate decision (Sage builds JSON/OData request bodies,
  not SQL text — forcing it through the shared template would be a leaky abstraction), not
  unfinished work. Listed here for visibility only.
- **Lambda memory sizing is flat across all entity types.** A per-entity `lambda_memory_mb`
  override (for DuckDB-heavy merges vs. small entities) was proposed and explicitly deprioritized.
  Low priority at current data volumes.
- **~~72 pre-existing mypy errors~~ (closed 2026-07-28).** The CI `typecheck` scope — now 17
  packages — is green, as is `bandit` (which had been red at `HEAD` on 20 pre-existing findings and
  hard-fails on any). DL-SEC-18's exit gate is "CI fully green including typecheck", and a
  permanently-red gate trains everyone to ignore gate failures. A mypy or bandit failure you see
  now is almost certainly newly introduced; confirm against `HEAD` before calling it pre-existing.

---

## Not yet attempted (needs a real deployment, not more code)

- **Pilot tenant onboarding** — running one real second tenant in parallel with the default tenant
  for a full week with no cross-tenant incidents.
- **Load testing** at the target scale (80–100 entities per tenant).
- **Staging and prod environments are not provisioned yet** — only dev has been deployed. Both
  validate cleanly (`terraform validate`) and are ready to bootstrap when needed.
- **Nothing from the SOW requirements programme has been applied to any AWS account.** 21 new
  DynamoDB tables, the WAF, the LF-Tags, the IAM tenant boundary, the per-stage DLQs, and the
  per-metric alarms all `terraform validate` cleanly in dev/staging/prod but exist only as code.
  The two data migrations (`make migrate-connections`, `make migrate-credentials`) must run
  **before** the corresponding code is deployed to each environment.
- **Client VPN is deliberately `enabled = false`** (`infrastructure/modules/client_vpn/`) pending the
  customer's answer on item 4's design decision. It is scaffolded, not chosen.
- **The enterprise semantic model is published as a draft, not activated.** DL-SEM-04 requires a
  named business owner's signature per KPI definition, and `scripts/seed_enterprise_semantic_model.py`
  deliberately does not forge one — collect signatures, then `--sign`, `--approve` (a different
  actor), and `--activate`.
- **DL-04 (AI agent runtime) and DL-05 (ML platform) are deferred by agreement**, not missed.

---

## Scale gaps found while sizing the DLQ (2026-07-29)

Full derivation and every threshold: [SCALE_AND_DLQ_THRESHOLDS.md](SCALE_AND_DLQ_THRESHOLDS.md).
The target these are measured against is 10–20 prod tenants × 5–12 sources × 100+ entities per
source at 12 months — roughly 24,000 runs/day and ~120 DLQ arrivals/day at the upper bound.

### 20. ~~Five of six pipeline stages enqueue nothing to any DLQ~~ (**closed 2026-07-29**)

`RunCoordinator.enqueue_dlq_entry` took a `failed_stage` argument and discarded it in favour of a
hardcoded `_DLQ_NAME`. The routing now lives in `contracts/dlq_routing.py`, which maps every
`PipelineStage` to its queue, and `observability/stage_execution.py` enqueues from its failure path —
so a stage gets replayability from the scaffold rather than from an author remembering.

`knowledge/twin_build_handler.py` and `serving_store/serving_store_loader_handler.py` were migrated to
`stage_execution` in the same change, because they were two of the five silent stages and had no
scaffold to route from.

`DlqStage.NOT_REPLAYABLE` is an affirmative value, not an omission: a deletion or an export must not
be automatically replayed, and the certificate is already the record. A handler that simply forgot to
declare a route is still a build error, because the field is required.

**Proven behaviourally**, not by wiring inspection: `observability/tests/test_stage_dlq_delivery.py`
drives each of the five previously-silent stages and reads its queue. The old unit tests all passed
while the defect was live, because they asserted a message reached the extraction queue — which it did.

### 21. ~~The nine per-stage DLQs have no producer and no consumer~~ (**closed 2026-07-29**)

Producers: item 20. Consumer: `aws_lambda_event_source_mapping.dlq_processor_stage_queues` binds the
processor to every stage queue via `for_each`, so `maxReceiveCount = 3` now counts (it only decrements
on *receive*). Each producing role gained `sqs:SendMessage` on `EdlStageDlq-*`, and the processor
role gained receive on the stage queues plus the terminal queue.

`observability/tests/test_dlq_routing_reconciliation.py` reconciles Terraform's `pipeline_stages`
against `DlqStage` **bidirectionally** — no queue without a producer, no producer without a queue —
in the same style as the alarm/emitter reconciliation, because an empty queue and a queue nothing can
write to look identical on a dashboard.

Still open, and deliberately so: the processor **records and notifies; it does not re-drive.** So
`EdlStageReplayExhausted` stays empty until an automatic replay exists. Item 24 covers the notification
half.

### 22. Scheduled runs bypass the burst buffer

`EdlPipelineTrigger.fifo` plus its Lambda exist to absorb simultaneous schedule fires — that is
their documented purpose. But `scripts/seed_schedules.py` sets the EventBridge Scheduler target to
the **state machine ARN**, so schedules call `StartExecution` directly and the queue is fed only by
the control-plane manual trigger route. Either point schedules at the queue or delete the queue and
its Lambda; do not keep documenting a buffer that is not in the path.

### 23. Concurrency wall at the 12-month target

Seeded crons are fixed times (`cron(0 2 * * ? *)`), so at 20 tenants that is 10,000–24,000
`StartExecution` calls in a single minute against an account-level token-bucket throttle. Those
throttles surface as failed *scheduler* invocations and land in the scheduler's own retry path,
which makes them **invisible in every DLQ dashboard**. If extraction averages ~10 minutes,
concurrent extraction Lambdas alone approach the default 1,000 per-region concurrency limit before
any other stage takes its share.

Mitigations in value order: deterministic cron jitter from a hash of `{tenant}#{source}#{entity}`
across a 4-hour window (~100 entities/min instead of 24,000 in one minute); a concurrency limit
increase with reserved concurrency partitioning per function; a wider window or per-tenant
staggering if the increase is refused.

### 24. The DLQ processor pages per message

One SNS publish per message. At the target, one tenant's ~1,200 entities failing means 1,200 pages.
It should publish a digest per `(stage, tenant)` per window, or stop publishing and let the
CloudWatch alarms be the single notification path — there are currently two paths for the same
event. The `(Stage, TenantCode)` custom metric and the "one tenant dominates" alarm are also not yet
implemented; because the alarm↔emitter reconciliation is bidirectional, the metric and its alarm
have to land in the same change.

## Checkov remediation and what it costs at apply time (2026-07-29)

The checkov job had never executed — its action SHA did not resolve — and `make iac-scan` passed
`--soft-fail` a value, which is a usage error, so the local target could not report a finding
either. Between them, 102 findings had accumulated with two controls certifying nothing. All 102
are now closed: 78 by real change, 24 by an inline skip carrying its reason.

None of it is applied. These are the consequences the next `terraform apply` will produce, and each
is deliberate rather than incidental:

- **Dev RDS becomes Multi-AZ** and gains deletion protection. The conversion is not instant and
  roughly doubles that instance's cost. Tearing dev down now needs an explicit
  `deletion_protection = false` apply first.
- **Performance Insights is enabled on `db.t3.micro`.** This was not verifiable offline. If that
  instance class does not support PI, the dev apply fails on this attribute — check `terraform plan`
  output before assuming the apply is clean, and either bump the class or gate PI on it.
- **Six buckets start replicating cross-region**, each with a replica bucket and a replica-region
  CMK. Ongoing replica storage plus per-object transfer. Replication is not retroactive: objects
  written before the apply are not copied, which matters if this is ever relied on for recovery.
- **Seven more Lambdas move inside the VPC.** Viable because the S3 and DynamoDB gateway endpoints,
  the interface endpoints and NAT already exist — but an environment that trims those endpoints
  breaks every outbound call from these functions.
- **Log retention goes from 30/90 to 365 days everywhere**, which is a storage cost that scales with
  volume.
- **Twelve new SQS DLQs** appear, one per Lambda module, named `EdlStageDlq-*` so the existing
  `sqs:SendMessage` grant covers them.

Two things remain deliberately open rather than fixed:

- **Code signing is `Warn`, not `Enforce`.** `make lambda-deploy` uploads an unsigned zip, so
  Enforce would reject every deployment this repo can perform. Closing this means signing the
  artefact in the build; the signing profile version ARN is already an output for that purpose.
- **Secrets Manager rotation on the two Sage secrets and the Redshift connection secret** is
  recorded as an inline exception, not implemented. All three are credentials for systems this
  platform does not operate, so rotation means a human obtaining new credentials from that vendor —
  there is no API to call, and a rotation schedule without working rotation logic would fail on
  every run while looking like a working control. The compensating control is the deployed
  `credential_expiry_notifier` (SEC-6), which sweeps daily and alerts before expiry.
