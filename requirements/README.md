# Requirements — DataLake Platform

SOW-driven requirement specifications for the **`DataLake` repository only**. Everything here is a
data-plane, control-plane, or platform-infrastructure requirement that is implemented inside this
repo.

**Source of truth for scope:** *Enterprise Data and Intelligence Platform Statement of Work*
(`~/DataLake Docs/Enterprise Data and Intelligence Platform Statement of Work (1).docx`) and its
companion source-system list (`Evive Data Sources - Data Lake.xlsx`).

**Baseline assessment date:** 2026-07-28. Current coverage against the SOW is **~38% code-complete
across both repositories, ~20% deployed (dev only), 0% production**. This directory specifies the
remaining ~62%.

---

## System boundary — the DataLake is configuration-driven, and owns no identity

Before the repository split below, one rule overrides it: **this repository never owns tenants,
users, roles, or permissions.** The **Identity API** does, and `enterprise-platform` is built on
it. The DataLake is a standalone processing system that reads configuration — entity settings,
field mappings, entity-resolution and survivorship rules, the entity-type registry, semantic
definitions, schedules, connections, the scope model — from what the enterprise-platform
publishes, and acts on it.

So: no requirement here may add a tenant-provisioning, user, role, or permission surface. The
DataLake *does* validate the verified claim it is handed and fails closed without one; that is
consuming identity, not owning it. `DL-SEC-12` was withdrawn on 2026-07-28 for exactly this
reason — see `DL-08`.

## Repository boundary — read this before adding anything here

| Concern | Repository |
|---|---|
| Ingestion, transformation, entity resolution, analytics publish | **DataLake** |
| Connectors, credentials, watermarks, schedules (runtime) | **DataLake** |
| Semantic model *engine*, query compiler, twin, agent *runtime* | **DataLake** |
| ML platform, workflow automation *engine*, serving store | **DataLake** |
| AWS infrastructure for all of the above | **DataLake** |
| Config *authoring* API + console screens | `enterprise-platform` |
| Dashboards, report builder, scheduled distribution, chat *UI* | `enterprise-platform` |
| Excel add-in, RBAC administration screens, enterprise apps | `enterprise-platform` |
| Training material, ROI/value dashboards | `enterprise-platform` |

**Do not mix.** A requirement belongs here only if its acceptance criteria can be met without
changing a file outside `/Users/deepnarayan/DataLake`. Requirements that span both repos are split
into a DataLake half (engine/API) and an enterprise-platform half (experience), each with its own
ID, cross-referenced in both traceability matrices.

---

## Documents

| ID | Document | SOW clauses | Priority |
|---|---|---|---|
| DL-01 | [Source Connectors and Integration Coverage](DL-01-source-connectors.md) | §3.1, §3.2, §3.4, §3.8, §14 | **P0** |
| DL-02 | [Ingestion Quality, Migration and Reconciliation](DL-02-ingestion-quality-reconciliation.md) | §3.3, §3.5, §3.6, §9 | **P0** |
| DL-03 | [Semantic Model and Enterprise KPI Definitions](DL-03-semantic-and-kpi.md) | §4, §18 | **P0** |
| DL-04 | [Conversational AI Agent Runtime](DL-04-ai-agent-runtime.md) | §6.1, §5.3, §18 | ⏸ **DEFERRED** |
| DL-05 | [Machine Learning Platform](DL-05-machine-learning-platform.md) | §6.2, §18 | ⏸ **DEFERRED** |
| DL-06 | [Workflow Automation Engine](DL-06-workflow-automation.md) | §6.3, §18 | **P1** |
| DL-07 | [Serving Layer and BI Network Access](DL-07-serving-and-bi-access.md) | §5, §11, §18 | **P0** |
| DL-08 | [Security, Tenant Isolation and Access Control](DL-08-security-tenant-isolation.md) | §8, §23 | **P0** |
| DL-09 | [Operations, Environments and Observability](DL-09-operations-environments.md) | §2, §3.7, §12, §15, §21, §22 | **P0** |
| DL-10 | [Data Portability, Retention and Compliance](DL-10-portability-lifecycle-compliance.md) | §23, §24 | **P2** |
| DL-11 | [Configuration Propagation and Runtime Consistency](DL-11-config-propagation-consistency.md) | §3, §4, §9, §12, §13 | **P0** |
| DL-12 | [Source Connections and Scope-Unit Data Isolation](DL-12-connections-and-scope-isolation.md) | §3.1, §8, §14, §23.4 | **P0** |
| — | [Cross-Repo Interface Contract](CROSS_REPO_INTERFACE_CONTRACT.md) | — | **normative** |
| — | [SOW Traceability Matrix](SOW_TRACEABILITY.md) | all | — |
| — | [Implementation Plan](IMPLEMENTATION_PLAN.md) | all | — |

### Deferred scope — owned by a separate team

