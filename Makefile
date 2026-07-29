.PHONY: install lint format typecheck test banned-names security-scan audit \
        reachability fail-open traceability security-columns wiring-gates \
        iac-validate iac-scan iac-fmt-check iac-fmt \
        lambda-package lambda-upload lambda-deploy \
        seed-entity-config seed-serving-store-config seed-schedules \
        seed-semantic-model migrate-connections migrate-credentials \
        backfill-scope-attribution clean help

# ─── Help ────────────────────────────────────────────────────────────────────
help:
	@echo "Enterprise Data Lake — development targets"
	@echo ""
	@echo "  install             Install all dev dependencies and pre-commit hooks"
	@echo "  lint                Run ruff linter (check only)"
	@echo "  format              Run ruff formatter"
	@echo "  banned-names        Fail if prohibited generic identifiers appear in production code"
	@echo "  reachability        Fail if a production module has no production importer (G1)"
	@echo "  fail-open           Fail if a security parameter defaults to None (G4)"
	@echo "  traceability        Fail if a requirement is uncited or unreachable (G5)"
	@echo "  security-columns    Fail if a filtered column has no writer or declaration (G7)"
	@echo "  wiring-gates        Run all four wiring gates together"
	@echo "  typecheck           Run mypy strict type checking"
	@echo "  test                Run test suite with coverage (≥80% required)"
	@echo "  security-scan       Run bandit SAST security scan"
	@echo "  audit               Run pip-audit dependency vulnerability scan"
	@echo "  iac-validate        Run terraform validate on all environments"
	@echo "  iac-scan            Run checkov IaC security policy scan"
	@echo ""
	@echo "  lambda-package      Build Lambda zip from source (dist/extraction-pipeline.zip)"
	@echo "  lambda-upload       Upload Lambda zip to S3 artifacts bucket"
	@echo "  lambda-deploy       Package + upload + terraform apply (Lambda only)"
	@echo ""
	@echo "  seed-entity-config  Write entity config records to DynamoDB (dev)"
	@echo "  seed-serving-store-config  Onboard tenant/entity pairs to the serving store (dev)"
	@echo "  seed-schedules      Create/sync EventBridge Scheduler schedules from DynamoDB (dev)"
	@echo "                      REQUIRED after every terraform apply — without it no cron triggers exist"
	@echo ""
	@echo "Required env vars for lambda-upload / seed-entity-config / seed-schedules:"
	@echo "  ARTIFACTS_BUCKET    S3 bucket for Lambda zip (e.g. edl-terraform-state-087972550871)"
	@echo "  AWS_PROFILE         AWS CLI profile to use (or leave unset for default)"
	@echo "  AWS_REGION          Default: us-east-1"
	@echo ""

# ─── Setup ───────────────────────────────────────────────────────────────────
install:
	pip install -e ".[dev]"
	pre-commit install
	@echo "Installation complete. Pre-commit hooks installed."

# ─── Code Quality ────────────────────────────────────────────────────────────
lint:
	ruff check .

# Enforce naming standards: prohibited generic identifiers must not appear as class names,
# function names, module filenames, or package directories (spec §10.4).
#
# This was a `grep` until 2026-07-29, and it could not fail: the pattern used BRE alternation
# (\|) but ran under `grep -E`, where \| is a literal pipe, so it only matched the literal text
# "def helper|def util|...". A file containing `def helper():` passed. The replacement is a
# script so it can also match suffixes and filenames, and so it can be tested —
# tests/test_prohibited_identifiers_gate.py feeds it known-bad input and asserts it fails.

banned-names:
	@python scripts/check_prohibited_identifiers.py

# ─── Wiring gates (G1, G4, G5, G7) ───────────────────────────────────────────
# These exist because "module written + unit tests green" was the definition of done that let
# eighteen unreachable modules ship on 2026-07-28. A unit test imports the module under test
# directly, which is precisely the import a deployed handler was missing.
#
# G7 was added after the follow-up audit: the twin routes filtered on a column the model never
# carried, so a reachable module with a green call-site gate still applied no filter at all.

reachability:
	@python scripts/check_module_reachability.py

fail-open:
	@python scripts/check_fail_open_defaults.py

traceability:
	@python scripts/check_requirement_traceability.py

security-columns:
	@python scripts/check_security_column_writers.py

paging-primitive:
	@python scripts/check_paging_primitive.py

wiring-gates: reachability fail-open traceability security-columns paging-primitive

