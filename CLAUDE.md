# CLAUDE.md — Enterprise Data Lake Platform

Metadata-driven, connector-based multi-tenant data lake on AWS Lambda / Step Functions /
DynamoDB / S3, provisioned via Terraform. Python 3.14, strict typing throughout.

## System boundary — read this before adding any endpoint or table

This repository is a **standalone data-lake processing system driven entirely by
configuration**. It does **not** own — and must never be given — tenant, user, role, or
permission management. Those belong to the **Identity API**, which `enterprise-platform` is
built on.

What this system owns: ingestion, transformation, entity resolution, the entity-type registry,
the semantic engine, the twin, the serving store, the workflow engine, exports — plus the
schemas, key construction, S3 layouts, and secret paths those use.

What it consumes, never authors: tenants, users, roles, permissions, and every configuration
surface (entity settings, field mappings, entity-resolution and survivorship rules,
entity-type registrations, semantic definitions, schedules, connections, scope model). The
enterprise-platform publishes them through the shared `edl_shared_contracts` package into
DataLake-owned tables; this system reads them and acts accordingly.

Concretely:

- **Never add a route, handler, model, table, or Terraform resource for creating or
  administering a tenant, user, role, or permission.** There is deliberately no
  `POST /tenants` here, and `connector_runtime/tests/test_control_plane_handler.py`
  asserts its absence so it is not re-added by reflex.
- **Do** keep validating the incoming verified claim — tenant code, capabilities, scope units
  — and failing closed when it is absent or mismatched. That is consuming identity, not
  owning it.
- `AdminActions` is emitted only for privileged operations this system genuinely owns: config
  rollback, semantic-model rollback, scope-partition widening, and data deletion.

This has been re-stated by the repo owner across multiple sessions. Treat it as a boundary,
not a preference.

This file exists so sessions don't re-derive the same conventions and traps from scratch every
time. Check here first; only re-explore when something below is stale (verify against the code,
not just this file, before relying on anything security- or infra-relevant).

Nested `CLAUDE.md` files exist in `infrastructure/` and `connector_runtime/` for domain-specific
detail — Claude Code loads them automatically when you work in those directories, so this file
stays focused on repo-wide conventions.

## Orientation — read these, don't re-derive them

- `README.md` — setup, doc index, connector credential paths
- `docs/DEVELOPER_GUIDE.md` — module map, Terraform workflow, known gotchas
- `docs/PLATFORM_STATUS.md` — canonical resource names (S3 buckets, DynamoDB tables, Lambda
  functions, Glue tables) per environment — check here before guessing a name
- `docs/PIPELINE_FLOW.md` — canonical pipeline architecture and the tenant-isolation model
  (which layers are genuinely key/prefix-isolated vs. application-level-guard-only vs. not
  isolated at all) — the single source other docs link to instead of re-deriving it
- `docs/KNOWN_GAPS_AND_ROADMAP.md` — the current source of truth for what's missing, broken, or
  deferred, plain language, re-verified against the code, no ID scheme
- `docs/WIRING_PASS_HANDOFF.md` — **session handoff** for the 2026-07-28 wiring pass: why the gates
  exist, what landed, the design notes not to re-derive, the ordering hazards, and the four items
  awaiting an approval before any `terraform apply`
