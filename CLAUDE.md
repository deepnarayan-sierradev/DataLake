# CLAUDE.md — Enterprise Data Lake Platform

Metadata-driven, connector-based multi-tenant data lake on AWS Lambda / Step Functions /
DynamoDB / S3, provisioned via Terraform. Python 3.14, strict typing throughout.

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
- Read in this order, each superseding the previous **for sequencing only, not design detail**:
  `architecture/IMPROVEMENT_PLAN.md` → `architecture/GAP_ANALYSIS_FINDINGS.md` →
  `architecture/MULTI_TENANT_ROLLOUT_PLAN.md`. The findings doc's "Implementation status" table
  is the current source of truth for what's done vs. partial vs. deferred.
- `.github/pull_request_template.md` — the actual quality bar (CI gates, security checklist,
  naming standard) — follow it when preparing a PR description
- `.github/CODEOWNERS` — path-based review ownership

## Verification — the commands that actually work

Invoke tools via `.venv/bin/<tool>` explicitly rather than assuming an activated venv — shell
activation doesn't reliably persist across separate tool calls in an agent session.

```bash
.venv/bin/ruff check .                                          # lint — matches CI exactly
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
As of 2026-07-08, `.github/workflows/ci.yml`'s `typecheck` job was itself fixed to stop running
bare `mypy .` (which crashed on the same duplicate-module collision) and now runs the exact scoped
form: `mypy -p connector_runtime -p transformation -p entity_resolution -p analytics_publisher
-p orchestration -p observability -p watermark_management -p schema_management -p contracts
-p governance`. Once unblocked, that command surfaces **75 pre-existing type errors across 16
files** (confirmed via `git show HEAD:<file> | .venv/bin/mypy -` on the affected files — pre-existing,
not introduced by the 2026-07-08 pass) — tracked as follow-up remediation debt (`architecture/GAP_ANALYSIS_FINDINGS.md`,
`OBS-6`), not something to fix incidentally per the untyped-test-fixture warning above. The CI
type-check job will report red on this debt until it's separately remediated.

For Terraform: `cd infrastructure/environments/<env> && terraform init -backend=false &&
terraform validate`. Only `dev` is guaranteed clean — `staging`/`prod` have pre-existing,
unrelated missing-argument errors on the `orchestration` module block (confirmed via `git diff`
to predate any recent change — don't assume you broke something there).

Before claiming any mypy/ruff finding is pre-existing rather than something you introduced,
confirm it: `git show HEAD:<file> | .venv/bin/mypy -` (or the ruff/git-diff equivalent) — don't
just assert it.

## House rules (non-obvious, actually enforced)

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
- **Not everything `tenant_code` touches is IAM-enforced yet**: S3 prefixes, the
  `entity-type-registry` table, and (as of the `ARCH-1` fix, 2026-07-08) `watermark-repository`
  are genuinely isolated at the key/prefix level (`tenant_scoped_key()` on the DynamoDB partition
  key, or an S3 prefix an IAM bucket-policy condition could enforce). `entity-extraction-config`
  is still only isolated by an application-level guard (`_enforce_tenant_match`) — see `ARCH-12`
  in the findings doc, deliberately deferred (manifests as a 409-on-onboarding-conflict, not a
  data leak, since the guard fails closed on read). Neither of these is IAM-enforced yet
  regardless (`SEC-2`, tracked follow-up). Run `tests/test_tenant_isolation.py` before touching
  any repository class — it's the single regression test covering every isolation mechanism, and
  the one place a Secrets-Manager isolation gap is deliberately tracked via a skipped placeholder
  rather than a fake pass.
- **`extra="forbid"`** is used specifically on config/params/API-boundary Pydantic models
  (`EntityExtractionConfig`, the `*_params.py` connector models, `connector_runtime/api/models.py`)
  — not universal; don't assume every model has it.
- **Cite the OWASP category** (`OWASP A03`, `A09`, etc.) in security-relevant code comments —
  this is a real, consistently-applied convention (150+ occurrences repo-wide), not aspirational.
- **Lambda handler pattern** (canonical example: `analytics_publisher/analytics_publisher_handler.py`):
  thin `lambda_handler` → `_validate_event` → `check_lambda_timeout(...)` / `configure_xray(...)`
  → `structlog.contextvars.bind_contextvars(run_id=..., tenant_code=..., ...)` → delegate to a
  private `_run_<thing>(...)` function → `try/except` logs a structured error event and re-raises
  → `finally: clear_contextvars()`. Skipping the `finally` leaks stale context into the next
  invocation on a warm container — this was a real bug (`OBS-1`). Never trust bucket/table names
  from the event payload; read them via `require_env(...)` from `observability.lambda_utils`.
- Every domain module owns its own `<module>/tests/`, registered in `pyproject.toml`'s
  `testpaths` (and `[tool.coverage.run].source`, and `known-first-party` for isort) — if you add
  a new module with tests, register it in all three or it silently never runs in CI. This exact
  gap existed for `analytics_publisher/tests` and `connector_runtime/tests/sage` until 2026-07-07.
- Top-level `tests/` is for cross-cutting integration tests only (e.g. tenant isolation) — not a
  dumping ground for module-specific tests, which belong under their own module's `tests/`.

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
