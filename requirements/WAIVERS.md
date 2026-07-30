# Waivers

Recorded decisions for the wiring gates in `scripts/`. A waiver is a **decision with a reason and
an owner-visible cost**, not a way to silence a gate. Both gates fail on a *stale* waiver — one
whose subject has since been wired — so this file cannot quietly drift out of date.

Format is load-bearing (the gates parse it):

```
- `package.module` — reason
- `DL-XXX-00` — reason
```

**Last reviewed:** 2026-07-28 (S6, S7, S8, S9 and most of S10 landed: pinning, governance routes, four platform Lambdas with their own roles: run-level pinning and effective-config recording are wired)

---

## Module reachability (G1) — `scripts/check_module_reachability.py`

### Awaiting a deployable entry point (plan items S6–S9)

These are complete, unit-tested modules with no Lambda, route, or state-machine task yet. Each
names the plan item that gives it one. They are the honest residue of the 2026-07-28 audit: the
code exists, and until the entry point lands it cannot run.

- `config_propagation.cache_invalidation` — read by S6's pinning path
- `data_quality.backfill_orchestrator` — bounded backfill; I1c drives it for the scope backfill
- `data_quality.brand_registry` — brand dimension; S10
- `data_quality.data_dictionary` — published dictionary; S7 exposes it
- `data_quality.reconciliation` — source reconciliation verdicts; S10
- `portability.phi_gate` — PHI onboarding gate: belongs on the onboarding path, which is the
  enterprise-platform's call into this system (S7)
- `portability.transition_package` — transition artefact: no route requests one yet (S7)
- `workflow_automation.actions` — the registered handlers: the runner constructs the engine but
  the wired handler set is injected by the deployment, so no module imports them yet (S9 remainder)
- `tenancy.tenant_session` — the mechanism that makes the IAM tenant boundary enforceable, with no
  caller yet. This is a *deliberate, tracked* state rather than an oversight, and the cost is
  visible three ways: `make tenant-session-adoption` (G9) lists the 47 remaining call sites,
  `tests/test_capability_reachability.py` asserts it is still pending so this waiver cannot go
  stale unnoticed, and the Terraform interlock refuses `enforce` while
  `tenant_session_tagging_adopted = false`.

  Why it is not simply wired: the boundary conditions on `aws:PrincipalTag/tenant_code`, which
  cannot exist on a shared Lambda execution role — a role tag holds one value and each of the four
  runtime roles serves every tenant. So the policy was not merely unapplied, it was unsatisfiable:
  under `enforce` the S3 statements would never apply (their `Null ... = false` guard is false for
  an untagged principal, leaving S3 open) while the DynamoDB and Secrets Manager statements would
  deny everything. Adoption means threading a tenant-tagged session through ~47 client
  constructions inside repository constructors, which is design-sized work and is why it is
  recorded here rather than half-done.

- `serving_store.credential_delivery` — blocked on the BI network-path decision (gap 4); there is
  no point delivering a reader credential nobody can connect with. Note the RLS policy it would
  deliver against is now correct as of 2026-07-29 (the loader is exempted by role, so applying the
  policy no longer breaks the loader's own next upsert) — what remains is the network path and the
  component that sets `datalake.scope_units` on a BI connection.

### Deferred phase — DL-04 (AI agent runtime)

The `agent/` package is the DL-04 conversational-agent prototype, deferred to a separate team by
agreement. It is unreachable because there is deliberately no agent runtime here; the modules exist
as the interface contract that team will build against.

- `agent` — DL-04 deferred; no agent runtime in this repository
- `agent.conversational_agent` — as above
- `agent.llm_client` — as above
- `agent.model_proposer` — as above
- `agent.proposer_interface` — as above

### Deliberately not done, with the reason

- **Full streaming through the matching engine (L15) is not achievable as scoped, and here is
  why.** Two constraints make the "materialise nothing" goal unreachable rather than merely
  unfinished:
  1. **Union-find clustering needs each block's records together.** Transitive matching (A~B, B~C
     therefore A~C) cannot be decided from a stream. Blocking already bounds this —
     `max_block_size` caps every block — so the engine's memory is bounded per block already.
  2. **The batch quality gate needs the whole set.** Completeness and duplicate *rates* are
     properties of the batch, so DL-DQ-01..04 cannot be evaluated on a stream. Streaming the
     transformation path would mean giving up the batch gate that S10 just wired.

  What was done instead: `RecordBlocker.partition` accepts any iterable, so a caller streaming
  from S3 never needs the full list to partition; and scope-partitioned resolution (I2) divides a
  partitioned tenant's peak memory by its number of scope units, because each unit is now its own
  set of blocks. The remaining materialisation is inherent to the two guarantees above, not an
  oversight — revisit only by changing one of those guarantees deliberately.