- `docs/ASSESSMENT_CLOSEOUT.md` — **read this before running another assessment.** The 2026-07-29
  third pass, its 21 findings, the four root causes that accounted for fourteen of them, and the
  **frozen exit list** that replaces open-ended assessment. It also records why each pass found
  new things (an unbounded audit scope, plus gates that certified shape rather than effect) and
  the one finding that is deliberately *not* closed (F1's 47-site session adoption).
- `docs/REMEDIATION_PASS_HANDOFF.md` — **session handoff** for the 2026-07-29 remediation pass: what a
  second assessment found in the wiring pass (ten of sixteen findings were defects in it), the
  `.gitignore` rule that had excluded 14 source files and kept CI red for months, the two new gates,
  and what is still open. Its sizing companion is `docs/SCALE_AND_DLQ_THRESHOLDS.md`.
- `docs/PLATFORM_EVOLUTION_PROGRESS.md` — **session handoff** for the twin/semantic/agent/
  dashboards work. Its requirements + assessment companion is `docs/PLATFORM_EVOLUTION_SPEC.md`.
- `requirements/` — the **SOW requirements programme** (DL-01…DL-12): one document per phase plus
  `IMPLEMENTATION_PLAN.md`, `CROSS_REPO_INTERFACE_CONTRACT.md`, and `README.md`. Read
  `requirements/README.md` for what is built, deferred (DL-04 agent runtime, DL-05 ML platform),
  and withdrawn (DL-SEC-12, on the system-boundary grounds above).
- `.github/pull_request_template.md` — the actual quality bar (CI gates, security checklist,
  naming standard) — follow it when preparing a PR description
- `.github/CODEOWNERS` — path-based review ownership

## Verification — the commands that actually work

Invoke tools via `.venv/bin/<tool>` explicitly rather than assuming an activated venv — shell
activation doesn't reliably persist across separate tool calls in an agent session.

```bash
make wiring-gates                                                # G1/G4/G5/G7/G8/G9/G12 — see below
make typecheck-scripts                                           # operational scripts (see below)
.venv/bin/ruff check .                                          # lint
.venv/bin/ruff format --check .                                  # CI runs this too — it is not optional
.venv/bin/pytest -q                                              # full suite, enforces 80% coverage gate
.venv/bin/pytest --no-cov -q                                     # faster loop, skip coverage
.venv/bin/bandit -r . --exclude .venv,tests,dist -c pyproject.toml
```

`ruff`'s `[tool.ruff] extend-exclude` in `pyproject.toml` now excludes `pptx/`,
`scripts/generate_presentation.py`, and `scripts/_gen_html.py` (fixed 2026-07-08) — `ruff check .`
above already respects that config, so it still matches CI exactly without any extra flags.

**Never run bare `mypy .`** — whole-repo invocation fails for reasons unrelated to your change: a
`dist/lambda-build/typing_extensions.py` shadow conflict and a `scripts/generate_presentation.py`
vs. `pptx/generate_presentation.py` module-name collision. Always scope it to the files/packages
you touched, e.g. `.venv/bin/mypy connector_runtime/foo.py connector_runtime/tests/test_foo.py`.
Per-package invocation (`mypy -p some_package`) also surfaces pre-existing, out-of-scope
`no-untyped-def`/`no-untyped-call` warnings in test fixtures across the whole suite (untyped
`monkeypatch`/fixture params) — treat those as known debt, not something to fix incidentally.
`.github/workflows/ci.yml`'s `typecheck` job runs the exact scoped form (17 packages as of
2026-07-28):

```bash
.venv/bin/mypy -p connector_runtime -p transformation -p entity_resolution \
  -p analytics_publisher -p orchestration -p observability -p watermark_management \
  -p schema_management -p contracts -p governance -p tenancy -p config_propagation \
  -p data_quality -p workflow_automation -p portability -p semantic -p serving_store \
  -p knowledge -p persistence -p processing_engine
```

`knowledge`, `persistence`, and `processing_engine` were added on 2026-07-29. They had been
registered in `testpaths`, `coverage.source`, and `known-first-party` — the three places the
new-module checklist named — but **not** in this command, so the twin repository (which carries the
`scope_unit_id` security column) and the shared paging primitive were never type-checked. `agent` is
excluded deliberately while DL-04 is deferred. When you add a package, this is the fourth place.

**That command is green as of 2026-07-28** — the long-standing pre-existing error backlog
(29 errors in 11 files, previously tracked as remediation debt) was cleared as part of DL-SEC-18,
whose exit gate is "CI fully green including typecheck". So a mypy error you see now is almost
certainly yours: don't reach for "pre-existing" without confirming it against `HEAD`.

Bandit is likewise green as of 2026-07-28, and the CI job hard-fails on **any** finding. It had
been red at `HEAD` on 20 pre-existing findings. Per `pyproject.toml`'s `[tool.bandit]` ("No
skips"), the only permitted suppression is an inline `# nosec BXXX — <justification>`; the B608
suppressions on the SQL generators all point at the allowlist validation bandit cannot see.

