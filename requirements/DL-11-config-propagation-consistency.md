# DL-11 — Configuration Propagation and Runtime Consistency

**SOW clauses:** §3.1, §3.5, §4, §9, §12, §13 · **Priority:** P0 · **Owner repo:** DataLake

---

## Objective

Define the runtime contract for configuration authored in `enterprise-platform` and consumed by the
DataLake: when a published change takes effect, how a single pipeline run stays internally
consistent while it does, what happens to data already processed under the previous configuration,
and how the authoring system learns whether the change actually landed.

The authoring half of this is `EP-12`. Neither half is useful alone: the console can already write a
config, but nothing tells it whether the runtime consumed it, and nothing guarantees that one run
uses one configuration.

## Current state (verified 2026-07-28, against source in both repositories)

The integration is shared-datastore — the config service writes directly into the DataLake's
DynamoDB tables, S3 config prefixes, and Secrets Manager through the shared `edl_shared_contracts`
CodeArtifact package. Substantial parts already work correctly:

| Mechanism | Evidence | Verdict |
|---|---|---|
| Draft/published separation with two-phase publish — `begin_publish(expected_draft_version)` then `mark_publish_failed(revert_status="draft")` on error | `entity_selection_service.py:137-144` | Correct. A concurrent publish loses with `ConflictError`; a failure never leaves the shared contract published while the registry shows draft |
| Re-validation at publish, guarding staleness between draft-save and publish | `entity_selection_service.py:129-130` | Correct |
| Downstream effects fire **only** on publish, never on draft save | `schedule_repository.py` docstring | Correct, and an explicit documented rule |
| Schedule publish syncs EventBridge Scheduler idempotently, deleting on disable | `schedule_service.py::_apply_publish` → `ScheduleRepository.sync()` → `ExtractionScheduleClient` | Correct |
| Publishes bump versions rather than overwriting in place | `entity_resolution_service.py:120` — `next_version = f"v{registry_item['draft_version']}"` | Correct, and the reason the runtime cache below is safe today |
| Extraction config read fresh per run from DynamoDB | `ConfigurationRepositoryClient`, no cache | Correct |
| Credentials cached with a documented TTL bound (`DEFAULT_CREDENTIAL_CACHE_TTL_SECONDS = 3600`) | `credential_client.py:39` | Correct, but the bound is one hour and unalarmed |
| Lineage records the resolution config versions used | `publishing_shared.py` — `rule_set_version`, `survivorship_version` | Correct, and the foundation this requirement builds on |

What is **not** defined:

1. **No per-run version pinning.** `ResolutionConfigRegistry.load()` resolves the `latest` pointer
   fresh on every call (`resolution_config_registry.py:131-136`). Each stage resolves independently,
   so a publish between extraction and entity resolution splits one run across two configuration
   generations. Lineage records what was used, so it is detectable afterwards — nothing prevents it.
2. **No cache-invalidation signal.** `invalidate_entity_type()` exists
   (`resolution_config_registry.py:211`) and is called by nothing. Safe today only because publishes
   always bump versions and the in-process cache is keyed by version
   (`resolution_config_registry.py:138`). The moment any path overwrites in place, warm containers
   serve stale configuration silently and indefinitely.
3. **No in-flight run coordination.** Publishing while a run executes has undefined semantics.
4. **No runtime acknowledgement.** The publish response returns the instant the write succeeds. No
   record says which run first consumed the new version, or whether it failed at runtime.
5. **No reprocessing semantics.** A changed field mapping or survivorship rule affects future runs
   only. Historical golden records stay resolved under the old rules, so two records of the same
   entity can be governed by different generations of rules with nobody informed.
6. **No rollback of the published contract.** The registry versions drafts; reverting the artefact
   the runtime reads is not evidenced.
7. **No contract-compatibility gate.** `edl_shared_contracts` version skew between the service and
   the runtime could publish a config shape the runtime cannot parse.
8. **Retention conflicts with reprocessing.** `DL-PORT-03` expires raw data on a retention schedule.
   Reprocessing history under a new rule needs that data. Neither requirement acknowledges the other.

---

## Functional requirements

### Run-level consistency

