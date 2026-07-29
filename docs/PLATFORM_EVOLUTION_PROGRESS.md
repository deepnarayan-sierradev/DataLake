# Platform Evolution — Session Handoff / Progress

**Purpose:** reload the context of the in-progress platform-evolution effort without re-deriving it.
This is a living status file for work on branch **`feat/platform-evolution-phase0`** — read it first
if you're continuing that work. The full requirements + assessment live in
[PLATFORM_EVOLUTION_SPEC.md](PLATFORM_EVOLUTION_SPEC.md); this file is the "what's actually built and
what's next" companion.

**Last updated:** 2026-07-23 · **Branch:** `feat/platform-evolution-phase0` · **All changes uncommitted**
(single commit only when the whole program is implemented + verified — user instruction).

---

## What this effort is

Adding an intelligence/experience layer on top of the existing data-lake pipeline — **Digital Twin**,
**Semantic Layer**, **Conversational AI Agent (+ verification loop)**, **Dashboards** — plus the
Phase-0 foundations they need, and (later) the enterprise-platform config-service/UI track. Scope and
rationale: `PLATFORM_EVOLUTION_SPEC.md` (Parts A–D). Origin: user asked to spec the "missing pieces"
seen in the Franchise Operations Suite reference and implement them without breaking existing
functionality (redesign allowed, no backward-compat required).

## Working constraints (binding)

- **No commit until the whole program is done + verified** — everything accumulates on the branch.
- **Tests for all new code**; keep the repo's 80% coverage gate green.
- **Minimal inline comments** — one line max; no prose above every method/class/property.
- House rules still apply: banned identifiers (`helper`/`util`/`common`/`manager`), OWASP-category
  comments on security code, canonical Lambda-handler pattern, `pyproject` registration for new modules.
- **No `terraform apply` without explicit user go-ahead**; prod apply is hard-blocked by a hook.
- **Agent layer is deferred** by the user — build everything else first.
- Enterprise-platform track comes **after** the DataLake track is complete (user ordering).

## Verification snapshot (all green as of last update)

`ruff check .` clean · CI-scope `mypy` adds **0 new errors** (73 pre-existing debt, none in new
files) · `bandit` 0 findings on new modules · **1727 passed, 1 skipped, coverage 96.39%** ·
`terraform validate` clean in dev/staging/prod · `terraform fmt` clean.

Commands: `.venv/bin/ruff check .` · `.venv/bin/pytest -q` · `.venv/bin/bandit -r <mods> --exclude tests -c pyproject.toml`
· scoped `.venv/bin/mypy -p processing_engine -p knowledge -p semantic -p agent` ·
`cd infrastructure/environments/<env> && terraform validate`.

---

## Done & verified

### New feature modules (additive — read existing golden records via the substrate; do NOT touch the live pipeline)

| Module | Contents |
|---|---|
| `processing_engine/` | Set-based `SetBasedQueryEngine` interface + registry + `DuckDbSetBasedEngine` (stream/materialize over S3 Parquet, bind-params, injection-free via relation API) — the substrate everything runs on |
| `knowledge/` | `RelationshipRule`/`RelationshipRuleSet`, `RelationshipResolver` (set-based edge joins), `Twin`/`TwinEdge`, `TwinBuilder`, `TwinRepository` (DynamoDB `EdlTwinIndex`), `TwinPipeline` (end-to-end orchestration) |
| `semantic/` | `SemanticModel`/`SemanticEntity`/`Dimension`/`Metric`, `QueryCompiler` (structured→parameterized SQL, access-tag enforcement, never raw SQL), `SavedQuery` + `SavedQueryRepository` (`EdlSavedQuery`), `SemanticModelRepository` (`EdlSemanticModel`), `SemanticQueryService` (compile+execute) |
| `agent/` | `SemanticRequestProposer` interface + `ConversationalAgent` (mandatory verification loop: schema-check → execute → ground; self-correct on hallucination; access-denied terminal; "cannot answer" fallback). **No concrete proposer** — see Loose Ends. |

