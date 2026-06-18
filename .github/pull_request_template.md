## Pull Request Checklist

Before requesting review, confirm all items below. Non-optional items will be
verified by automated CI pipeline gates.

---

### Description

<!-- What does this PR do? Reference the implementation plan phase and section. -->

**Phase / Feature:**
**Related Issue / Ticket:**

---

### Automated CI Gates (must be green before merge)

- [ ] **Lint** — `ruff check` passes with no errors
- [ ] **Format** — `ruff format --check` passes
- [ ] **Type check** — `mypy .` passes (strict mode)
- [ ] **Unit tests** — pytest passes with ≥80% coverage on changed packages
- [ ] **Security SAST** — bandit reports zero findings (no `# nosec` without justification)
- [ ] **Dependency scan** — pip-audit reports no vulnerabilities
- [ ] **IaC security** — checkov reports no HIGH/CRITICAL findings on Terraform changes
- [ ] **Terraform validate** — all environments validate cleanly

---

### Security Checklist (reviewer must verify)

- [ ] No credentials, API keys, tokens, or secrets in code or configuration files
- [ ] No hardcoded field lists, table names, or source-specific logic outside adapters
- [ ] All new IAM policies are resource-scoped — no wildcard `*` on actions or resources
- [ ] Secrets sourced from AWS Secrets Manager only — no `os.environ` for credentials
- [ ] New S3 buckets have versioning, SSE-KMS, public access block, and TLS-only bucket policy
- [ ] New DynamoDB tables have SSE with KMS and point-in-time recovery enabled
- [ ] All log output passes through `scrub_sensitive_values()` before emission
- [ ] `StructuredLogEvent` used for all structured log emissions (not raw print/logging calls)

---

### Architecture Alignment

- [ ] Changes conform to the naming standards in the specification (no `helper`, `util`, `manager`, `phase1`)
- [ ] New connector adapter implements `ConnectorInterface` fully (no partial implementations)
- [ ] Raw layer writes are append-only (no updates or deletes to existing raw files)
- [ ] Watermark is not advanced in this PR (watermark advancement belongs to orchestration only)
- [ ] Schema drift detection is not bypassed

---

### Testing

- [ ] Unit tests cover the happy path and at least two failure scenarios
- [ ] Security test included: credential scrubbing verified where applicable
- [ ] Integration test added or updated if this changes external system interaction
- [ ] Test names describe behaviour under test (e.g. `test_watermark_not_advanced_on_failed_run`)

---

### Operational Readiness

- [ ] Runbook updated if this introduces a new failure mode
- [ ] CloudWatch alarm updated if this changes an observable metric
- [ ] Configuration contract updated if new entity configuration keys are introduced
- [ ] Documentation updated in `docs/` if public interfaces changed

---

### Reviewer Notes

<!-- Anything specific you want the reviewer to focus on? -->