format:
	ruff format .
	ruff check --fix .

typecheck:
	mypy .

# ─── Tests ───────────────────────────────────────────────────────────────────
test:
	pytest

test-unit:
	pytest -m "not integration"

test-integration:
	pytest -m "integration"

# ─── Security ────────────────────────────────────────────────────────────────
security-scan:
	bandit -r . --exclude .venv,tests,dist -c pyproject.toml

audit:
	pip-audit --requirement <(pip freeze) --strict

# ─── Infrastructure ──────────────────────────────────────────────────────────
iac-validate:
	@for env in dev staging prod; do \
		echo "Validating $$env..."; \
		cd infrastructure/environments/$$env && terraform init -backend=false && terraform validate; \
		cd ../../..; \
	done

iac-scan:
	checkov -d infrastructure/ \
		--framework terraform \
		--output cli \
		--compact \
		--soft-fail false

iac-fmt-check:
	terraform fmt -recursive -check infrastructure/

iac-fmt:
	terraform fmt -recursive infrastructure/

# ─── Lambda Packaging ────────────────────────────────────────────────────────
#
# NOT byte-reproducible: pyproject.toml pins dependency *ranges*, not exact
# versions, so two builds with no source change can still hash differently.
# Always run 'make lambda-deploy' as a single command (builds once, uploads
# that exact artifact, hashes it after) — never chain 'lambda-package' (copy
# the printed hash) then 'lambda-upload' by hand, since 'lambda-upload'
# depends on 'lambda-package' and will silently rebuild a second, possibly
# different artifact before uploading it. Hit live during dev's first real
# deployment (2026-07-09) — see infrastructure/CLAUDE.md for the incident.

ARTIFACTS_BUCKET ?= edl-terraform-state-087972550871
AWS_REGION       ?= us-east-1
LAMBDA_S3_KEY    ?= lambda/extraction-pipeline.zip
LAMBDA_ZIP       := dist/extraction-pipeline.zip
LAMBDA_BUILD_DIR := dist/lambda-build

lambda-package:
	@echo "Building Lambda deployment package..."
	@rm -rf $(LAMBDA_BUILD_DIR) && mkdir -p $(LAMBDA_BUILD_DIR)
	# Install production dependencies into the build directory
	pip install \
		--quiet \
		--target $(LAMBDA_BUILD_DIR) \
		--platform manylinux2014_x86_64 \
		--python-version 3.13 \
		--only-binary=:all: \
		pydantic boto3 botocore structlog python-dateutil requests pyarrow pymysql duckdb
	# Copy platform source packages into build directory. processing_engine/knowledge/
	# semantic are imported by the control-plane Lambda (cold-start) and the twin builder;
	# omitting them makes those Lambdas fail to import. agent ships for the deferred layer.
	@for pkg in contracts connector_runtime schema_management watermark_management observability orchestration transformation governance entity_resolution analytics_publisher processing_engine knowledge semantic agent serving_store tenancy config_propagation data_quality workflow_automation portability; do \
		cp -r $$pkg $(LAMBDA_BUILD_DIR)/$$pkg; \
	done
	@mkdir -p dist
	@rm -f $(LAMBDA_ZIP)
	cd $(LAMBDA_BUILD_DIR) && zip -q -r ../../$(LAMBDA_ZIP) .
	@echo "Package built: $(LAMBDA_ZIP)"
	@echo "SHA-256 (base64):"
	@openssl dgst -sha256 -binary $(LAMBDA_ZIP) | openssl base64

lambda-upload: lambda-package
	@echo "Uploading $(LAMBDA_ZIP) to s3://$(ARTIFACTS_BUCKET)/$(LAMBDA_S3_KEY)..."
	aws s3 cp $(LAMBDA_ZIP) s3://$(ARTIFACTS_BUCKET)/$(LAMBDA_S3_KEY) \
		--region $(AWS_REGION) \
		--sse aws:kms
	@echo "Upload complete."

