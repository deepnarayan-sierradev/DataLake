# CLAUDE.md — infrastructure/

Terraform, modular structure under `infrastructure/modules/*`, wired **identically** into all
three environments (`infrastructure/environments/{dev,staging,prod}/main.tf`) — the same module
blocks in each (the `kms` module is invoked 4× per environment for distinct storage/database/
secrets/logs keys). If you change wiring in one environment's `main.tf`, mirror it in the other
two unless there's a documented reason not to.

**Modules**: `analytics_publisher_lambda`, `control_plane`, `entity_resolution_lambda`, `glue`,
`iam`, `kms`, `lambda_pipeline`, `metadata_persistence`, `networking`, `observability`,
`orchestration`, `secrets`, `serving_store_database`, `serving_store_lambda`, `storage`,
`transformation_lambda`. The two `serving_store_*` modules are wired into all three environments'
`main.tf` but have not been `terraform apply`'d anywhere yet — see `docs/PLATFORM_STATUS.md`.

## Verify

```bash
cd infrastructure/environments/<env> && terraform init -backend=false && terraform validate
```

All three environments (`dev`/`staging`/`prod`) validate cleanly (re-confirmed 2026-07-09). Re-run
`terraform validate` in all three environment directories after touching a shared module or any
environment's `main.tf`, and correct this file in the same change if the result changes — don't
let this note drift from the code.

**`dev`-specific quirk:** `dev` has already been through a real `terraform init` against its live
S3 backend, so `.terraform/` there holds real provider state. Re-running `terraform init
-backend=false` in `dev` prints a `No valid credential sources found` error from the backend
reconcile step before `terraform validate` still passes cleanly off the cached providers — that's
an artifact of dev being a real, deployed environment, not a config problem. `staging`/`prod`
init cleanly with no such error since they're not yet bootstrapped.

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
- **All 6 DynamoDB tables are Terraform-managed** (`module.metadata_persistence`:
  `entity_extraction_config`, `entity_type_registry`, `run_audit_log`, `source_onboarding_registry`,
  `watermark_repository`, `serving_store_config`) — real `aws_dynamodb_table` resources in
  `infrastructure/modules/metadata_persistence/main.tf`. Don't create any of them by hand.
- **After any `terraform apply`**, `scripts/seed_schedules.py` must be re-run or no EventBridge
  cron triggers exist for the deployed pipelines (per the Makefile's own comment on the
  `seed-schedules` target) — this is not automatic and Terraform won't remind you.
- IAM policies in `infrastructure/modules/iam/` should be resource-scoped, never wildcard
  (`Resource = "*"`) — this is enforced by the PR template's security checklist and by
  `CODEOWNERS` requiring security review on `infrastructure/modules/iam/` and `.../secrets/`.
- **Lambda `environment.variables` must never set `AWS_REGION`** (or any other AWS-reserved key:
  `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_SESSION_TOKEN`, `AWS_EXECUTION_ENV`,
  `_HANDLER`, etc.) — Lambda's `CreateFunction`/`UpdateFunctionConfiguration` reject the whole
  request if you do. It's injected automatically at runtime. Every Lambda module documents this
  with a comment instead of setting the value (`# AWS_REGION — injected automatically by Lambda
  runtime`); match that pattern if you add a new one.
- **An SQS queue's `visibility_timeout_seconds` must be ≥ the timeout of any Lambda consuming it
  via `aws_lambda_event_source_mapping`**, or `CreateEventSourceMapping` is rejected. Check this
  pairing explicitly whenever you add or resize a queue-triggered Lambda — Terraform's own
  validation won't catch a mismatch, only a real `apply` will.
- **A pending change in any module can force spurious replacement of resources in modules that
  consume its outputs**, even when nothing about those resources actually changes. Terraform defers
  zero-argument data source reads (`data.aws_region`, `data.aws_caller_identity`, `data.aws_vpc`)
  to apply-time whenever the containing module "depends on a module with changes pending" (visible
  in `terraform plan` as `# (depends on a resource or a module with changes pending)`), which makes
  every `ForceNew` attribute computed from that data source show as `(known after apply)` — e.g. a
  security group's `vpc_id` or a Lambda permission's `source_arn` — and Terraform then plans a full
  destroy+recreate for it, even though the real value won't change. **Mitigation:** land small,
  unrelated fixes via `terraform apply -target=<specific resource>` first so the dependency graph
  is quiescent (no pending changes anywhere) before running a full-environment `plan`/`apply` —
  don't let an unrelated one-line fix drag in unnecessary security-group churn.
- **Never assume an AWS account is clean before a first `terraform init`/`apply`, even in an
  environment nothing has "officially" deployed to.** A deployment torn down by deleting only the
  big, visible resources (S3 buckets, Lambda functions, IAM roles, DynamoDB tables, the Terraform
  state bucket) instead of via `terraform destroy` can leave SQS queues, Secrets Manager secrets,
  CloudWatch Logs query definitions, an X-Ray group, a Glue catalog resource policy, or an
  EventBridge Scheduler group behind — any of which blocks the next `apply` with
  `AlreadyExists`/`Conflict` errors. **Always tear down with `terraform destroy`, never a
  manual/partial cleanup.** Before bootstrapping any environment, run a quick inventory
  (`aws sqs list-queues`, `aws secretsmanager list-secrets --include-planned-deletion`,
  `aws logs describe-query-definitions`, `aws xray get-groups`, `aws glue get-resource-policy`,
  `aws scheduler list-schedule-groups`, filtered to that account/region) to rule out this
  situation before assuming a truly empty account.
- **Lambda deployment package builds are not byte-reproducible.** `make lambda-package` installs
  unpinned-version dependencies (`pyproject.toml` uses ranges, e.g. `pydantic>=2.7,<3.0`), so two
  consecutive builds with no source change can still produce different SHA-256 hashes. Because
  `lambda-upload` depends on `lambda-package` in the `Makefile`, running them as two separate
  commands silently rebuilds a *second*, possibly different artifact before uploading it —
  invalidating any hash copied from the first build's output. **Always use `make lambda-deploy` as
  a single command** (it builds once, uploads that exact artifact, and computes the hash from it
  afterward) instead of chaining `lambda-package` → copy hash → `lambda-upload` by hand. It updates
  all eight Lambda functions in one pass (extraction, transformation, entity-resolution,
  analytics-publisher, control-plane, pipeline-trigger, dlq-processor, credential-expiry-notifier),
  and writes the deployed hash back into `infrastructure/environments/dev/terraform.tfvars` —
  without that, a later plain `terraform apply`/`plan` reads the stale committed hash and plans to
  revert every Lambda's code back to whatever was last committed there.
