# DL-10 — Data Portability, Retention and Compliance

**SOW clauses:** §23.1, §23.6, §23.8, §23.9, §24 · **Priority:** P2 · **Owner repo:** DataLake

---

## Objective

Satisfy the data-ownership, portability, retention, deletion, and exit obligations in SOW §23 and
§24 with capabilities rather than promises. Several of these become contractually operative only at
termination, but every one of them must be built and rehearsed before production, because an exit
capability improvised under notice is not a capability.

## Current state (verified 2026-07-28)

- Data resides in the customer-designated AWS account, in open Parquet, under tenant-prefixed keys —
  the strongest possible starting position for §24.1 and §24.2.
- `governance/retention_policy_enforcer.py` implements retention application, legal hold, and hold
  lifting, with audit. Real code, tested.
- JSON schema snapshots per entity give a durable record of structure over time.
- Field-mapping, entity-resolution, relationship, and semantic configs are versioned JSON — the
  raw material for §24.6 continuity.

Missing: no export API or tooling in any format; **CSV and JSON export (§24.4) do not exist** —
data is Parquet-only; no transition tooling; no deletion certificate; no subprocessor register; no
PHI gating; retention policies are not attached to entities.

---

## Functional requirements

### Portability (§24.4)

- **DL-PORT-01** **Export service** producing **CSV, JSON, and Parquet** for any tenant dataset —
  raw, curated, golden, analytics, twin, and semantic-model definitions. Asynchronous job model:
  request → job → signed download or delivery to a customer-designated S3 bucket. Row-level security
  applies to exports exactly as it applies to queries; an export must never be a privilege-escalation
  path.
- **DL-PORT-02** **Transition package (§24.5)**: a single command producing the complete exit
  bundle — all datasets in the requested format, schema documentation, data-dictionary artefacts
  (DL-DQ-06), semantic-model and KPI definitions with calculation methodology (DL-SEM-05),
  field-mapping and transformation documentation, entity-resolution and survivorship rules,
  relationship rules, workflow definitions, and an inventory of source-system integrations.
  Rehearsed at least once before production go-live.
- **DL-PORT-09** **Infrastructure hand-over (§24.2)**: documented procedure for the customer to
  assume control of the underlying account and resources, including Terraform state hand-off and
  the resources whose lifecycle is protected by `prevent_destroy`.
- **DL-PORT-10** **Model and transformation continuity (§24.6)**: the exported definitions must be
  sufficient for a successor provider to reproduce the transformations. Validated by an
  independent reproduction test on one entity, not by assertion.

### Retention and deletion (§23.9, §24.7)

- **DL-PORT-03** **Attach retention policies to every entity**, with class-based defaults driven by
  the existing data classification. Today the enforcer exists but nothing is configured to use it.
- **DL-PORT-04** **Deletion workflow and certificate**: on termination plus the 180-day transition
  window, permanently delete or render inaccessible all customer data except where legal retention
  applies; produce a written deletion confirmation enumerating what was deleted, when, and what was
  retained under which obligation. Deletion must cover every store — S3 across all six buckets,
  every DynamoDB table, Secrets Manager, the serving store, CloudWatch logs, and any ML artefacts.
  A partial deletion certificate is worse than none.
- **DL-PORT-05** **Legal hold** interaction with deletion: a hold suspends deletion for the held
  scope only, and the certificate reflects it. The enforcer supports holds; the workflow must honour
  them.

### Compliance

- **DL-PORT-06** **Processing-purpose enforcement (§23.1)**: technical controls demonstrating
  customer data is processed only for service delivery — access logging, purpose-tagged roles, and
  no analytics on customer data for vendor purposes outside the aggregated/de-identified carve-out
  in §23.2.
- **DL-PORT-07** **Subprocessor register (§23.6)**: a maintained list of third parties processing
  customer data — AWS services in use, the LLM provider if not Bedrock-in-account, and any others —
  available to the customer on request, with change notification.
