.PHONY: install lint format typecheck test banned-names security-scan audit \
        iac-validate iac-scan iac-fmt-check iac-fmt \
        lambda-package lambda-upload lambda-deploy \
        seed-entity-config seed-schedules clean help

# ─── Help ────────────────────────────────────────────────────────────────────
help:
	@echo "Enterprise Data Lake — development targets"
	@echo ""
	@echo "  install             Install all dev dependencies and pre-commit hooks"
	@echo "  lint                Run ruff linter (check only)"
	@echo "  format              Run ruff formatter"
	@echo "  banned-names        Fail if prohibited generic identifiers appear in production code"
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
	@echo "  seed-schedules      Create/sync EventBridge Scheduler schedules from DynamoDB (dev)"
	@echo "                      REQUIRED after every terraform apply — without it no cron triggers exist"
	@echo ""
	@echo "Required env vars for lambda-upload / seed-entity-config / seed-schedules:"
	@echo "  ARTIFACTS_BUCKET    S3 bucket for Lambda zip (e.g. dev-edl-terraform-state)"
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

# Enforce naming standards: prohibited generic identifiers must not appear as
# class or function names in production source code (spec §10.4).
# Permitted exceptions: test fixtures, scripts, and the checklist itself.
BANNED_PATTERN := 'def helper\b\|def util\b\|def common\b\|class Helper\b\|class Util\b\|class Common\b\|class Manager\b'
BANNED_EXCLUDE_PATHS := .venv scripts

banned-names:
	@echo "Checking for prohibited generic identifiers..."
	@if grep -rn --include='*.py' \
		--exclude-dir='.venv' \
		--exclude-dir='tests' \
		--exclude-dir='scripts' \
		-E $(BANNED_PATTERN) .; then \
		echo ""; \
		echo "ERROR: Prohibited generic identifiers found (helper/util/common/manager)."; \
		echo "Rename these to domain-specific identifiers per spec §10.4."; \
		exit 1; \
	else \
		echo "OK — no prohibited generic identifiers found."; \
	fi

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

ARTIFACTS_BUCKET ?= dev-edl-terraform-state
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
		pydantic boto3 botocore structlog python-dateutil requests pyarrow pymysql
	# Copy platform source packages into build directory
	@for pkg in contracts connector_runtime schema_management watermark_management observability orchestration transformation governance entity_resolution analytics_publisher; do \
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
		-target=module.lambda_pipeline \
		-var="lambda_package_s3_bucket=$(ARTIFACTS_BUCKET)" \
		-var="lambda_package_s3_key=$(LAMBDA_S3_KEY)" \
		-var="lambda_package_source_hash=$$HASH" \
		-auto-approve
	@echo "Lambda deployment complete."

# ─── Entity Config Seeder ────────────────────────────────────────────────────

seed-entity-config:
	@echo "Writing entity config records to DynamoDB (dev)..."
	python scripts/seed_entity_config.py \
		--environment dev \
		--region $(AWS_REGION)
	@echo "Entity config seed complete. Run 'make seed-schedules' to sync EventBridge schedules."

# Sync EventBridge Scheduler schedules from DynamoDB entity config.
# Must be run after every terraform apply (creates the schedule group)
# and after seed-entity-config (populates schedule_cron / schedule_enabled fields).
# Without this step, no cron triggers exist and the pipeline never runs automatically.
seed-schedules:
	@echo "Syncing EventBridge Scheduler schedules from DynamoDB (dev)..."
	python scripts/seed_schedules.py \
		--environment dev
	@echo "Schedule sync complete."

# ─── Clean ───────────────────────────────────────────────────────────────────
clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -name "*.pyc" -delete
	find . -name "*.pyo" -delete
	rm -rf .pytest_cache .mypy_cache .ruff_cache dist *.egg-info
	@echo "Clean complete."

# ─── Full CI gate (mirrors CI pipeline locally) ──────────────────────────────
ci: lint typecheck security-scan audit test iac-validate iac-scan
	@echo "All CI gates passed."
