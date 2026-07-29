# Implementation Plan — DataLake Platform

Covers every requirement in `requirements/DL-01` … `DL-12`, **except `DL-04` (AI agent runtime) and
`DL-05` (ML platform), which are deferred to a separate team as of 2026-07-28** — see `README.md`. The enterprise-platform half of the
programme is planned in `/Users/deepnarayan/enterprise-platform/requirements/IMPLEMENTATION_PLAN.md`;
the two plans share the phase numbering so a given phase means the same calendar window in both.

**Baseline:** ~38% of SOW scope code-complete across both repos, ~20% deployed (dev only), 0%
production. **Target:** 100% of SOW-mandated capability in production.

---

## 0. Governing principles

1. **Nothing is "done" until it is in production.** The SOW's language is *production-ready*
   throughout. Dev completion is progress, not delivery.
2. **Additive-first.** New capability runs alongside existing behaviour behind a flag or a skippable
   stage until parity is proven. The `LoadServingStore` and `BuildTwin` stages are the pattern:
   conditional `Task`/`Pass`, failures caught, pipeline never fails because a new stage failed.
3. **Foundations before surfaces.** Shared abstractions land before the consumers that would
   otherwise each build their own. Deferring this trades a week now for a quarter later.
4. **One definition, many consumers.** Every metric resolves to the semantic layer. Every tenant key
   comes from `contracts/identifier_policy.py`. Every SQL statement comes from the compiler or the
   substrate. Violations of this rule are the mechanism by which the platform would drift back into
   inconsistent numbers.
5. **Security work is not a phase.** It is a gate on every phase. The one exception is the IAM tenant
   boundary, which is sequenced early precisely because retrofitting it later is materially harder.
6. **Comment density: one line maximum** above any function, class, or method. No prose docstring
   blocks. Explain *why*, never *what*.

---

## 1. Cross-cutting engineering standards

These apply to **every** task in every phase. They are not a checklist to run at the end.

### Architecture

- Module boundaries mirror the existing ones: each new module owns `<module>/tests/` and is
  registered in `pyproject.toml` `testpaths`, `[tool.coverage.run].source`, isort
  `known-first-party`, and the hatch wheel `packages` list. Omitting any one means the tests
  silently never run in CI — this exact gap existed for `analytics_publisher/tests` until 2026-07-07.
- Depend on `contracts/` and on interfaces, never on a sibling module's internals.
- New pipeline stages are **additive and skippable**, wired as conditional Step Functions
  `Task`/`Pass` with a `Catch`, so an unconfigured tenant is a no-op and a new-stage failure never
  fails the pipeline.
- Long-running or human-latency work belongs in Step Functions, not in a Lambda holding a request.
- Services that are not on the ingestion path (agent, exports, reconciliation) are separate
  deployables so their failure modes are isolated.

### Design patterns

| Pattern | Where it applies |
|---|---|
| Registry | connectors, sync strategies, pagination, processing engines, serving-store loaders, workflow actions, ML model types |
| Strategy | relationship rules, reconciliation comparators, SQL dialects, LLM providers, export formats |
| Adapter / Port | one module per source system; `LlmStructuredClient` as the provider port |
| Repository | every DynamoDB and S3-backed store |
| Template method | stage handler lifecycle, serving-store load lifecycle, report pipeline |
| Specification | quality checks, semantic filters |
| Saga with compensation | backfill chunks, deletion workflow |
| Circuit breaker / bulkhead | every external dependency — sources, LLM providers, webhook destinations |
| Command with idempotency key | every workflow action with an external effect |
| State machine | agent verification loop, onboarding gates |

Explicitly rejected: filesystem plugin discovery (registration stays import-time and explicit);
god-objects; dual-path legacy modes left in place after a migration; a general expression language
in the workflow engine; per-tenant AWS accounts.

### Performance

- **Set-based by default.** No new Lambda materialises a full dataset into a `list[dict]`. Use
  `processing_engine/`.