lambda-deploy: lambda-upload
	@echo "Deploying Lambda via Terraform..."
	@HASH=$$(openssl dgst -sha256 -binary $(LAMBDA_ZIP) | openssl base64); \
	cd infrastructure/environments/dev && \
	terraform apply \
		-target=module.lambda_pipeline.aws_lambda_function.extraction_pipeline \
		-target=module.transformation_lambda.aws_lambda_function.transformation_pipeline \
		-target=module.entity_resolution_lambda.aws_lambda_function.entity_resolution_pipeline \
		-target=module.analytics_publisher_lambda.aws_lambda_function.analytics_publisher \
		-target=module.control_plane.aws_lambda_function.control_plane \
		-target=module.secrets.aws_lambda_function.credential_expiry_notifier \
		-target=module.orchestration.aws_lambda_function.pipeline_trigger \
		-target=module.orchestration.aws_lambda_function.dlq_processor \
		-var="lambda_package_s3_bucket=$(ARTIFACTS_BUCKET)" \
		-var="lambda_package_s3_key=$(LAMBDA_S3_KEY)" \
		-var="lambda_package_source_hash=$$HASH" \
		-auto-approve && \
	sed -i.bak "s|^lambda_package_source_hash.*|lambda_package_source_hash = \"$$HASH\"|" terraform.tfvars && \
	rm -f terraform.tfvars.bak
	@echo "Lambda deployment complete. terraform.tfvars updated with the deployed hash —"
	@echo "a plain 'terraform apply' now matches what's actually running, no manual sync needed."

# ─── Entity Config Seeder ────────────────────────────────────────────────────

seed-entity-config:
	@echo "Writing entity config records to DynamoDB (dev)..."
	python scripts/seed_entity_config.py \
		--environment dev \
		--region $(AWS_REGION)
	@echo "Entity config seed complete. Run 'make seed-schedules' to sync EventBridge schedules."

# Onboard tenant/entity pairs to the serving store (populates EdlServingStoreConfig).
# Without this, the LoadServingStore stage skips every run and the serving RDS stays empty.
seed-serving-store-config:
	@echo "Writing serving store config records to DynamoDB (dev)..."
	python scripts/seed_serving_store_config.py \
		--environment dev \
		--region $(AWS_REGION)
	@echo "Serving store config seed complete."

# Sync EventBridge Scheduler schedules from DynamoDB entity config.
# Must be run after every terraform apply (creates the schedule group)
# and after seed-entity-config (populates schedule_cron / schedule_enabled fields).
# Without this step, no cron triggers exist and the pipeline never runs automatically.
seed-schedules:
	@echo "Syncing EventBridge Scheduler schedules from DynamoDB (dev)..."
	python scripts/seed_schedules.py \
		--environment dev
	@echo "Schedule sync complete."

# ─── SOW programme: semantic model + DL-12 migrations ────────────────────────
#
# Both migrations are dry-run by default and must be run with APPLY=--apply BEFORE the
# connection-aware code deploys to an environment (DL-SCOPE-05). migrate-connections must run
# first: credential paths are derived from the connection model it creates.

TENANT_CODE  ?= demo
CURATED_BUCKET ?= edl-curated-087972550871
PUBLISHED_BY ?= platform-team
APPLY        ?=

seed-semantic-model:
	@echo "Publishing the enterprise semantic model for $(TENANT_CODE) (draft until signed)..."
	python scripts/seed_enterprise_semantic_model.py \
		--tenant-code $(TENANT_CODE) \
		--bucket $(CURATED_BUCKET) \
		--region $(AWS_REGION) \
		--published-by $(PUBLISHED_BY) \
		$(APPLY)

migrate-connections:
	@echo "Migrating to connection identity (DL-SCOPE-05)..."
	python scripts/migrate_to_connection_identity.py --region $(AWS_REGION) $(APPLY)

backfill-scope-attribution:
	@echo "Backfilling scope_unit_id onto pre-DL-SCOPE-07 partitions (dry-run unless APPLY=1)..."
	python scripts/backfill_scope_attribution.py \
		--tenant-code $(TENANT_CODE) \
		--connection-id $(CONNECTION_ID) \
		--bucket $(BUCKET) \
		--strategy $(or $(STRATEGY),report) \
		$(if $(APPLY),--apply --confirm-rewrite,)

migrate-credentials:
	@echo "Migrating credentials to per-connection paths (DL-SEC-05)..."
	python scripts/migrate_credentials_to_connection_paths.py --region $(AWS_REGION) $(APPLY)

# ─── Clean ───────────────────────────────────────────────────────────────────
clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -name "*.pyc" -delete
	find . -name "*.pyo" -delete
	rm -rf .pytest_cache .mypy_cache .ruff_cache dist *.egg-info
	@echo "Clean complete."

# ─── Full CI gate (mirrors CI pipeline locally) ──────────────────────────────
ci: lint typecheck banned-names wiring-gates security-scan audit test iac-validate iac-scan
	@echo "All CI gates passed."
