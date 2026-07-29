# DL-09 — Operations, Environments and Observability

**SOW clauses:** §2, §3.7, §9, §11, §12, §13, §15, §21, §22, §23.4 · **Priority:** P0 · **Owner repo:** DataLake

---

## Objective

Get the platform into staging and production, and operate it as a managed service — which is what
§22 converts the engagement into after implementation.

## Current state (verified 2026-07-28)

- **Dev only.** All eight Lambdas, the Step Functions state machine, control plane, DynamoDB tables,
  S3 buckets, SQS, EventBridge Scheduler, and the serving store are deployed in account
  `087972550871`. Two sources are live end-to-end.
- **Staging and production are not provisioned.** Both `terraform validate` cleanly; neither has an
  AWS account, state bootstrap, or deployment. Everything in the SOW that says "production-ready"
  is currently unmet for this reason alone.
- Observability is genuinely good for a dev platform: structured logging with `structlog`
  contextvars, X-Ray, metrics emitter, CloudWatch alarms, DLQ with a processor Lambda, immutable run
  audit log, and an alarm↔emitter reconciliation guard test that prevents dead alarms.
- Known operational gaps: four alarms have emitter contracts but their **emit calls are not wired at
  the runtime failure points**; metrics are not flushed in `finally` across the five stage handlers;
  a hard kill produces no failure record; there is no per-stage DLQ; the correlation id is not stable
  across replays; there is no Lambda-Insights memory alarm. (FR-F0.6, partial.)
- Two correlation-id mechanisms coexist (explicit `run_id` kwarg vs. `structlog.contextvars`).
- `duckdb` is a declared project dependency but is **absent from the Lambda package**, so every
  DuckDB-accelerated path silently falls back to the slower fully-materialising Python
  implementation (gap 18).
- No checkpoint-and-resume: a checkpointed extraction needs a manual re-trigger (gap 16).

---

## Functional requirements

### Environments

- **DL-OPS-01** **Provision staging**: AWS account, Terraform state bootstrap (state bucket, lock
  table, KMS key, plus the orphaned-resource check in `docs/DEPLOYMENT_GUIDE.md` Phase 1 Step 1.6),
  Lambda artefact upload, and apply in the documented module order.
- **DL-OPS-02** **Provision production**: same sequence. `terraform apply`/`destroy` against
  `infrastructure/environments/prod` is hard-blocked by a `.claude/settings.json` hook and requires
  explicit operator sign-off outside automated tooling — this is intentional and must not be
  weakened. Log retention is already 365 days in HCL.
- **DL-OPS-03** **Promotion policy**: staging sign-off gates production. Define the sign-off
  checklist explicitly (`docs/GO_LIVE_READINESS_CHECKLIST.md` exists — bind it to the gate).
- **DL-OPS-04** **Production validation (§9)**: post-deploy smoke suite covering one extraction per
  source, one full pipeline run, one semantic query, one dashboard load, and one agent turn — run
  automatically after every production deploy.
- **DL-OPS-14** **Release process (§15)**: semantic versioning, changelog, staged rollout, and a
  documented rollback for each deployable. Feature releases must not require a customer change
  order (§13), which means the release path has to be routine and low-ceremony.
- **DL-OPS-15** **Business continuity and disaster recovery (§23.4)**: documented and tested RTO/RPO,
  cross-region backup for S3 and DynamoDB (PITR is enabled; a restore has never been rehearsed),
  and a restore runbook. An untested backup is not a backup.

### Pipeline reliability (§3.7)

- **DL-OPS-05** **Complete FR-F0.6**: wire the four dead alarms' emit calls at their runtime failure
  points (`CircuitBreakerOpened`/`DDBFallback`, `InputValidationFailures`,
  `CredentialRetrievalFailures`); flush metrics in `finally` across all five stage handlers;
  guarantee a failure record on hard kill; add per-stage DLQ and replay; make the correlation id
  stable across replays; add the Lambda-Insights memory alarm.
- **DL-OPS-06** **Checkpoint-and-resume** (gap 16): automatic re-invocation from an extraction
  checkpoint. ASL's `Catch` does not feed error details into a retried task's parameters, so this
  needs either a `Choice`/`Wait` construct or a redesigned input contract carrying resume state.
  Today a checkpointed run silently waits for a human.
- **DL-OPS-07** **Standardise on one correlation-id mechanism** across the handlers. Two mechanisms
  with the same guarantee is a refactor hazard.
- **DL-OPS-08** **Add `duckdb` to the Lambda package** (gap 18), subject to a compatible prebuilt
  wheel for the runtime. Several documented performance improvements currently never execute.