- Pagination and batching on every list endpoint and every source read.
- Async job model for anything exceeding a few seconds; return a job, never hold a request.
- Partition pruning on `tenant_code`, date, and `brand_code`; push aggregation to the engine.
- Caching with explicit invalidation, never TTL-only, for semantic results and catalogs.
- Establish a baseline measurement before optimising, and prove the delta after. Several
  performance improvements in this repo are documented as complete but never execute because
  `duckdb` is absent from the Lambda package (gap 18) — measurement would have caught that.

### Security and OWASP

Every task is assessed against the categories below; the relevant one is cited in the code comment.

| Category | Standing control in this codebase |
|---|---|
| A01 Broken access control | tenant scope injected server-side from verified claims; row-level predicate from the compiler; IAM conditions (DL-SEC-01) |
| A02 Cryptographic failures | KMS CMK per data class; TLS everywhere; SENSITIVE_PII `FULL_MASK`; no secret in a log or artefact |
| A03 Injection | all SQL from the compiler or the substrate's relation API; parameter binding; identifier allowlists |
| A04 Insecure design | maker-checker on high-blast-radius publishes; fixed agent tool surface; closed workflow action set |
| A05 Misconfiguration | fail-closed defaults; IaC only; drift detection; no public network paths |
| A06 Vulnerable components | dependency and image scanning with a remediation SLA |
| A07 Auth failures | JWT verification with audience; admin-scoped claims; per-tenant VPN certificates |
| A08 Integrity failures | hash-verified config bodies; digest-pinned artefacts; webhook signature verification |
| A09 Logging failures | audit record for every mutation; security log stream with extended retention |
| A10 SSRF | outbound host allowlists for sources, webhooks, and LLM providers |

### Monitoring and observability

- Every new stage, endpoint, and action emits an **emitted-and-alarmed** metric. The reconciliation
  guard test (`observability/tests/test_alarm_emitter_reconciliation.py`) enforces the pairing —
  a metric without an alarm or an alarm without an emitter fails CI.
- Structured logs with `run_id`, `tenant_code`, `correlation_id` bound via contextvars, cleared in
  `finally`. Skipping the `finally` leaks context into the next warm invocation — a previously-fixed
  real bug.
- Metrics flushed in `finally`, not on the success path.
- One correlation id per logical operation, stable across replays, propagated across service
  boundaries.
- Audit record for every mutation. Lineage record for every data movement.
- No PII and no parameter values in logs.

### Reuse and redundancy

- Before writing a second implementation of anything, extract the first. The threshold is two, not
  three — this codebase already carries the cost of that lesson in its gap register.
- Shared handler scaffold replaces the boilerplate repeated across five Lambda entrypoints and 13+
  test files.
- One identifier policy, one credential client, one raw-layer writer, one watermark repository, one
  versioned-config repository, one LLM port, one export path.
- Banned identifiers `helper` / `util` / `common` / `manager` are CI-enforced — name by domain
  concept.

---

## 2. Phased delivery

Sequencing is driven by dependency and by risk, not by visibility. Two things dominate the critical
path: **customer-supplied credentials and AWS accounts** (external), and **the semantic model**
(internal — five downstream capabilities are blocked on it).

### Phase 0 — Unblock (foundations, no new features)

Nothing here is customer-visible. Everything here is a prerequisite for something that is.