- **DL-CFG-01** **Pin configuration versions at run start.** The pipeline trigger resolves every
  `latest` pointer once — extraction config version, field-mapping version, resolution config
  versions, relationship-rule version, semantic model version where a stage consumes it — and threads
  the resolved set through the Step Functions payload as an explicit `config_versions` object.
  Downstream stages consume the pinned versions and **never** resolve `latest` themselves. The
  threading pattern already exists: `infrastructure/modules/orchestration/main.tf:35,39` threads
  `entity_type.$` and `tenant_code.$` through every stage's `Parameters` block. Extend it, do not
  invent a second mechanism.
- **DL-CFG-02** **Fail closed on a missing pinned version.** If a pinned version no longer resolves —
  deleted, or an unexpected rollback — the stage fails with a distinguishable error rather than
  falling back to `latest`. A silent fallback would defeat the pinning.
- **DL-CFG-03** **Record the effective configuration set** in the run audit log and in every lineage
  record. Lineage already carries `rule_set_version` and `survivorship_version`; extend it to the
  full pinned set so any output can be traced to the exact configuration that produced it.

### Cache and propagation

- **DL-CFG-04** **Explicit cache invalidation contract.** Every config registry with an in-process
  cache declares its invalidation basis: version-keyed (invalidates naturally on a version bump),
  TTL-bounded (documented bound), or signal-driven. `invalidate_entity_type()` is either wired to a
  real signal or deleted — a dead invalidation API is worse than none, because it implies a
  guarantee that does not exist.
- **DL-CFG-05** **Publishes are always version-bumping.** Codify the behaviour
  `entity_resolution_service.py:120` already implements as a contract test on the DataLake side, so
  an in-place overwrite from any future writer is caught rather than silently serving stale config
  from warm containers.
- **DL-CFG-06** **Bounded propagation for TTL-cached configuration.** **Partially implemented:**
  the credential cache TTL is bounded at 300s and `force_refresh()` covers rotation outright
  (`connector_runtime/credential_client.py`). **Still open:** the observed propagation lag is not
  emitted as a metric for the credential cache — `ConfigPropagationLagSeconds` is produced only by
  the effective-config path. The credential cache's one-hour
  TTL is the current worst case and is unalarmed. Reduce it to a defensible bound, emit the observed
  propagation lag as a metric, and provide a forced-refresh path for the case that matters most — a
  rotated or corrected credential should not wait an hour.

### Publish and in-flight runs

- **DL-CFG-07** **In-flight run coordination.** A publish affecting an entity with a run in flight
  is permitted but recorded, and the affected run is annotated so its output is attributable. Where
  a capability cannot tolerate a mid-flight change — a resolution rule change during entity
  resolution — the publish is queued to apply at the next run boundary rather than blocked, and the
  console is told (EP-CHG-03). Blocking publishes on run state would make the console unusable at
  scale; annotating and deferring is the correct trade.
- **DL-CFG-08** **Runtime acknowledgement record.** A queryable record per capability per tenant
  stating the currently-effective version, the run that first consumed it, and the timestamp. This is
  what turns "published" into "in effect" for EP-CHG-02.
- **DL-CFG-09** **Rollback of the published contract.** Repoint `latest` to a prior version as a
  single audited operation, with the same maker-checker treatment as a publish. Prior versions are
  retained, which the versioned S3 layout already provides.

### Reprocessing

- **DL-CFG-10** **Declare a reprocessing policy per capability.** Each configuration capability
  declares whether a change is **apply-forward** (affects future runs only) or
  **reprocess-eligible** (historical data may be recomputed under the new configuration). Proposed
  defaults, to be confirmed with the business owner per capability:

  | Capability | Default | Rationale |
  |---|---|---|
  | Entity selection, schedules, credentials, sync strategy | apply-forward | No historical meaning |
  | Field mappings | reprocess-eligible from curated | Changes the canonical shape of existing records |
  | Quality policies | reprocess-eligible, report-only | Re-evaluating history produces exceptions, not data changes |
  | Entity resolution / survivorship | reprocess-eligible from curated | Otherwise golden records span rule generations |
  | Relationship rules | reprocess-eligible from analytics | Edges are derived, cheap to rebuild |
  | Semantic model / KPIs | apply-forward, but **restatement-flagged** | Definitions are read-time; a change restates every historical figure and must be announced, not silently applied |
  | Serving-store config | apply-forward with full reload | Physical projection |

