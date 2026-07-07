# CLAUDE.md — infrastructure/

Terraform, modular structure under `infrastructure/modules/*`, wired **identically** into all
three environments (`infrastructure/environments/{dev,staging,prod}/main.tf`) — the same module
blocks in each (the `kms` module is invoked 4× per environment for distinct storage/database/
secrets/logs keys). If you change wiring in one environment's `main.tf`, mirror it in the other
two unless there's a documented reason not to.

**Modules**: `analytics_publisher_lambda`, `control_plane`, `entity_resolution_lambda`, `glue`,
`iam`, `kms`, `lambda_pipeline`, `metadata_persistence`, `networking`, `observability`,
`orchestration`, `secrets`, `storage`, `transformation_lambda`.

## Verify

```bash
cd infrastructure/environments/<env> && terraform init -backend=false && terraform validate
```

Only `dev` validates cleanly today. `staging`/`prod` have 7 pre-existing errors on the
`orchestration` module block (missing `lambda_package_s3_key`, `lambda_package_s3_bucket`,
`lambda_package_source_hash`, `run_audit_log_table_name`, `extraction_failure_dlq_arn`,
`pipeline_trigger_role_arn`, `dlq_processor_role_arn`) — confirmed pre-existing via `git diff`,
not introduced by any recent change. Don't treat these as something you broke unless `git diff`
on the `orchestration` module block itself says otherwise.

Local checks mirror CI: `make iac-validate` (loops dev/staging/prod), `make iac-scan` (checkov),
`terraform fmt -recursive -check infrastructure/`. CI additionally pins every third-party GitHub
Action to a full commit SHA (OWASP A03 supply-chain hardening) — match that convention if you
touch `.github/workflows/*.yml`.

## Hard rules

- **Never run `terraform apply`/`plan` against a real AWS account without the user's explicit
  go-ahead.** `terraform apply`/`destroy` against `infrastructure/environments/prod` is hard-blocked
  at the tool level by a PreToolUse hook (see root `CLAUDE.md` and `.claude/settings.json`)
  regardless of session context. `dev`/`staging` apply is *not* blocked at the tool level, but
  still needs explicit sign-off per this repo's own safety norms — the hook is a backstop, not a
  substitute for asking.
- **DynamoDB tables are NOT uniformly Terraform-managed.** `entity-extraction-config`,
  `watermark-repository`, and `run-audit-log` must be created manually per environment (see
  `docs/PLATFORM_STATUS.md`). `entity-type-registry` IS Terraform-managed (`metadata_persistence`
  module). Don't assume every table follows the same lifecycle.
- **After any `terraform apply`**, `scripts/seed_schedules.py` must be re-run or no EventBridge
  cron triggers exist for the deployed pipelines (per the Makefile's own comment on the
  `seed-schedules` target) — this is not automatic and Terraform won't remind you.
- IAM policies in `infrastructure/modules/iam/` should be resource-scoped, never wildcard
  (`Resource = "*"`) — this is enforced by the PR template's security checklist and by
  `CODEOWNERS` requiring security review on `infrastructure/modules/iam/` and `.../secrets/`.