| Work | Requirements | Why first |
|---|---|---|
| Shared handler scaffold; one correlation-id mechanism | DL-OPS-05, DL-OPS-07 | Fixes the missing `finally` flush and the dead alarms in one place instead of five |
| Add `duckdb` to the Lambda package | DL-OPS-08 | Accelerated paths currently never execute; every performance claim downstream depends on this |
| Complete FR-F0.6 observability truth-up | DL-OPS-05 | Per-stage DLQ, guaranteed failure record, replay-stable correlation id, Lambda-Insights memory alarm |
| Tenant-prefix lineage and quality reports | DL-SEC-04 | Small, and blocks the IAM conditions that follow |
| Run the entity-config tenant-key migration with `--apply` per environment | DL-SEC-03 | **Release-blocking.** Deploying the new code first takes existing configs dark |
| Clear the 72–73 pre-existing mypy errors | DL-SEC-18 | A permanently-red gate trains everyone to ignore gate failures |
| Provision staging | DL-OPS-01 | Every "production-ready" clause depends on the promotion path existing |
| Source-connection model + scope-unit dimension + key migrations | DL-SCOPE-01..06, DL-SCOPE-09 | **Must precede the connector wave.** Ten connectors written against a connection-aware model cost almost nothing extra; retrofitting fourteen later is expensive, and the DynamoDB PK change is trivial at 36k dev rows and painful against live franchise data |
| Per-run config version pinning; effective-config record; cache-invalidation contract; shared-package contract tests | DL-CFG-01..05, DL-CFG-08, DL-CFG-14, DL-CFG-15 | Ten connectors and fourteen config surfaces are about to multiply config traffic. Pinning is far cheaper to add now than to retrofit across every stage later, and it pairs with EP Phase 0/1 |

**Exit gate:** CI fully green including typecheck; staging deployed and passing the smoke suite;
zero dead alarms; `ConfigVersionMismatchWithinRun` at zero with pinning proven under a mid-run
publish.

### Phase 1 — Security boundary and production environment

| Work | Requirements |
|---|---|
| IAM-enforced tenant isolation — audit mode, CloudTrail verification, then enforce | DL-SEC-01, DL-SEC-02 |
| Per-tenant credential paths + migration; replace the skipped placeholder test | DL-SEC-05 |
| ~~Admin authorization on tenant provisioning~~ — **withdrawn**; tenant/user/role/permission management belongs to the Identity API, not this system | DL-SEC-12 |
| WAF on the control plane | DL-SEC-13 |
| Exercise the live auth path end-to-end | DL-SEC-14 |
| Scope predicate builder, resolution scoping, twin boundary, adversarial isolation tests | DL-SCOPE-10..14, DL-SCOPE-17, DL-SCOPE-18 |
| Provision production; go-live checklist; DR rehearsal | DL-OPS-02, DL-OPS-03, DL-OPS-15 |
| Post-deploy smoke suite | DL-OPS-04 |

Phased carefully: add IAM conditions in audit mode first and verify with CloudTrail that no
legitimate access would be denied before enforcing. The default tenant must not break.

**Exit gate:** a principal scoped to tenant A is denied by IAM — not application code — when
reaching for tenant B's data. Production deployed. DR restore rehearsed within the documented RTO.

### Phase 2 — Sources (the long pole)

Runs in parallel with Phase 1 from the moment credentials arrive. This phase is gated by the
customer, so start credential collection on day one of the programme.

| Wave | Work | Requirements |
|---|---|---|
| 2a | Framework: rate limiting, sync strategies, pagination strategies, capability declaration, webhook receiver | DL-CONN-11, 13, 14, 15, 17 |
| 2b | **HubSpot** read (covers 3 of 12 rows) + Sage Intacct activation | DL-CONN-01, DL-CONN-12 |
| 2c | HubSpot write-back (§3.8 FMS) | DL-CONN-02 |
| 2d | WellSky, Maid Central, HouseCall Pro, SeniorPlace | DL-CONN-03, 05, 09, 10 |
| 2e | Google Ads/Analytics, Meta Ads, DialPad, ServMan Pro | DL-CONN-04, 06, 07, 08 |

Framework first, deliberately. Ten connectors built before the shared strategies exist means ten
bespoke rate limiters.

**Exit gate:** all ten sources extracting on schedule in production with non-zero rows.

### Phase 3 — Trust: quality, reconciliation, migration

Begins per source as soon as that source lands; does not wait for all ten.

