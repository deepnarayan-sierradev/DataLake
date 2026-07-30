# DL-05 — Machine Learning Platform

**SOW clauses:** §6.2, §12, §13, §18, §23.3 · **Priority:** ⏸ **DEFERRED** · **Owner repo:** DataLake

> ## ⏸ Deferred — separate team, 2026-07-28
>
> This requirement is **out of the current programme's delivery scope** and assigned to a separate
> team. The specification below is complete and remains authoritative — it is not withdrawn, and no
> part of it is superseded.
>
> **Binding on whoever picks it up:** features derive from semantic-layer metrics rather than
> re-implementing them (`DL-ML-01`), training sets are point-in-time correct (`DL-ML-02`),
> predictions are written as **ordinary analytics datasets** so dashboards and the semantic layer
> consume them with no ML-specific integration (`DL-ML-05`), and training roles are tenant-scoped
> and — once `DL-12` lands — scope-unit-aware. Those constraints are what make this additive.
>
> **What the remaining programme must not assume:** no predictions exist as data, so
> `EP-DASH-06` predictive widgets have no source; `DL-WF-02`'s ML-signal trigger has no producer;
> `DL-ML-11` anomaly detection is unavailable, so trend alerting is threshold-based only.
>
> **Sequencing note for the separate team:** `DL-SEC-01` (IAM tenant boundary) and `DL-12`
> (scope-unit isolation) should both be in place before tenant-scoped training roles are built, or
> the isolation model will need reworking.

---

## Objective

Deliver a machine-learning environment capable of supporting the SOW's named use cases —
forecasting, predictive analytics, trend analysis, customer/franchise segmentation, risk
identification, performance prediction — as a managed platform capability, not a data-science
side project.

## Current state (verified 2026-07-28)

**Nothing exists.** A search across all three codebases for `sagemaker`, `sklearn`, `xgboost`,
`forecast`, and related terms returns zero application matches. There is no training pipeline, no
feature store, no model registry, no inference path, no monitoring. This is a from-zero build.

What the platform *does* provide as foundation: golden records, the analytics layer in Parquet, the
set-based processing substrate, the semantic layer's metric definitions, and the twin's entity
relationships. A feature store built on these inherits tenant scoping and governed definitions for
free — that is the design constraint that matters most here.

---

## Functional requirements

### Foundation

- **DL-ML-01** **Feature store.** Offline features materialised from the analytics and twin layers
  as tenant-partitioned Parquet with point-in-time-correct joins; online features for low-latency
  inference in the serving store or DynamoDB. Feature definitions are **derived from semantic-layer
  metrics and dimensions wherever one exists** — a feature named `revenue_trailing_90d` must resolve
  to the semantic `Revenue` metric, never a re-implementation.
- **DL-ML-02** **Point-in-time correctness.** Training sets are assembled as-of a timestamp so no
  future information leaks into a training row. This is non-negotiable for the forecasting and risk
  use cases and is the single most common failure in platforms of this shape.
- **DL-ML-03** **Model registry.** Versioned models with training dataset reference, feature-set
  version, hyperparameters, metrics, approval state, and lineage back to the source runs.
- **DL-ML-04** **Training orchestration.** SageMaker Training Jobs invoked from a Step Functions
  state machine, parameterised by tenant, model type, and feature set. Scheduled retraining and
  on-demand training from the config console.
- **DL-ML-05** **Inference.** Two modes: batch scoring written back to the analytics layer as a
  first-class dataset (so predictions are queryable through the semantic layer and appear on
  dashboards like any other measure), and real-time inference behind a SageMaker endpoint for
  interactive use. Batch is the default; real-time only where a use case requires it.

### Use cases (§6.2)

- **DL-ML-06** **Forecasting**: revenue, collected revenue, and job/lead volume by brand,
  franchisee, and period. Baseline model plus a seasonal model; the platform must support both and
  select by backtest score, not by preference.
- **DL-ML-07** **Segmentation**: customer and franchisee clustering over behavioural and financial
  features, with stable cluster identity across retrains (cluster drift is a reporting hazard).
- **DL-ML-08** **Risk and performance prediction**: franchisee under-performance risk, contract
  churn/non-renewal risk, and AR collection risk. Each emits a calibrated probability plus the top
  contributing features, because an uninterpretable risk score will not be acted on.
- **DL-ML-11** **Trend and anomaly detection** over KPI time series, feeding the alerting workflow
  in DL-06 — an anomalous drop in collected revenue should raise an operational alert, not wait for
  a monthly review.

### Governance

- **DL-ML-09** **Model governance (§23.3).** Customer data is never used to train a foundational or
  general-purpose model, and never crosses tenants — training jobs are tenant-scoped by input prefix
  and by execution role. Models are tenant-specific artefacts by default; a cross-tenant model may
  exist only on aggregated, de-identified inputs and requires explicit approval, matching §23.2's
  carve-out.
