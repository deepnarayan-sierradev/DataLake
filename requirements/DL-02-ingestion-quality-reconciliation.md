# DL-02 — Ingestion Quality, Historical Migration and Reconciliation

**SOW clauses:** §3.3, §3.5, §3.6, §9, §18 · **Priority:** P0 · **Owner repo:** DataLake

---

## Objective

Prove that what lands in the lake matches the source system, and keep proving it on every run. The
SOW makes reconciliation an explicit, repeated obligation (§3.3, §3.5, §3.6, §9, §12); today the
platform has no reconciliation capability of any kind.

## Current state (verified 2026-07-28)

- `LoadType.FULL` exists in `contracts/entity_configuration_contract.py` — full-load extraction is
  possible, but no historical migration has been executed and there is no migration tooling.
- `transformation/quality_evaluation/quality_policy_evaluator.py` implements `NullCheck`,
  `RangeCheck`, `PatternCheck`, `AllowedValuesCheck`, severities, and a `QualityReport`. **No entity
  has a quality policy attached** (gap register item 9).
- Normalisation is real: field mapping, type standardisation, entity resolution with a matching
  engine, survivorship policy, golden-record publication, `contributing_source_records` provenance.
- **A repository-wide search for "reconcil" returns only an unrelated CloudWatch alarm test.** There
  is no record-count validation, no referential-integrity check, no financial reconciliation, no
  cross-system reconciliation, and no exception reporting.

---

## Functional requirements

### Historical migration (§3.3)

- **DL-DQ-01** A **backfill orchestrator** that executes a bounded historical load for an entity as
  a sequence of date-ranged chunks, each an independent, resumable Step Functions execution with its
  own watermark checkpoint. Must survive Lambda timeout, be idempotent on replay, and be resumable
  from the last completed chunk — never restart a multi-million-row backfill from zero.
- **DL-DQ-02** **Record-count validation** per chunk: source count (via the connector's count
  capability or a `COUNT` query) compared against raw, curated, and golden counts. Mismatch beyond a
  configured tolerance blocks promotion of that chunk and raises an exception record.
- **DL-DQ-03** **Key-field validation**: for a configurable sample plus all records in a configurable
  set of key fields, compare source values to curated values field-by-field. Report per-field match
  rate.
- **DL-DQ-04** **Reconciliation to source and cross-system**: a scheduled reconciliation job that
  re-queries the source for a period, compares aggregates (count, sum of declared measures,
  min/max of the watermark field) against the analytics layer, and produces a signed reconciliation
  report. Financial entities (AR invoices, AP bills, revenue) must reconcile on monetary sums, not
  only counts (§3.6 "revenue and financial reconciliation").

### Data quality (§3.6)

- **DL-DQ-05** **Attach quality policies to every configured entity.** This closes gap register
  item 9. No entity reaches production without a policy, enforced by a pre-promotion check.
- **DL-DQ-10** **Completeness checks**: required-field population rate per entity per run against a
  threshold.
- **DL-DQ-11** **Duplicate detection** as a first-class quality check, distinct from entity
  resolution: report intra-source duplicate rate on the declared natural key before resolution runs.
- **DL-DQ-12** **Referential integrity checks**: declared foreign-key relationships between entity
  types validated after entity resolution — orphan rate reported and thresholded.
- **DL-DQ-13** **Date and period validation**: future-dated records, records before a configured
  epoch, and period-boundary anomalies flagged.
- **DL-DQ-14** **Exception reporting**: every quality violation, count mismatch, and orphan produces
  a structured exception record in a queryable store with tenant, entity, run, rule, severity,
  sample offending keys (PII-masked), and resolution state. Exceptions are the input to the
  exception-management workflow in DL-06.
- **DL-DQ-15** **Quality gate on promotion**: severity `ERROR` violations block the analytics publish
  stage for that entity and route to the DLQ with a distinguishable reason; `WARN` publishes and
  alerts. Configurable per entity, defaulting to block.

### Normalisation completeness (§3.5)

- **DL-DQ-06** **Transformation rule documentation is generated, not written**: the field-mapping and
  survivorship configuration renders to a human-readable data dictionary artefact per entity per
  version, published to S3 and exposed through the config API for EP-04 to display.
- **DL-DQ-07** **Conflicting-value reconciliation is explainable**: for every golden field the
  survivorship decision records which source won and under which rule. Extend the existing
  `field_provenance` to carry the rule id, not only the source.
- **DL-DQ-08** **Common identifier issuance is auditable**: golden-id assignment history retained so
  a merge or split can be traced and reversed.