| Work | Requirements |
|---|---|
| Backfill orchestrator with resumable chunks | DL-DQ-01 |
| Record-count, key-field, completeness, duplicate, referential, date validation | DL-DQ-02, 03, 10, 11, 12, 13 |
| Reconciliation to source, including financial sums | DL-DQ-04 |
| Attach quality policies to every entity; quality gate on promotion | DL-DQ-05, DL-DQ-15 |
| Exception records and reporting | DL-DQ-14 |
| Brand as a first-class dimension | DL-DQ-09 |
| Data dictionary generation; survivorship explainability | DL-DQ-06, 07, 08 |
| Performance gap closure (items 11–15) | DL-OPS-11 |
| Reprocessing policy matrix, execution on the backfill orchestrator, retention reconciliation, rollback | DL-CFG-09..12 |

Reprocessing lands here rather than in Phase 0 because it reuses the DL-DQ-01 backfill orchestrator —
building it earlier would mean building a second chunked-replay engine.

**Exit gate:** every production entity has a quality policy; a financial reconciliation matches
source; a corrupted batch is blocked by the gate and produces masked exception records; a
survivorship-rule change reprocesses a bounded historical range and resumes after an induced failure;
a retention policy shorter than a declared reprocessing window is rejected at publish.

### Phase 4 — Semantic model (the internal critical path)

Five capabilities are blocked on this: dashboards, reports, chat, ML features, and reconciliation's
financial comparators. It cannot be parallelised away.

| Work | Requirements |
|---|---|
| Engine: joins, time grain, fiscal calendar, filters, derived metrics | DL-SEM-01, 02, 07, 09 |
| Lineage, versioning, maker-checker, rollback, result cache | DL-SEM-10, 11, 12 |
| Author the enterprise entity model | DL-SEM-03 |
| Author the §4 KPI set with named owners and signed definitions | DL-SEM-04 |
| Ownership enforcement; generated methodology documentation | DL-SEM-05, DL-SEM-06 |
| KPI validation harness in CI and post-deploy | DL-SEM-08 |
| Restatement events on definition changes that alter historical figures | DL-CFG-13 |

The authoring work is **business-stakeholder-bound**, not engineering-bound. Start definition
workshops during Phase 2 so Phase 4 is implementation rather than discovery.

**Exit gate:** all §4 KPIs published and signed; a cross-entity query returns correct results
without the caller expressing a join; KPI validation green.

### Phase 5 — Consumption substrate

| Work | Requirements |
|---|---|
| Client VPN and BI network path | DL-SERV-01 |
| Per-tenant credential delivery and rotation | DL-SERV-02 |
| Tenant/entity onboarding into the serving store | DL-SERV-03 |
| Serving views generated from the semantic model | DL-SERV-04 |
| Incremental loads with merge strategy; performance sizing | DL-SERV-05, DL-SERV-06 |
| Replace the Athena/Glue wildcard grant with LF-Tags | DL-SERV-07 |
| Serving-store engine constraint for partitioned tenants (MySQL has no native RLS); aggregate suppression | DL-SCOPE-15, DL-SCOPE-16 |
| Resolve the curated Glue registration ambiguity | DL-SERV-08 |
| Row-level security: department, executive, brand, franchise | DL-SEC-09, 10, 11 |

DL-SERV-01 needs a **customer decision** on VPN topology — raise it in Phase 0, not Phase 5.

**Exit gate:** a BI tool connects from outside AWS and returns rows; a franchisee sees only their
rows; no principal can query another tenant's tables.

### Phase 6 — Intelligence ⏸ DEFERRED (separate team)

> Deferred 2026-07-28. `DL-04` and `DL-05` are assigned to a separate team; this phase is out of the
> active programme. The table remains for that team's sequencing. **Phase 7 no longer depends on
> Phase 6** — `DL-WF-02`'s ML-signal trigger contract is still defined so DL-05 plugs in later
> without a schema change, but ships with no producer; alerting is driven by `DL-DQ-14` exception
> records and `DL-SEM` threshold conditions instead.