- **DL-OPS-09** **Automatic retry and recovery** coverage review per §3.7 — confirm every stage has
  a defined retry policy, backoff, and terminal behaviour, and that DLQ replay is safe (idempotent)
  for each stage.
- **DL-OPS-10** **Failure notification** routing through the DL-06 workflow engine rather than the
  bespoke SNS paths, once that engine exists.
- **DL-OPS-11** **Continuous optimisation (§3.7, §12)**: close the performance items in the gap
  register — full-materialisation in `_load_raw_records` and the entity-resolution combined list
  (item 11), the analytics publisher's two in-memory copies (item 12), scan-based tenant list
  queries (item 13), the three-value hot GSI partition on the watermark table (item 14), and
  EventBridge schedule jitter (item 15).
- **DL-OPS-12** **Operational dashboards** for platform health: pipeline success rate, freshness per
  entity, DLQ depth, cost per tenant, alarm state. Distinct from the business dashboards in EP-05.

### Cost and capacity (§11)

- **DL-OPS-13** **Internal cost attribution per tenant.** §11 forbids per-token, per-query, and
  compute-credit pricing to the customer, so this is a **capacity-planning and margin tool, never a
  customer meter**. Gap register item 20 is re-scoped accordingly. Track records processed, compute
  seconds, storage, and inference tokens per tenant per period from the existing CloudWatch metric
  stream.

---

## Design and patterns

- **Infrastructure as code only** — no console changes in any environment. Drift detection in CI.
- **Template method** for the stage handler lifecycle via the shared scaffold (FR-F0.4), which also
  fixes the correlation-id divergence and the missing `finally` flush in one place rather than five.
- **Circuit breaker** and **bulkhead** per external dependency so one failing source cannot exhaust
  shared concurrency.
- **Idempotent replay** as a first-class property of every stage, not a per-stage accident.
- Environments are separate AWS accounts; the account boundary is the isolation mechanism, which is
  why table names carry no environment prefix. Keep it that way.

## Performance

- Address the four gap-register performance items above before load testing, not after.
- Load-test at the target scale — 80–100 entities per tenant — which has never been attempted.
- Per-entity Lambda memory override (previously deprioritised) becomes justified with report-style
  connectors and DuckDB-heavy merges.
- Set a freshness SLO per entity and alarm on breach; freshness is the metric customers actually
  perceive.

## Security and OWASP

- **A05** — security misconfiguration is the dominant risk in a first production deploy: run
  `terraform plan` review, drift detection, and a configuration baseline check as gates.
- **A08** — Lambda artefacts are versioned and checksummed; deploys reference an immutable S3
  object version, not a mutable key.
- **A09** — CloudTrail enabled in every environment with log-file validation; security log group
  separate with extended retention.
- Production apply remains hook-blocked; that guardrail exists precisely for long sessions that lose
  track of caution and must survive this work.

## Observability

Every requirement in this document adds an **emitted and alarmed** metric — the reconciliation guard
test enforces the pairing. Additions: `PipelineFreshnessSeconds{entity}`, `StageRetries{stage}`,
`DlqDepth{stage}`, `ReplaySuccessRate`, `CostPerTenantUsd{tenant}`, `LambdaMemoryUtilization`,
`DeploymentDurationMs`, `PostDeploySmokeFailures`.

Configuration propagation metrics — `ConfigPropagationLagSeconds`,
`ConfigVersionMismatchWithinRun`, `ConfigCacheStaleServed`, `PublishesNotYetEffective` — are
specified in **DL-11** and belong on the platform-operations dashboard (DL-OPS-12) alongside these.
`ConfigVersionMismatchWithinRun` is a paging alarm: any non-zero value means run-level config pinning
has been bypassed and a run's output cannot be attributed to one configuration.

## Reuse and redundancy

- One shared handler scaffold replaces boilerplate repeated across all five Lambda entrypoints and
  13+ test files (REU-01). Fold this into the FR-F0.1 handler rewrite rather than doing it twice.
- One Terraform module per concern, composed per environment — the existing structure is correct;
  staging and prod must not fork it.
- One alarm definition source paired with one emitter, guarded by the reconciliation test.

## Acceptance criteria

1. Staging and production provisioned, applied, and passing the post-deploy smoke suite.
2. Go-live readiness checklist signed for production.
3. A DR restore rehearsed end-to-end within the documented RTO.
4. A checkpointed extraction resumes automatically with no human action.
5. Zero dead alarms; reconciliation guard test green.
6. Load test at 80–100 entities per tenant passes within SLO.
7. `duckdb` present in the deployed package and the accelerated paths verified as executing.

## Dependencies

- Customer-provided AWS accounts for staging and production — a hard external blocker on the
  entire "production-ready" clause of the SOW.