**DL-04 (Conversational AI Agent Runtime)** and **DL-05 (Machine Learning Platform)** are deferred as
of 2026-07-28 and assigned to a separate team. The documents remain complete and authoritative — they
are not withdrawn, and nothing in them is superseded. They are simply out of this programme's
delivery scope until that team picks them up.

Consequences the remaining scope must respect:

- **The LLM port stays unimplemented.** `agent/llm_client.py` remains an abstract port with no
  concrete adapter. Any requirement that assumed an LLM was available — narrative generation,
  workflow-triggered summaries — must degrade gracefully or drop the feature, not stub a provider.
- **`DL-WF-02`'s ML-signal trigger type has no producer.** The workflow engine must still define
  the trigger contract (so DL-05 plugs in later without a schema change) but ship with no ML source
  emitting into it. Anomaly-driven alerting comes from `DL-DQ-14` exception records and
  `DL-SEM` threshold conditions instead.
- **`DL-ML-11` anomaly detection is unavailable**, so trend-based alerting is threshold-based only.
- **Predictions are not an analytics dataset**, so `EP-DASH-06` predictive widgets have no data.
- Both documents' cross-cutting design decisions (provider neutrality, tenant-scoped training roles,
  predictions-as-ordinary-analytics) remain binding on the separate team, because they are what keep
  the deferred work additive when it lands.

---

## Working independently — DataLake session

This repository is implemented in **its own session, separate from `enterprise-platform`**.

**Do not read, open, or reason about `/Users/deepnarayan/enterprise-platform`.** Everything you need
about that side is in [CROSS_REPO_INTERFACE_CONTRACT.md](CROSS_REPO_INTERFACE_CONTRACT.md) — an
identical copy lives in that repo, and changing one without the other is a defect.

**What you own:** table schemas, key construction, S3 layouts, secret paths, config payload shapes,
pipeline execution, and the HTTP endpoints the console consumes. You are the upstream side of the
contract.

**What you may assume:** the enterprise-platform writes config only through the published contract
package shapes; publishes always bump versions; drafts never produce downstream effects.

**What you must not do without coordination:** change any table key schema, S3 layout, secret path,
schedule-name format, or config payload shape. Those are contract changes — follow §10 of the
interface contract (bump the package with both forms supported, land the consumer, then remove the
old form). `DL-12` is exactly such a change and is already documented in §7.

**Start with** [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md) Phase 0. Nothing in Phase 0 or
Phase 1 requires the enterprise-platform to exist.

## Requirement ID scheme

`DL-<AREA>-<nn>` — e.g. `DL-CONN-04`, `DL-SEC-11`. Areas: `CONN`, `DQ`, `SEM`, `AGENT`, `ML`, `WF`,
`SERV`, `SEC`, `OPS`, `PORT`, `CFG`.

IDs are stable and never renumbered. A withdrawn requirement is marked `WITHDRAWN` in place.

## Relationship to existing documents

These specs **supersede nothing**. They sit alongside:

- `docs/KNOWN_GAPS_AND_ROADMAP.md` — engineering debt register. Where a SOW requirement is blocked
  by a known gap, the requirement cites the gap number; the gap register stays the owner of the
  remediation detail.
- `docs/PLATFORM_EVOLUTION_SPEC.md` — the twin/semantic/agent architecture spec with `FR-*` IDs.
  Where a requirement here restates one of those, the `FR-*` ID is cited rather than re-specified.
- `docs/PLATFORM_STATUS.md` — deployed state. Every "current state" claim here was re-verified
  against source on 2026-07-28, not copied from that document.

## Binding conventions for all work specified here

Non-negotiable, enforced by CI and hooks — see root `CLAUDE.md`:

- Banned identifiers: `helper`, `util`, `common`, `manager` (`make banned-names`).
- ID/tenant validation only via `contracts/identifier_policy.py`.
- `tenant_code` always prefixed, including the default `demo` tenant.
- `extra="forbid"` on config/params/API-boundary Pydantic models.
- OWASP category cited in security-relevant comments (`OWASP A03`, etc.).
- Canonical Lambda handler pattern with `finally: clear_contextvars()`.
- **New module ⇒ register in SIX places, not four.** `testpaths`, `[tool.coverage.run].source`,
  isort `known-first-party`, the hatch wheel `packages` list, **the `Makefile`'s `lambda-package`
  copy list**, and **the CI `typecheck` mypy scope**. `persistence/` was created on 2026-07-29 and
  registered in four of the six; seventeen modules import it, and it was missing from both the wheel
  and the Lambda copy list — so the deployed artefact would have raised `ModuleNotFoundError` on the
  first invocation, with the whole suite green because the suite imports from the working tree.
  `tests/test_package_registration.py` (G11) now reconciles all six, so counting is no longer the
  control.
- 80% coverage gate; `ruff`, scoped `mypy`, `bandit` clean.
- **Comment density:** one line maximum above any function, class, or method. No prose docstring
  blocks on new code. Explain *why*, never *what*.