| Work | Requirements |
|---|---|
| Bedrock LLM adapter behind the existing port | DL-AGENT-01 |
| Agent service, sessions, streaming, memory | DL-AGENT-02, 03, 04, 05 |
| Injection defences, cost guards, data-use guarantees | DL-AGENT-06, 07, 08 |
| NL evaluation harness; explainability; report generation | DL-AGENT-09, 10, 11 |
| Feature store with point-in-time correctness | DL-ML-01, DL-ML-02 |
| Model registry, training orchestration, inference | DL-ML-03, 04, 05 |
| Forecasting, segmentation, risk, anomaly detection | DL-ML-06, 07, 08, 11 |
| Model governance and monitoring | DL-ML-09, DL-ML-10 |

**Exit gate (deferred team):** a grounded, cited chat answer matching the semantic query exactly; a
forecast visible as an ordinary semantic measure; drift detection triggering a retrain
recommendation.

### Phase 7 — Automation

| Work | Requirements |
|---|---|
| Workflow definitions, triggers, conditions, actions | DL-WF-01, 02, 03, 04 |
| Approval tasks, exception management | DL-WF-05, DL-WF-06 |
| Idempotency, execution history, failure handling, dry-run | DL-WF-07, 08, 09, 10 |
| Retire the bespoke DLQ-alert and credential-expiry Lambdas onto the engine | DL-WF reuse clause |
| Checkpoint-and-resume for extraction | DL-OPS-06 |

**Exit gate:** a business user authors and runs a workflow with no deploy; a duplicate trigger
produces exactly one notification; the bespoke alerting Lambdas are retired.

### Phase 8 — Compliance and exit readiness

| Work | Requirements |
|---|---|
| Export service: CSV, JSON, Parquet | DL-PORT-01 |
| Transition package; reproduction test; infrastructure hand-over | DL-PORT-02, 09, 10 |
| Retention attachment; deletion workflow and certificate; legal hold | DL-PORT-03, 04, 05 |
| Subprocessor register; PHI gating | DL-PORT-07, DL-PORT-08 |
| Incident response; SOC 2 readiness; vulnerability management | DL-SEC-16, 17, 18 |
| Authorization test matrix; external penetration test | DL-SEC-15 |
| Internal cost attribution | DL-OPS-13 |

**PHI gating (DL-PORT-08) is out of sequence and must land before Phase 2d** — WellSky is home care
and SeniorPlace is senior placement. Both are live PHI risks, not theoretical.

**Exit gate:** a deletion rehearsal produces a complete verified certificate; a transition package
reproduces one entity independently; penetration test findings remediated or accepted.

---

## 3. Dependency map

```
Phase 0 ─┬─> Phase 1 (security + scope + prod) ─┬─> Phase 5 (serving + scope RLS) ──┐
         │                                       │                                  │
         └─> Phase 2 (sources) ─> Phase 3 (quality) ─> Phase 4 (semantic) ──────────┴─> Phase 7 (workflow)
                                                              │
                                                              └─> EP Phase 4 (dashboards)

Phase 6 (agent + ML) ⏸ DEFERRED — no active phase depends on it.
Phase 8 runs continuously from Phase 1; PHI gating pulled forward to before Phase 2d.
DL-12 (connections + scope) sits in Phase 0/1 and gates Phase 2 — connectors must be
connection-aware before they are written.
```

Hard external blockers, all owed by the customer under §19 and all worth escalating on day one:

1. Source credentials and API access — gates Phase 2 entirely.
2. Staging and production AWS accounts — gates Phases 0 and 1.
3. VPN topology decision — gates Phase 5.
4. Business owners for KPI definitions — gates Phase 4.
5. BAA execution if PHI-bearing sources onboard — gates WellSky and SeniorPlace.

---

## 4. Verification per phase

Run before any phase is declared complete:

```bash
.venv/bin/ruff check .
.venv/bin/pytest -q                      # 80% coverage gate
.venv/bin/bandit -r . --exclude .venv,tests,dist -c pyproject.toml
.venv/bin/mypy -p connector_runtime -p transformation -p entity_resolution \
  -p analytics_publisher -p orchestration -p observability -p watermark_management \
  -p schema_management -p contracts -p governance
cd infrastructure/environments/<env> && terraform init -backend=false && terraform validate
```