- **DL-PORT-08** **PHI gating (§23.8)**: the platform must **refuse** to onboard a tenant, brand, or
  entity flagged as PHI-bearing until a BAA is executed and the environment is confirmed
  HIPAA-capable. Implemented as a hard onboarding gate in the source-onboarding registry — Executive
  Home Care (WellSky) and Assisted Living Locators (SeniorPlace) are home-care and senior-placement
  businesses, so this is a live and near-term risk, not a theoretical clause.

---

## Data model

| Store | Purpose |
|---|---|
| `datalake-export-jobs-dev` (new) | PK `tenant_code`, SK `job_id` — scope, format, status, artefact location, expiry |
| `datalake-deletion-certificates-dev` (new) | PK `tenant_code`, SK `certificate_id` — scope, executed_at, retained items and basis |
| `datalake-source-onboarding-registry-dev` (existing) | add `phi_bearing` and `baa_executed` gate attributes |

Retention configuration attaches to the entity configuration record rather than a parallel store.

## Design and patterns

- **Job/worker** for exports — never synchronous, never memory-bound. Step Functions plus the
  substrate.
- **Strategy** per export format; Parquet is a pass-through copy, CSV and JSON are conversions on
  the substrate.
- **Gate pattern** for PHI onboarding, reusing the existing source-onboarding gate transitions in
  `governance/source_onboarding_registry.py` rather than a new mechanism.
- Deletion is a **saga with verification** — each store's deletion is a step whose completion is
  verified before the certificate is issued.

## Performance

- Exports are set-based on the substrate, streamed to S3 in parts; a large export must never be
  bounded by Lambda memory.
- CSV conversion is columnar-batch, not row-wise.
- Export jobs are rate-limited per tenant to protect shared capacity, alarmed rather than silently
  queued — consistent with §11's no-throttling posture.

## Security and OWASP

- **A01** — export scope is tenant-bound and row-level-security-filtered; export requires an
  explicit capability distinct from read.
- **A02** — export artefacts are KMS-encrypted, land in a dedicated prefix with a short lifecycle
  expiry, and are delivered by time-limited signed URL. Artefacts are deleted after retrieval or
  expiry.
- **A04** — deletion is irreversible; require maker-checker plus an explicit typed confirmation, and
  verify the legal-hold state before executing.
- **A09** — every export request, download, and deletion step is audited with actor and scope. The
  audit trail must itself survive deletion of the data it describes.
- **A05** — the PHI gate fails closed: an unclassified source is treated as potentially PHI-bearing
  until classified.

## Observability

`ExportJobsRequested/Completed/Failed`, `ExportBytes{format}`, `ExportDurationMs`,
`RetentionRecordsExpired`, `LegalHoldsActive`, `DeletionStepsCompleted`, `PhiGateBlocks` — alarmed.

A failed deletion step must page; an incomplete deletion against a contractual commitment is a
compliance incident.

## Reuse and redundancy

- Export reuses the substrate, row-level predicates, and semantic definitions — no second data
  access path.
- Data-dictionary and methodology artefacts in the transition package are the same artefacts
  generated by DL-DQ-06 and DL-SEM-05, not exit-specific documents.
- The PHI gate reuses the existing onboarding registry state machine.

## Acceptance criteria

1. A tenant exports a dataset in each of CSV, JSON, and Parquet, with row-level security applied.
2. A full transition package is generated and a successor-provider reproduction test passes on one
   entity.
3. Retention policies attached to every production entity; expiry demonstrated.
4. A deletion rehearsal in a non-production environment produces a complete, verified certificate
   covering every store.
5. A legal hold demonstrably blocks deletion of the held scope only.
6. A PHI-flagged source cannot be onboarded without a recorded BAA.

## Dependencies

- DL-SEC-11 (row-level security) applies to exports.
- DL-DQ-06 and DL-SEM-05 supply the documentation artefacts.
- §23.8 PHI gating should land before WellSky or SeniorPlace onboarding (DL-CONN-05, DL-CONN-10).