- **DL-ML-10** **Model monitoring and continuous improvement (§12).** Data drift, prediction drift,
  and accuracy-against-actuals tracked per model per period; degradation triggers a retrain
  recommendation and an alert. Every deployed model has an owner and a review cadence.

---

## Data model

| Store | Purpose |
|---|---|
| S3 `{tenant_code}/features/{feature_set}/{version}/as_of=…/` | offline feature store, Parquet |
| S3 `{tenant_code}/ml/training-sets/{model}/{version}/` | immutable training snapshots |
| S3 `{tenant_code}/ml/predictions/{model}/{version}/score_date=…/` | batch scores |
| `datalake-feature-sets-<env>` (new) | PK `tenant_code`, SK `{feature_set}#{version}` — definitions, lineage |
| `datalake-model-registry-<env>` (new) | PK `tenant_code`, SK `{model_name}#{version}` — artefact, metrics, state |
| `datalake-model-monitor-<env>` (new) | PK `tenant_code`, SK `{model_name}#{period}` — drift and accuracy |

Predictions land in the analytics layer under the standard tenant-prefixed pattern and are
registered in Glue, so they are queryable by Athena, the serving store, the semantic layer, and the
agent with no special-case plumbing.

## Design and patterns

- **Registry** for model types and for feature transformers, matching the connector and serving-store
  registries — a new model type is a registration, not a fork of the pipeline.
- **Strategy** per algorithm behind a `TrainableModel` interface (`fit`, `predict`, `explain`).
- **Repository** for feature sets, model registry, and monitoring records.
- **Pipeline as data**: a training run is a declarative spec persisted before execution, so it is
  reproducible and auditable.
- **Managed services over bespoke infrastructure**: SageMaker Training Jobs, Processing Jobs, and
  endpoints. Do not build a training scheduler; the platform already has Step Functions and
  EventBridge Scheduler.

## Performance

- Feature materialisation is set-based on the substrate over Parquet — never row-wise Python.
- Training runs in SageMaker, not in Lambda; Lambda orchestrates and never holds a dataset.
- Batch scoring is partitioned by tenant and brand and parallelised by key range.
- Online feature reads are single-digit-millisecond point lookups; if a use case needs a join at
  inference time, the join is precomputed into the online store.
- Training data snapshots are immutable and reused across experiments rather than rebuilt.

## Security and OWASP

- **A01** — training and inference roles are tenant-scoped; a training job cannot read another
  tenant's prefix. This depends on DL-SEC-01 (IAM tenant boundary) and is a strong argument for
  sequencing that work before ML.
- **A02** — model artefacts and feature stores are KMS-encrypted; PII is excluded from features by
  default, admitted only by explicit classification override with justification recorded.
- **A03** — feature SQL is generated by the substrate with allowlisted identifiers.
- **A04** — model outputs that drive decisions carry confidence and contributing features; a bare
  score with no explanation is not an acceptable deliverable for risk use cases.
- **A08** — model artefacts are checksummed and version-pinned at deployment; an endpoint never
  loads a mutable "latest".
- **A09** — training runs, deployments, approvals, and prediction batches are all audited.
- **ML-specific** — training-data poisoning is mitigated by the DL-02 quality gate upstream; model
  inversion risk is mitigated by excluding PII from features.

## Observability

`TrainingJobsStarted/Succeeded/Failed`, `TrainingDurationMs`, `FeatureMaterializationRows`,
`PredictionBatchRows`, `ModelAccuracy{model}`, `DataDriftScore{model}`, `PredictionDriftScore`,
`InferenceLatencyMs`, `InferenceErrors` — all alarmed.

Drift crossing threshold raises a retrain recommendation into the DL-06 workflow engine. Every
prediction batch writes a lineage record tying scores to the model version and feature-set version.

## Reuse and redundancy

- Features derive from semantic metrics — one definition of revenue across dashboards, chat,
  reconciliation, and ML.
- Predictions are ordinary analytics datasets — dashboards and the agent consume them through the
  existing semantic path with no ML-specific integration.
- The LLM port from DL-04 is reused for narrative explanation of model output; no second integration.
- Substrate, lineage, audit, alarm-reconciliation, and handler scaffold are all shared.

## Acceptance criteria

1. A revenue forecast model trains on point-in-time-correct features, registers, batch-scores, and
   its predictions are queryable through the semantic layer and visible on a dashboard.
2. Backtest results recorded in the registry; model selection is evidence-based.
3. A risk model returns calibrated probabilities with top contributing features.
4. Drift monitoring detects an induced distribution shift and raises a retrain recommendation.
5. Cross-tenant isolation test proves a training job cannot read another tenant's prefix.
6. No PII in any feature set without a recorded override.

## Dependencies

- DL-01, DL-02, DL-03 — models over unreconciled data with undefined metrics are not deliverable.
- DL-SEC-01 — IAM tenant boundary should precede tenant-scoped training roles.
- DL-06 — anomaly and drift alerts need somewhere to go.