### Deliberately not used, with the reason

- **`build_merge_plan` is not executed by the loader.** The plan generates stage → merge → verify
  statements, but every loader already implements a correct merge inline (`MERGE INTO` in
  `redshift_loader.py` / `sqlserver_loader.py`, row-wise upsert elsewhere). Executing a generated
  plan on top would be a *second* merge mechanism — the duplication the audit wrongly attributed to
  the quality modules would become real here. What the module is used for is its
  `default_sizing_profile`, whose index recommendations are applied alongside the RLS policy
  because the predicate filters on exactly those columns. Revisit only if a loader is added whose
  engine has no native merge.

### Reached only by an operator, by design

- `connector_runtime.certification` — connector certification checklist, run during review
- `connector_runtime.certification.connector_certification_checklist` — as above
- `governance.retention_policy_enforcer` — scheduled/manual retention sweep, not on the pipeline
- `governance.source_onboarding_registry` — source certification workflow, operator-driven
- `orchestration.step_functions.run_replay_controller` — replay is initiated by an operator
- `transformation.athena_query_client` — ad-hoc verification helper for operators

### Imported for their side effect only

- `connector_runtime.legacy_source_capabilities` — registers the four pre-DL-01 capability
  declarations at import; the registry is the consumer, not a caller
- `connector_runtime.sync_strategy` — strategy registry populated at import; L14 selects from it
  in the extraction handler
- `connector_runtime.adapters.sage.protocols.sage_auth_protocol` — a `Protocol` definition;
  structural typing means nothing imports it at runtime
- `entity_resolution.matching_engine.match_evaluation` — explainability record type, referenced
  through the engine's return values

---

## Requirement traceability (G5) — `scripts/check_requirement_traceability.py`

### Not code — process, deployment, or contract obligations

- `DL-OPS-01` — provision staging: a deployment act, not code. Blocked on account provisioning
- `DL-OPS-02` — environment promotion rehearsal; follows DL-OPS-01
- `DL-OPS-03` — deployment approval gates: GitHub environment settings, not repository code
- `DL-OPS-04` — runbook coverage; lives in `docs/PRODUCTION_INCIDENT_RUNBOOK.md`
- `DL-OPS-06` — on-call rotation: an operational arrangement
- `DL-OPS-10` — capacity review cadence: a process
- `DL-OPS-12` — change-management record: a process
- `DL-OPS-14` — disaster-recovery rehearsal: needs a provisioned environment
- `DL-OPS-15` — cost review cadence: a process
- `DL-SEC-14` — penetration test: an external engagement
- `DL-SEC-15` — security training: a process
- `DL-SEC-16` — incident-response tabletop: a rehearsal
- `DL-SEC-17` — SOC 2 readiness: certification is a business process; the engineering obligation
  (continuous evidence) is tracked under DL-DQ-14 and the audit tables
- `DL-SEC-12` — **withdrawn**, not deferred: tenant provisioning belongs to the Identity API and
  the route was deleted. See the system-boundary section of the root `CLAUDE.md`

### Partially wired — completion tracked by a plan item