For Terraform: `cd infrastructure/environments/<env> && terraform init -backend=false &&
terraform validate` — **and `cd infrastructure && terraform fmt -recursive -check`**, which is a
separate CI step that `validate` does not cover; three files passed validate while failing fmt. All three environments (`dev`, `staging`, `prod`) validate cleanly as of
2026-07-09 — the previously-tracked pre-existing `orchestration` module errors in
`staging`/`prod` were fixed by commit `138b692` (2026-07-08); this note just hadn't been updated
since. If you hit a validate error in staging/prod, treat it as new, not pre-existing debt.

Before claiming any mypy/ruff finding is pre-existing rather than something you introduced,
confirm it: `git show HEAD:<file> | .venv/bin/mypy -` (or the ruff/git-diff equivalent) — don't
just assert it.

## Wiring gates — why "tests pass" is not enough

On 2026-07-28 eighteen modules shipped complete, unit-tested, and **unreachable**: no deployed
Lambda, route, or script could reach them. Every existing gate stayed green, because a unit test
imports the module under test directly — which is precisely the import the handlers were missing.

Seven gates now make that class of defect detectable. Three run from the Makefile and CI:

Two more were added on 2026-07-29 after a second assessment found controls that were present and
inert: **G7** (`make security-columns`) fails when a scope filter reads a column no record declares
and no writer sets — the twin routes filtered on a field the model never carried — and the naming
gate was rewritten because its `grep` used BRE alternation under `grep -E` and therefore **could not
fail**. Both have committed negative tests, including a positive control.

```bash
make reachability            # G1: a production module with no production importer
make fail-open               # G4: a security parameter that can be omitted, nullified, or skipped
make traceability            # G5: a requirement uncited, unreachable, or covered by a stale waiver
make security-columns        # G7: a scope filter reading a column no writer sets
make paging-primitive        # G8: a hand-rolled DynamoDB paging loop outside persistence/
make tenant-session-adoption # G9: clients built outside the IAM tenant boundary
make workflow-integrity      # G12: a workflow referencing what CI cannot resolve
make wiring-gates            # all seven
```

**G4 checks three shapes, not one, and that is the lesson worth carrying.** Its first version
checked for a `None` *default*; the fix that satisfied it made every guarded parameter positionally
required while keeping `| None` and its early return, so the fail-open survived being fixed. It now
rejects the default, the nullable annotation, and an `is None` comparison. Where a read legitimately
has no caller claim, pass `tenancy.scope_predicate.unrestricted_predicate(reason)` — an affirmative,
metered object rather than an absence.

**G12** (`make workflow-integrity`) exists because every other gate runs locally, and so cannot see a
workflow referencing something the runner lacks. Two defects had CI red on `main` for months while
all local checks passed: `.secrets.baseline` matched `.gitignore`'s `*secret*` and was never
committed, so the secret-scan job failed on every run with `Invalid path`; and `requires-python` was
capped at `<3.14` while ci.yml pins `3.14.6`, so `pip install -e .` failed before lint, typecheck,
tests, or pip-audit executed a single assertion. **Gates in the `wiring-gates` job must import only
the standard library** — that job installs no dependencies on purpose, so a gate importing `yaml`
fails there and passes locally; `tests/test_workflow_integrity_gate.py` asserts this.

Local green is not CI green. Before claiming a change is verified, check the actual run
(`gh run list --branch <branch>`), because a job that dies during `Install dependencies` reports
failure without having run one check.

**G10** (`tests/test_capability_reachability.py`) closes G1's blind spot: G1 asks whether a *module*
is imported, so a capability with no path from any entry point passes it. That is how
`ExportService.execute` had no production caller through two audit passes while DL-PORT-01 read as
delivered.

Three more run as tests or infrastructure: **G2** asserts every supported source resolves while
importing *only* the extraction handler; **G3** (`tests/test_scope_call_sites.py`) asserts each
`ConsumptionSurface` applies the predicate **at its call site**, not just that the predicate object
behaves; **G6** alarms when a control metric publishes nothing, because an inert control is
indistinguishable from a healthy one.

**`requirements/WAIVERS.md` is load-bearing.** Anything unreachable or uncited must be waived there
with a reason and the plan item that will fix it — and G1/G5 **fail on a stale waiver**, so the file
cannot drift once the code catches up. Do not add a waiver to make a gate green; add it to record a
decision.