- **DL-CFG-11** **Reprocessing execution.** Reprocess-eligible changes can be replayed over a bounded
  historical range as a distinct, resumable job reusing the DL-DQ-01 backfill orchestrator — not a
  second replay mechanism. Idempotent, chunked, cancellable, and pinned to the new configuration
  version for the whole job.
- **DL-CFG-12** **Retention/reprocessing reconciliation.** Reprocessing depends on source-layer data
  that DL-PORT-03 retention may have expired. Resolve explicitly: a capability declared
  reprocess-eligible sets a **minimum reprocessing window**, and the retention policy for its input
  layers must be at least that long. A retention policy shorter than a declared reprocessing window
  is a configuration error caught at publish, not discovered when a reprocess is attempted and the
  data is gone.
- **DL-CFG-13** **Restatement notice.** A semantic-model change that alters historical figures emits
  a restatement event carrying the metrics affected, the periods affected, and the before/after
  definition. Board and investor reporting (EP-APP-04, EP-RPT-08) depend on being able to explain a
  changed number, and this is the source of that explanation.

### Contract integrity

- **DL-CFG-14** **Config schema versioning and compatibility.** Every configuration artefact carries
  a schema version. The runtime declares the range it supports; a config outside that range fails
  closed with an actionable error. Paired with the publish-side gate in EP-CHG-05, so an incompatible
  config is refused at authoring time rather than at 03:00 in a scheduled run.
- **DL-CFG-15** **Contract test suite** for the shared `edl_shared_contracts` package, executed in
  both repositories' CI, asserting that what the service writes is what the runtime reads for every
  capability. This is the test that would have caught the whole class of defect this requirement
  addresses.

---

## Data model

| Store | Purpose |
|---|---|
| `EdlEffectiveConfig` (new) | PK `tenant_code`, SK `{capability}#{scope_id}#{entity_key}` — effective version, first-consuming run, effective-at timestamp, schema version |
| `EdlConfigRestatement` (new) | PK `tenant_code`, SK `{capability}#{event_id}` — metrics, periods, before/after definition, actor |
| `EdlRunAuditLog` (existing) | add the pinned `config_versions` object to the run record |
| Step Functions payload | add `config_versions` alongside the existing `tenant_code` and `entity_type` |
| Lineage records | extend to the full pinned set, not only the two resolution versions |

Reprocessing jobs reuse `EdlBackfillJob` (DL-DQ-01) with a `reprocess_reason` and the pinned target
configuration version — no second job store.

---

## Interfaces

```
GET  /tenants/{tc}/config/effective                       all capabilities, current effective versions
GET  /tenants/{tc}/config/effective/{capability}/{key}    one, with first-consuming run
POST /tenants/{tc}/config/{capability}/{key}/rollback     repoint latest, audited, maker-checker
POST /tenants/{tc}/config/{capability}/{key}/reprocess    bounded historical replay, returns a job
GET  /tenants/{tc}/config/restatements                    semantic restatement events
```

---

## Design and patterns

- **Pin-at-entry, consume-downstream.** Resolve `latest` once at the run boundary; every stage reads
  the pinned value. This is the same discipline the pipeline already applies to `tenant_code` — one
  authoritative resolution, threaded, never re-derived.
- **Declared invalidation basis** over ad-hoc caching. Each registry states its contract; the
  contract is tested.
- **Policy as data** for the reprocessing matrix — a capability declares its policy rather than
  each caller deciding.
- **Reuse the backfill saga** for reprocessing. A reprocess is a backfill with a different reason
  and a pinned config version; a second chunked-replay implementation would be pure duplication.
- **Eventual consistency, made observable.** Do not attempt distributed transactions across the two
  systems. Accept that propagation takes time, bound it, measure it, and surface it — which is what
  DL-CFG-06 and DL-CFG-08 do.
- Deliberately **not** blocking publishes on in-flight runs (unusable at scale) and **not** an
  event-bus invalidation broadcast (the version-keyed cache makes it unnecessary; adding a bus would
  be infrastructure carrying no guarantee the cache key does not already provide).

## Performance