All registered in `pyproject.toml` (`testpaths`, `[tool.coverage.run].source`, isort `known-first-party`,
hatch wheel `packages`).

### Phase-0 cleanups (existing code)

- **DP-01** — deleted the orphaned duplicate `entity_resolution/golden_record_publisher/` (dead code; the live class is under `canonical_record_publisher/`).
- **REU-02 / OWASP A03** — `SAFE_S3_PREFIX_PATTERN` + `validate_s3_prefix()` + `SAFE_COLUMN_PATTERN` consolidated into `contracts/identifier_policy.py`; 4 duplicate defs removed; the pattern now rejects `..` traversal (previously the handlers were dot-permissive).
- **SEC-03 / OWASP A02** — SENSITIVE_PII auto-mask changed `HASH` → `FULL_MASK` (unsalted SHA-256 was dictionary-reversible for SSN/CC); corrected the false "always tokenised" docstring.

### Terraform (new storage services)

`infrastructure/modules/metadata_persistence/` — three new DynamoDB tables (KMS-encrypted, PITR,
`prevent_destroy`, tenant-partitioned; wired via the shared module into all 3 envs) + module outputs:
`EdlTwinIndex` (PK `tenant_code`, SK `sk`), `EdlSemanticModel` (PK `tenant_code`, SK `model_version`),
`EdlSavedQuery` (PK `tenant_code`, SK `query_id`). Repos default to these names via env-var fallback,
so no consumer wiring is needed until a Lambda/endpoint uses them. **Agent tables intentionally omitted.**

### Docs

- `PLATFORM_EVOLUTION_SPEC.md` — the requirements spec + evidence-based 7-category assessment (findings
  re-verified against source; one refutation caught: the raw S3 layer **is** tenant-prefixed today).
- Corrected stale raw-layer claims in `PIPELINE_FLOW.md` / `PLATFORM_STATUS.md` + regenerated `.html` twins.
- `Enterprise_Data_Lake_Platform_Product_Definition_v1.html`/`.pdf` (product-definition deliverable).

---

## Key design decisions (and why)

- **Additive-first sequencing.** The twin/semantic/agent modules read the golden records the *current*
  entity resolution already produces (via the substrate) — delivering the features **without** re-platforming
  the live pipeline. The risky set-based ER re-platform (FR-F0.1) is deferred, not a prerequisite.
- **Agent proposer must be provider-neutral.** Do NOT hardcode Claude/Anthropic. The design is an abstract
  `LlmStructuredClient` port + a `ModelSemanticRequestProposer`, with per-provider adapters behind the port.
  (A Claude-specific `llm_proposer.py` was written then deleted per user direction.)
- **REU-01 (shared handler scaffold) is folded into the FR-F0.1 handler rewrite** — the five `_validate_event`
  handlers get rewritten onto the substrate anyway; refactoring them twice is waste. Tests pin exact error
  substrings, so a premature shared validator is fiddly for no lasting gain.
- **Twin store = relational adjacency** (DynamoDB index) first, not a graph DB. Value rollups (FR-1.5) and
  lifecycle *history* (SCD-2) deferred — current: edge-count rollups + current stage only.
- **Semantic compiler = thin custom** (no Cube/dbt-metrics library). Saved-query filters deferred (metrics+dims only).
- **Substrate tests mock `duckdb`** via `sys.modules` (repo convention, matching `curated_layer_reader` tests) — no
  real-data integration test yet.

---

## Done & verified — 2026-07-23 (net-new feature + infra increment)

1. ✅ **Provider-neutral proposer.** `agent/llm_client.py` (`LlmStructuredClient` port) +
   `agent/model_proposer.py` (`ModelSemanticRequestProposer`, grounds the prompt in the tenant's
   semantic model; compiler re-validates output) + tests. No provider SDK imported.