- **DL-DQ-09** **Multi-brand separation**: brand is modelled as a first-class dimension on every
  entity for the multi-brand franchise structure (Maid Brigade, Pacific Lawn, Executive Home Care,
  Brothers Gutters, Shine, Grasons, Assisted Living Locators), distinct from `tenant_code`. Brand
  drives row-level access (DL-SEC-11) and dashboard filtering.

---

## Data model

| Store | Purpose |
|---|---|
| `EdlDataQualityException` (new) | PK `tenant_code`, SK `{run_id}#{rule_id}#{seq}`; GSI on `entity_id`+`detected_at`; TTL configurable |
| `EdlReconciliationReport` (new) | PK `tenant_code`, SK `{entity_id}#{period}#{run_id}` — counts, sums, variance, verdict |
| `EdlBackfillJob` (new) | PK `tenant_code`, SK `{entity_id}#{job_id}` — chunk plan, per-chunk state, resume pointer |
| S3 | `{tenant_code}/quality-reports/{source_id}/{entity_id}/{run_id}/` — **note the new tenant prefix**, closing gap register item 10 |
| S3 | `{tenant_code}/reconciliation/{entity_id}/{period}/report.json` |
| S3 | `{tenant_code}/data-dictionary/{entity_id}/{version}.md` |

Brand is added to the curated and analytics schemas as `brand_code`, validated against a tenant
brand registry, and included as a partition column on analytics where cardinality permits.

---

## Design and patterns

- **Specification pattern** for quality checks — each check is a composable predicate object, which
  the existing evaluator already approximates; extend rather than replace.
- **Strategy** for reconciliation comparators (count, sum, hash, sampled field compare).
- **Saga / chunked orchestration** for backfill: each chunk is an independent transaction with a
  compensating action (delete chunk partition) so a failed chunk never leaves partial state.
- **Repository** for exceptions and reconciliation reports.
- Reconciliation runs as a **separate Step Functions state machine**, not a stage inside the
  extraction pipeline — it operates on a period, not a run, and must not couple to run latency.

## Performance

- All reconciliation aggregates are computed **set-based on the processing engine substrate**
  (`processing_engine/`) against Parquet — never by materialising records in Python. This is the
  substrate FR-F0.1 introduced; reconciliation is its first non-additive consumer.
- Sampling is deterministic (hash of natural key modulo N) so successive runs sample the same rows
  and drift is detectable.
- Backfill chunk size is derived from observed rows-per-second, not a fixed constant.
- Quality evaluation moves to a columnar pass over the Parquet batch rather than per-record Python
  iteration; this also removes one of the two full-materialisation paths in gap register item 11.

## Security and OWASP

- **A01** — exception records are tenant-partitioned at the key level from creation.
- **A02** — offending-key samples in exception records are masked through the existing
  classification policy before persistence; SENSITIVE_PII never appears, even masked.
- **A03** — reconciliation SQL is generated by the substrate's parameterised relation API with
  identifier allowlisting from the semantic/field-mapping config.
- **A04** — the quality gate fails closed: an evaluator error blocks promotion rather than passing.
- **A09** — every reconciliation verdict is an immutable audit record with the rule version used.

## Observability

Metrics, all alarmed: `QualityViolations{severity}`, `QualityGateBlocks`, `CompletenessRate`,
`DuplicateRate`, `OrphanRate`, `ReconciliationVariancePct`, `ReconciliationFailures`,
`BackfillChunksCompleted`, `BackfillChunksFailed`, `BackfillRowsPerSecond`.

A reconciliation variance beyond tolerance is a **paging** alarm, not an informational one — an
undetected revenue mismatch is the highest-consequence failure mode in this platform.

Every exception record carries the correlation id of the run that produced it, so a dashboard can
pivot from an alarm to the offending rows.

## Reuse and redundancy

- One quality evaluator, extended — do not build a second checker for reconciliation.
- Reconciliation comparators reuse the semantic layer's measure definitions (DL-SEM-04) so
  "revenue" reconciles against the same definition the dashboards show. Never define revenue twice.
- The exception repository is shared by DL-02, DL-06 (exception workflows) and DL-09 (operations).
- Data-dictionary generation reuses the field-mapping config already in S3; no parallel metadata.

## Acceptance criteria

1. A historical backfill of the largest available source entity completes, resumes correctly after
   an induced failure, and reconciles to source within tolerance.
2. Every production entity has an attached quality policy; the pre-promotion check rejects one
   without.
3. A deliberately corrupted record set triggers the quality gate, DLQs the run, and produces
   exception records with masked samples.
4. A monthly financial reconciliation report for AR invoices matches Sage Intacct totals.
5. Quality-report and lineage S3 keys are tenant-prefixed (gap register item 10 closed).

## Dependencies

- DL-01 (sources must exist to reconcile against).
- DL-SEM-04 (measure definitions) for financial reconciliation.
- FR-F0.1 substrate for set-based evaluation.