Cited in both a reachable module and a waived one, so part of the requirement runs today and part
does not. Waived at id level because the module waiver alone cannot express "half wired".

- `DL-CFG-15` — shared-package contract tests pass; the runtime side is unwired (S6)
- `DL-OPS-13` — replay controller: reachable by an operator, not from the state machine (L14)
- `DL-SCOPE-18` — scope-widening approval: the profile carries the approval fields and the
  partition profile is now read at runtime; the widening *operation* has no route (S7)
- `DL-SEC-01` — serving-store reader isolation: credential provisioning runs in the loader; the
  delivery path to the tenant is blocked on the BI network decision (gap 4)

### Satisfied without an id citation

- `DL-SEC-18` — CI gates: enforced by `.github/workflows/ci.yml` jobs and the `scripts/check_*`
  gates rather than by a citation in application code
- `DL-OPS-08` — `duckdb` in the Lambda package: satisfied in the `Makefile`'s `lambda-package`
  target, which carries no requirement ids
- `DL-SEC-03` — tenant-scoped config keys: implemented in
  `connector_runtime/configuration_repository/` and migrated 2026-07-24; the code documents the
  behaviour without the id
- `DL-SEC-06` — encryption at rest: KMS settings across every storage module, not one citable site
- `DL-SEC-07` — encryption in transit: TLS enforced per service, spread across modules
- `DL-SEC-08` — key management: KMS key policies and rotation, spread across modules

### Genuinely outstanding

One requirement is now *partially* implemented and therefore no longer waived — the gate reports
it as wired because its code is reachable. The residual gap is recorded in the requirement
document itself rather than here, so a waiver cannot hide a half-done requirement:

1. **DL-CFG-06** — the TTL bound and `force_refresh()` exist; the observed propagation-lag metric
   is not yet emitted for the credential cache. Recorded in
   `requirements/DL-11-config-propagation-consistency.md`.

**DL-SCOPE-13 is closed**, and how it was previously recorded here is worth keeping. This file
used to say the twin *API* filtered and only edge fan-out was open. That was wrong in the safer-
sounding direction: `Twin`, `TwinEdge` and the `datalake-twin-index-dev` item carried **no** `scope_unit_id`,
so `getattr(twin, "scope_unit_id", None)` made the node filter read every twin as unattributed —
match-all for a `single` tenant and deny-all for a partitioned one. Both halves are now real: the
model carries the column, the writer persists it, the node filter uses direct attribute access so
a missing field is a type error, and edges are filtered by the target's owning unit. Gate **G7**
(`make security-columns`) exists so a filter on an unwritten column cannot recur, and
`connector_runtime/tests/test_twin_scope_isolation.py` drives both routes with two sibling
franchisees rather than the single-partition `demo` tenant that hid this.

**DL-SEM-07 is closed, and how it was previously recorded here is the reason this file needs
reading sceptically.** The entry said "filters on saved queries: implemented in
`semantic/query_compiler.py` (`SemanticFilter`, `RelativeDateRange`) but uncited; **add the id
rather than the code**" — advice that would have closed a requirement whose behaviour no caller
could reach. `SemanticFilter` did exist in the compiler. But `SavedQuery` said *"Filters are
deferred to a follow-up"* and carried none, and `SemanticQueryBody` accepted only
entity/metrics/dimensions, so no request could express a filter, a date range, a fiscal grain, a
period comparison, or a join. Joins were worse than unreachable: the compiler emitted
`JOIN <entity>` while only `entity_data` was ever registered as an input, so a joined query
compiled cleanly and failed at execution.

Both halves are now real: `SemanticQueryShape` carries the compiler's full surface,
`SavedQuery` persists it, and `SemanticQueryService._inputs_for` binds a relation per joined
entity, asserted by a test that runs the query rather than reading its SQL.

The general lesson: **"the code exists in some module" is not reachability.** G1 answers that at
module granularity, so a capability with no path from any entry point passes it — which is why G1
now also flags a public method with no production caller.