Never run bare `mypy .` — the `dist/lambda-build/typing_extensions.py` shadow and the
`scripts/generate_presentation.py` vs. `pptx/generate_presentation.py` module collision make it fail
for reasons unrelated to any change.

Plus, per phase: `tests/test_tenant_isolation.py` green with a new case for every isolation
mechanism added; the alarm↔emitter reconciliation test green; the post-deploy smoke suite green in
the target environment.

`terraform apply`/`destroy` against `infrastructure/environments/prod` and `git push --force` are
hard-blocked at the tool level by `.claude/settings.json`. That is intentional and must not be
weakened for convenience during this programme.

---

## 5. Risk register

| Risk | Impact | Mitigation |
|---|---|---|
| Customer credentials arrive late | Phase 2 slips, and everything downstream with it | Escalate day one; build the framework and stub adapters against recorded fixtures meanwhile |
| KPI definitions are contested between departments | Phase 4 stalls; dashboards blocked | Start workshops in Phase 2; publish contested definitions as `draft` and ship the uncontested set |
| IAM tenant-boundary rollout breaks the default tenant | Production incident | Audit mode plus CloudTrail verification before enforcement; per-environment rollout |
| FR-F0.1 set-based re-platform is attempted big-bang | High-risk rewrite of the most heavily-tested code | One stage at a time behind the existing contract, parity-tested before deleting the in-memory path. Never big-bang |
| Reconciliation exposes long-standing source discrepancies | Programme perceived as introducing errors | Frame reconciliation output as discovery; report variance before automating anything on top of it |
| Serving-store VPN decision deferred | Phase 5 blocked; no BI access at all | Force the decision in Phase 0 with a written recommendation for Client VPN |
| PHI lands before a BAA | Compliance breach | Hard onboarding gate (DL-PORT-08) before WellSky and SeniorPlace |
| Two permission vocabularies persist across repos | Divergent enforcement, audit failure | Reconcile in Phase 1 jointly with EP-RBAC-01 |
| A config path starts overwriting in place instead of version-bumping | Warm containers serve stale config silently and indefinitely; the version-keyed cache stops protecting anything | DL-CFG-05 contract test on the DataLake side, so the guarantee is enforced rather than assumed |
| Config pinning bypassed by a new stage | One run spans two config generations; output unexplainable | `ConfigVersionMismatchWithinRun` as a paging alarm, not informational |
| Reprocessing attempted after retention expired the input data | Historical data permanently ungoverned by the new rule | DL-CFG-12 validates retention against declared reprocessing windows at publish |
| A KPI definition change silently restates board figures | A number changes between two readings with no explanation available | DL-CFG-13 restatement events, surfaced to owners by EP-CHG-09 |
| Scope isolation built as a conditional franchise flag | One wrong flag runs a franchise tenant wide open with no error anywhere | DL-SCOPE-02 degenerate-not-conditional design; the predicate is always applied, `single` tenants just have one unit |
| Empty scope claim read as "no filter" | Total cross-unit exposure through a single defect | DL-SCOPE-14 empty-means-deny, with a dedicated test; EP-SCOPE-06 rejects empty grants at save |
| Connector wave ships before DL-12 | Fourteen connectors retrofitted; PK migration against live franchise data | DL-SCOPE work sequenced into Phase 0, ahead of Phase 2 |
| Cross-franchise merge in entity resolution | Golden records contain two franchisees' data with queryable provenance; unfixable by filtering | DL-SCOPE-12 scope-partitioned resolution, defaulting to scope-unit for partitioned tenants |
| Deferred DL-04/DL-05 quietly re-enter scope via a stub | An unowned half-built LLM or ML path with no team behind it | Deferral notes are explicit that features degrade or drop, never stub a provider |