2. ✅ **Twin-build Lambda service.** `knowledge/relationship_rules_registry.py` (S3-backed, per-tenant,
   versioned, like the ER config registry) + `knowledge/twin_build_handler.py` (canonical handler;
   resolves entity_type, loads rules, targets the latest analytics partition via the shared
   `analytics_publisher/analytics_location.py` locator, skips cleanly when no rules) + tests.
   Terraform: `infrastructure/modules/twin_build_lambda/` (`EdlTwinBuilder`, no VPC) + IAM role
   `EdlTwinBuilderRuntimeRole` (analytics R/W-edges, curated read, `EdlTwinIndex` write, registry read)
   + additive **`BuildTwin` Step Functions stage** (skippable Pass; twin failures caught → pipeline
   never fails) wired into dev/staging/prod.
3. ✅ **Control-plane API endpoints** — twin read (get/list), semantic query execution, saved-query
   CRUD + run — table-driven dispatcher (`_route_intelligence_layer`) to stay under the complexity
   gate; access tags from verified claims (OWASP A01); + resource-scoped IAM grants on
   `EdlTwinIndex`/`EdlSemanticModel`/`EdlSavedQuery` + analytics-S3 read on `EdlControlPlaneRole`;
   + control-plane module routes + env vars. Tests in `test_control_plane_intelligence_routes.py`.

## Phase-0 hardening — status (2026-07-23)

- ✅ **FR-F0.8b DONE.** `ConfigurationRepositoryClient` converged onto `tenant_scoped_key()` (PK is
  the `{tenant_code}#{source_id}` composite, same KeySchema → non-destructive), mirroring
  `WatermarkRepository`. Plain `source_id` restored on read; `list_configs_for_tenant` strips the
  prefix. `scripts/seed_entity_config.py` updated. **`scripts/migrate_entity_config_to_tenant_scoped_key.py`
  written (dry-run default) — MUST be run (with `--apply`) against dev BEFORE deploying this code, or
  existing dev configs go dark. Needs explicit AWS-mutation consent — NOT run.** Parity tests added
  (two tenants, same source/entity, no collision).
- 🟡 **FR-F0.6 PARTIAL.** Done: alarm↔emitter **reconciliation guard test** (OBS-01 "+ CI check",
  `observability/tests/test_alarm_emitter_reconciliation.py`) + closed the 4 dead alarms at the
  emitter-contract level (`CircuitBreakerOpened/DDBFallback`, `InputValidationFailures`,
  `CredentialRetrievalFailures` now have emitter methods). **Remaining:** wire those 4 emit calls at
  their runtime failure points; flush-in-`finally` across the 5 stage handlers; guaranteed failure
  record on hard kill; per-stage DLQ + replay; stable cross-replay correlation id; Lambda-Insights
  memory alarm. These are cross-cutting handler/orchestration/TF changes — do per-item with tests.
- ⬜ **FR-F0.1 NOT STARTED (deliberately).** Set-based re-platform of ER + transformation + analytics
  behind unchanged stage contracts, *with parity tests before deleting the in-memory paths*. Large,
  high-risk rewrite of the most heavily-tested code; **not required for the shipped features** (they
  already run additively on the substrate). Recommended approach: one stage at a time — add the
  set-based path behind the existing contract, prove output parity against the in-memory path with
  tests, only then delete the old path. Do NOT big-bang.

## Remaining work (ordered; agent layer deferred per user)

1. **Agent layer (deferred)** — conversational-agent endpoint + `EdlAgentAudit`/`EdlAgentSession` tables +
   verification-loop audit persistence.
2. **Enterprise-platform track** (after DataLake) — IaC + CI (both currently absent), security defaults
   (SEC-01 fail-open `env` default, SEC-02 JWT audience), `async def`→`def` fix, generic
   `RegistryBackedConfigService` base + CST-UI `createConfigResource`/`ConfigListPage` factories, then the
   new config screens (quality rules, PII/classification, data catalog, relationships/twin, semantic, dashboards).

## Assessment cross-reference

Finding IDs (ARCH-/DP-/PERF-/SEC-/OBS-/REU-) referenced above are defined in
`PLATFORM_EVOLUTION_SPEC.md` Part A, with per-finding severity, evidence file, and recommendation.