When you add a module, wire it to an entry point in the same change. "I'll wire it next session" is
what produced the eighteen.

## House rules (non-obvious, actually enforced)

- **No standalone comments above declarations. This one is absolute.** Do not put a comment on its
  own line above a `def`, `class`, attribute, assignment, or Terraform/YAML block. Not rationale,
  not history, not a warning, not a `# ─── Section ───` banner, not a one-liner. On 2026-07-30 the
  repo owner had 6,264 such lines deleted from 384 files, having raised it in prior sessions; treat
  it as a standing constraint, not a style preference.

  Specifically **do not** write the shape this codebase had accumulated — a retrospective
  explaining what once broke:

  ```python
  # Must track the secret path prefix. A Deny whose resource pattern matches nothing denies
  # nothing — this read `secret:edl/*` after the rename, leaving cross-tenant secret access
  # ungoverned while the policy still looked present (OWASP A01).
  resources = [...]
  ```

  If the reasoning is genuinely load-bearing, it belongs in one of the places that survives:
  the module or function **docstring**, a Pydantic `Field(description=...)`, a Terraform
  `description`, a **test name** that asserts the behaviour, or `docs/`. An incident belongs in
  `docs/KNOWN_GAPS_AND_ROADMAP.md`, not above the line it happened on. Prefer naming things so the
  comment is unnecessary. **Never re-add deleted narrative** when editing nearby code.

  Only four kinds of own-line comment are legitimate, because deleting them breaks a gate — the
  50 that remain repo-wide are all of these:
  `#checkov:skip=CKV_...`, `# nosec BXXX — <justification>`, `#!/usr/bin/env ...`, and the
  requirement-ID citations below. Trailing comments on the same line as code are also acceptable
  in moderation (`message_retention_seconds = 1209600 # 14 days, the maximum`).
- **Requirement-ID citations (`DL-XXX-NN`) are load-bearing — never delete one.** G5
  (`make traceability`, `scripts/check_requirement_traceability.py`) greps raw file text for
  `\bDL-[A-Z]+-\d+\b` and **fails** when an active requirement has no citation. Cite the id in a
  docstring, a `Field(description=...)`, or a test name — not in a narrative comment. Two
  Terraform sites (`DL-SERV-08` in `modules/transformation_lambda/main.tf`, `DL-WF-08` in
  `modules/metadata_persistence/programme_tables.tf`) carry a deliberate one-line comment because
  HCL offers nowhere else to put the string; leave them alone. Run `make traceability` after any
  bulk edit that touches comments or docstrings.
- **Banned identifiers**: `helper`, `util`, `common`, `manager` (and `Helper`/`Util`/`Common`/
  `Manager` classes) are rejected by `make banned-names` — name things by domain concept instead
  (see `PROHIBITED_IDENTIFIERS` in `contracts/identifier_policy.py`). This is now also enforced in
  CI, not just locally — a dedicated `banned-names` job (`.github/workflows/ci.yml`) runs
  `make banned-names` on every PR.
- **ID/tenant validation lives in exactly one place**: `contracts/identifier_policy.py`
  (`STABLE_ID_PATTERN`, `TENANT_CODE_PATTERN`, `DEFAULT_TENANT_CODE = "demo"`,
  `tenant_scoped_key()`). Never re-derive these regexes elsewhere.
- **`tenant_code` is always prefixed**, including the default tenant (`"demo"`) — no
  special-casing. Established in every repository class (`ConfigurationRepositoryClient`,
  `WatermarkRepository`, `SchemaSnapshotRepository`, `EntityTypeRegistryClient`).
- **Not everything `tenant_code` touches is isolated the same way** — see
  `docs/PIPELINE_FLOW.md`'s canonical isolation-model table for the current, layer-by-layer truth
  (S3, each DynamoDB table, Secrets Manager, the control plane, Glue/Athena, the serving store).
  Nothing is IAM-enforced yet anywhere (tracked in `docs/KNOWN_GAPS_AND_ROADMAP.md`). Run
  `tests/test_tenant_isolation.py` before touching any repository class — it's the single
  regression test covering every isolation mechanism, and it now has **zero skipped tests**: the
  old Secrets-Manager placeholder was replaced by real assertions once credentials became
  per-connection (`TestSecretsManagerConnectionIsolation`), and
  `TestScopeIsolationAcrossEverySurface` parameterises the same adversarial checks over every
  `ConsumptionSurface`.
- **`extra="forbid"`** is used specifically on config/params/API-boundary Pydantic models
  (`EntityExtractionConfig`, the `*_params.py` connector models, `connector_runtime/api/models.py`)
  — not universal; don't assume every model has it.
- **Cite the OWASP category** (`OWASP A03`, `A09`, etc.) on security-relevant code — a real,
  consistently-applied convention (209 occurrences repo-wide as of 2026-07-30), not aspirational.
  Put it **in the docstring**, never in a comment above the declaration: after the 2026-07-30
  comment sweep all 209 live in docstrings and `Field(description=...)` strings, and zero live in
  comments. Adding one as a comment would both violate the rule above and make this count wrong.
- **Lambda handler pattern** (canonical example: `analytics_publisher/analytics_publisher_handler.py`):
  thin `lambda_handler` → `_validate_event` → `check_lambda_timeout(...)` / `configure_xray(...)`
  → `structlog.contextvars.bind_contextvars(run_id=..., tenant_code=..., ...)` → delegate to a
  private `_run_<thing>(...)` function → `try/except` logs a structured error event and re-raises
  → `finally: clear_contextvars()`. Skipping the `finally` leaks stale context into the next
  invocation on a warm container — this was a real, previously-fixed bug. Never trust bucket/table
  names from the event payload; read them via `require_env(...)` from `observability.lambda_runtime`.
  **New handlers should use `observability/stage_execution.py::stage_execution(...)` instead of
  hand-rolling that boilerplate** — it makes the clear, the flush, the duration metric, and a
  failure record on both an exception and a hard Lambda kill structurally impossible to forget
  (`connector_runtime/webhook_receiver_handler.py` and `writeback_handler.py` are the examples).
- Every domain module owns its own `<module>/tests/`, and a new package must be registered in
  **six** places: `pyproject.toml`'s `testpaths`, `[tool.coverage.run].source`, isort
  `known-first-party`, the hatch wheel `packages` list, the `Makefile`'s `lambda-package` copy list,
  and the CI `typecheck` mypy scope. `tests/test_package_registration.py` (G11) reconciles all six —
  added after `persistence/` was registered in four of them and was consequently absent from both the
  wheel and the Lambda package, which seventeen modules import. The suite stayed green because it
  imports from the working tree, not the artefact. The older instance of the same gap was
  `analytics_publisher/tests` and `connector_runtime/tests/sage`, unrun until 2026-07-07.
- Top-level `tests/` is for cross-cutting integration tests only (e.g. tenant isolation) — not a
  dumping ground for module-specific tests, which belong under their own module's `tests/`.
- **Metrics are declared once**, in `contracts/platform_metrics.py::PlatformMetric`, and
  `observability/tests/test_alarm_emitter_reconciliation.py` reconciles that catalogue against the
  Terraform alarms **bidirectionally**: no alarm without a producer, no catalogued metric without
  an alarm. If it fails, wire the real `record_platform_metric(...)` call at the point the event
  happens — don't relax the assertion, and only add an `_INFRASTRUCTURE_PRODUCED` exemption when
  an AWS service genuinely emits the metric.
- Domain modules never hold a CloudWatch client: they call
  `observability.metric_recorder.record_platform_metric(...)`, and
  `observability/stage_execution.py::StageExecution` drains the recorder in its `finally`.

## Safety guardrails (enforced by hooks, not just convention)

`.claude/settings.json` hard-blocks two operations at the tool level, regardless of session
context: `terraform apply`/`destroy` against `infrastructure/environments/prod`, and
`git push --force` (and `-f`). These apply even if a long session loses track of earlier caution.
If either is genuinely needed, tell the user to run it themselves.

## Slash commands available in this repo

- `/verify` — run the full local verification suite (ruff, scoped mypy, pytest, bandit, and
  Terraform validate if IaC changed) and report a concise pass/fail, mirroring CI
- `/new-connector` — scaffold a new source connector following the existing adapter pattern
  (interface, credential client, raw layer writer, query builder, params model, Terraform secret)