- Version pinning adds one pointer resolution per run, not per stage — a net **reduction** in S3
  reads for multi-stage runs.
- The effective-config record is a single-item write per capability change, not per run; a run
  updates it only on first consumption of a new version.
- Reprocessing is set-based on the substrate and chunked, inheriting DL-DQ-01's resumability. A
  reprocess must never be a single unbounded job.
- Reducing the credential TTL (DL-CFG-06) increases Secrets Manager calls; size the bound against
  measured invocation rate rather than choosing a number.
- Contract tests run in CI, not at runtime.

## Security and OWASP

- **A01** — the effective-config and restatement APIs are tenant-scoped and capability-gated;
  rollback and reprocess require elevated capabilities distinct from publish.
- **A04** — pinning is a design control against a partially-applied configuration producing data
  nobody can explain. Rollback under maker-checker prevents a single actor silently reverting a
  governed definition.
- **A08** — schema-version compatibility and hash verification on config load; a config outside the
  supported range fails closed rather than being parsed leniently.
- **A09** — every publish, effective-version transition, rollback, reprocess, and restatement is
  audited with actor and correlation id. The restatement record is itself the audit evidence for a
  changed historical figure.
- **A05** — a retention policy shorter than a declared reprocessing window is rejected at publish
  (DL-CFG-12), turning a latent data-loss misconfiguration into an immediate validation error.

## Observability

`ConfigPropagationLagSeconds{capability}`, `ConfigVersionPinFailures`,
`ConfigVersionMismatchWithinRun`, `ConfigCacheStaleServed`, `EffectiveVersionTransitions`,
`ConfigRollbacks`, `ReprocessJobsStarted/Completed/Failed`, `ReprocessRowsRecomputed`,
`RestatementEventsEmitted`, `ConfigSchemaIncompatibilityRejections`,
`CredentialCachePropagationLagSeconds` — all alarmed.

`ConfigVersionMismatchWithinRun` must be **zero** once DL-CFG-01 lands; any non-zero value means the
pinning has been bypassed somewhere and is a paging alarm, not an informational one.

`ConfigPropagationLagSeconds` is the metric the console reads to answer "is my change live yet".

## Reuse and redundancy

- One pinning mechanism for every capability; the Step Functions `Parameters` threading pattern
  already in `orchestration/main.tf` is extended, not duplicated.
- One reprocessing engine — the DL-DQ-01 backfill orchestrator.
- One versioned-config repository pattern across entity resolution, relationship rules, field
  mappings, semantic models, and workflow definitions.
- One audit and lineage path; the effective-config record extends the existing run audit log rather
  than creating a parallel history.
- The contract test suite (DL-CFG-15) is shared between both repositories' CI from one source.

## Acceptance criteria

1. A publish executed mid-run does not change the configuration that run is using; the run's lineage
   shows one consistent pinned set, and `ConfigVersionMismatchWithinRun` stays zero.
2. A published change is consumed by the next run and the effective-config record shows which run
   first consumed it and when.
3. A pinned version that no longer resolves fails the stage with a distinguishable error, never a
   silent `latest` fallback.
4. `invalidate_entity_type()` is either wired to a real signal with a test, or deleted.
5. A contract test fails when the service writes a config shape the runtime cannot read.
6. A survivorship-rule change reprocesses a bounded historical range, resuming correctly after an
   induced failure, with all output pinned to the new version.
7. Publishing a retention policy shorter than a declared reprocessing window is rejected at publish.
8. A semantic KPI change emits a restatement event naming the affected metrics and periods.
9. A rollback repoints `latest`, is audited, and the next run uses the reverted version.
10. Credential propagation lag is measured and within the documented bound.

## Dependencies

- **EP-12** is the authoring and visibility half; the two ship together or neither is useful.
- DL-DQ-01 (backfill orchestrator) is reused for reprocessing.
- DL-PORT-03 (retention) must be reconciled per DL-CFG-12.
- DL-SEM-11 (semantic versioning) and DL-WF-01 (workflow definitions) adopt the same contract.

## Out of scope

- Distributed transactions across the two systems. Propagation is eventually consistent, bounded,
  and observable — not atomic.
- Real-time config push to warm Lambda containers. Version-keyed caching plus pinning makes it
  unnecessary.
