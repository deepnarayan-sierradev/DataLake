"""
Alarm↔emitter reconciliation guard (FR-F0.6 / OBS-01, DL-OPS-05).

Reconciles the Terraform alarm definitions against `contracts/platform_metrics.py` in **both**
directions, which is what the requirement means by "a metric without an alarm or an alarm
without an emitter fails CI":

  - a platform-namespace alarm watching a metric nothing emits is a dead alarm;
  - a catalogued metric with no alarm is an unwatched signal;
  - a catalogued metric no production code emits is a promise with no producer.

The third check is the one that catches the failure mode this programme could most easily
introduce: adding a metric name to the catalogue and an alarm to Terraform, and then never
wiring the emit call.
"""

from __future__ import annotations

import re
from pathlib import Path

from contracts.platform_metrics import ALL_PLATFORM_METRIC_NAMES, PlatformMetric, metric_unit
from observability.metrics_emitter import PLATFORM_METRIC_NAMESPACE, CloudWatchMetricsEmitter

_REPO_ROOT = Path(__file__).resolve().parents[2]
_OBSERVABILITY_TF = _REPO_ROOT / "infrastructure" / "modules" / "observability"
_ALARMS_TF = _OBSERVABILITY_TF / "main.tf"
_PLATFORM_ALARMS_TF = _OBSERVABILITY_TF / "platform_metric_alarms.tf"
_EMITTER_SRC = _REPO_ROOT / "observability" / "metrics_emitter.py"

# Production packages searched for emit calls. Tests are excluded deliberately: a metric
# emitted only from a test has no producer in the running system.
_PRODUCTION_PACKAGES = (
    "connector_runtime",
    "transformation",
    "entity_resolution",
    "analytics_publisher",
    "orchestration",
    "observability",
    "serving_store",
    "knowledge",
    "semantic",
    "governance",
    "watermark_management",
    "schema_management",
    "tenancy",
    "config_propagation",
    "data_quality",
    "workflow_automation",
    "portability",
)

# Metrics produced by AWS or by infrastructure rather than by application code. Each entry is
# a deliberate exemption from the "must be emitted from Python" check, with the producer named.
_INFRASTRUCTURE_PRODUCED: dict[str, str] = {
    # CloudWatch metric filter over CloudTrail (iam/tenant_boundary.tf).
    "CrossTenantAccessAttempts": "CloudTrail metric filter",
    # AWS/WAFV2 BlockedRequests, republished by the WAF module's own alarm.
    "WafBlockedRequests": "AWS WAF",
    # AWS/ClientVPN and AWS/CertificateManager.
    "VpnClientConnections": "AWS Client VPN",
    "VpnCertificateDaysToExpiry": "AWS Certificate Manager",
    # AWS/SQS queue depth, alarmed directly on the queue dimension.
    "DlqDepth": "AWS SQS queue depth",
    "WorkflowDlqDepth": "AWS SQS queue depth",
    # LambdaInsights extension.
    "LambdaMemoryUtilization": "Lambda Insights extension",
    # Emitted by the deploy pipeline and the post-deploy smoke suite, not by the runtime.
    "DeploymentDurationMs": "deployment pipeline",
    "PostDeploySmokeFailures": "post-deploy smoke suite",
    # Derived from the CloudWatch metric stream by the cost-attribution job (DL-OPS-13).
    "CostPerTenantUsd": "cost attribution job",
    # Serving-engine metrics read from the database's own CloudWatch namespace.
    "ServingQueryLatencyMs": "RDS/Redshift performance insights",
    "ServingConcurrentConnections": "RDS/Redshift performance insights",
}


def _individual_alarm_metrics() -> set[str]:
    """Metrics named by hand-written `aws_cloudwatch_metric_alarm` blocks."""
    text = _ALARMS_TF.read_text()
    metrics: set[str] = set()
    for block in re.split(r'resource\s+"aws_cloudwatch_metric_alarm"', text)[1:]:
        metric = re.search(r'metric_name\s*=\s*"([^"]+)"', block)
        namespace = re.search(r'namespace\s*=\s*"([^"]+)"', block)
        if metric and namespace and namespace.group(1) == PLATFORM_METRIC_NAMESPACE:
            metrics.add(metric.group(1))
    return metrics


def _map_driven_alarm_metrics() -> set[str]:
    """Metrics named as keys of the `platform_metric_alarms` map."""
    text = _PLATFORM_ALARMS_TF.read_text()
    start = text.index("platform_metric_alarms = {")
    end = text.index("\n}\n", start)
    body = text[start:end]
    return set(re.findall(r"^\s{4}([A-Za-z][A-Za-z0-9]*)\s*=\s*\{", body, re.MULTILINE))


def _absence_alarm_metrics() -> set[str]:
    """
    Metrics covered by an "is this control inert" alarm (G6).

    An absence alarm is a real alarm — it fires when the metric publishes nothing, which is the
    failure mode a threshold alarm cannot see. Counting it here keeps the reconciliation honest
    without forcing a meaningless threshold on a control-liveness metric.
    """
    text = _PLATFORM_ALARMS_TF.read_text()
    if "absence_alarmed_metrics = {" not in text:
        return set()
    start = text.index("absence_alarmed_metrics = {")
    end = text.index("\n  }\n", start)
    body = text[start:end]
    return set(re.findall(r"^\s{4}([A-Za-z][A-Za-z0-9]*)\s*=\s*\{", body, re.MULTILINE))


def _alarmed_metrics() -> set[str]:
    return _individual_alarm_metrics() | _map_driven_alarm_metrics() | _absence_alarm_metrics()


def _emitter_literal_metrics() -> set[str]:
    """Metrics emitted through a named `emit_*` method with a literal metric name."""
    return set(re.findall(r'metric_name="([^"]+)"', _EMITTER_SRC.read_text()))


def _catalogue_emitting_source() -> str:
    """Concatenated production source, for finding `PlatformMetric.X` references."""
    chunks: list[str] = []
    for package in _PRODUCTION_PACKAGES:
        for path in (_REPO_ROOT / package).rglob("*.py"):
            if "/tests/" in str(path) or path.name.startswith("test_"):
                continue
            chunks.append(path.read_text())
    return "\n".join(chunks)


def _catalogue_referenced_metrics(source: str) -> set[str]:
    referenced = set(re.findall(r"PlatformMetric\.([A-Z0-9_]+)", source))
    return {PlatformMetric[name].value for name in referenced if name in PlatformMetric.__members__}


class TestAlarmEmitterReconciliation:
    def test_no_dead_alarms(self):
        """Every alarmed metric is either an emitter literal or a catalogue entry."""
        emitted = _emitter_literal_metrics() | ALL_PLATFORM_METRIC_NAMES
        dead = _alarmed_metrics() - emitted
        assert not dead, (
            f"Dead alarms watch metrics nothing emits: {sorted(dead)}. "
            "Add the metric to contracts/platform_metrics.py, add an emit_* method to "
            "CloudWatchMetricsEmitter, or remove the alarm."
        )

    def test_every_catalogued_metric_is_alarmed(self):
        """A catalogued metric with no alarm is a signal nobody is watching."""
        unalarmed = ALL_PLATFORM_METRIC_NAMES - _alarmed_metrics()
        assert not unalarmed, (
            f"Catalogued metrics with no alarm: {sorted(unalarmed)}. "
            "Add an entry to infrastructure/modules/observability/platform_metric_alarms.tf."
        )

    def test_every_catalogued_metric_has_a_producer(self):
        """
        A catalogued, alarmed metric nothing emits is a promise with no producer.

        This is the check that catches adding a name and an alarm and then forgetting the emit
        call — the exact shape of the four dead alarms FR-F0.6 was opened for.
        """
        source = _catalogue_emitting_source()
        produced = (
            _catalogue_referenced_metrics(source)
            | _emitter_literal_metrics()
            | set(_INFRASTRUCTURE_PRODUCED)
        )
        orphans = ALL_PLATFORM_METRIC_NAMES - produced
        assert not orphans, (
            f"Catalogued metrics with no producer: {sorted(orphans)}. Either emit them from "
            "production code, or add them to _INFRASTRUCTURE_PRODUCED naming the AWS service "
            "or job that does."
        )

    def test_infrastructure_exemptions_are_all_real_metrics(self):
        """An exemption for a metric that no longer exists hides a stale allowance."""
        stale = set(_INFRASTRUCTURE_PRODUCED) - ALL_PLATFORM_METRIC_NAMES
        assert not stale, f"Exemptions for non-existent metrics: {sorted(stale)}."

    def test_previously_dead_alarms_now_have_emit_calls(self):
        """
        The four FR-F0.6 alarms whose emit calls were never wired at their failure points.

        Named explicitly rather than covered only by the generic check above, so a regression
        reports the specific gap the requirement was opened for.
        """
        source = _catalogue_emitting_source()
        for metric_name, method in (
            ("CircuitBreakerOpened", "emit_circuit_breaker_opened"),
            ("CircuitBreakerDDBFallback", "emit_circuit_breaker_ddb_fallback"),
            ("InputValidationFailures", "emit_input_validation_failures"),
            ("CredentialRetrievalFailures", "emit_credential_retrieval_failures"),
        ):
            assert metric_name in _emitter_literal_metrics(), f"{metric_name} lost its emitter."
            assert method in source, (
                f"{method} is defined but never called from production code — that is exactly "
                "the dead-alarm state FR-F0.6 exists to prevent."
            )


class TestEmitterSurface:
    def test_operational_metrics_buffer(self):
        emitter = CloudWatchMetricsEmitter(region_name="us-east-1")
        emitter.emit_circuit_breaker_opened("salesforce", "account", "dev")
        emitter.emit_circuit_breaker_ddb_fallback("salesforce", "account", "dev")
        emitter.emit_input_validation_failures("salesforce", "account", "dev")
        emitter.emit_credential_retrieval_failures("salesforce", "account", "dev")
        buffered = {item["MetricName"] for item in emitter._pending}
        assert buffered == {
            "CircuitBreakerOpened",
            "CircuitBreakerDDBFallback",
            "InputValidationFailures",
            "CredentialRetrievalFailures",
        }

    def test_catalogued_emit_uses_the_catalogue_unit(self):
        emitter = CloudWatchMetricsEmitter(region_name="us-east-1")
        emitter.set_tenant_context("evive")
        emitter.emit_metric(
            PlatformMetric.SEMANTIC_QUERY_LATENCY_MS,
            123.0,
            environment="dev",
            dimensions={"EntityType": "ar_invoice"},
        )
        buffered = emitter._pending[0]
        assert buffered["MetricName"] == "SemanticQueryLatencyMs"
        assert buffered["Unit"] == metric_unit(PlatformMetric.SEMANTIC_QUERY_LATENCY_MS).value
        names = {dimension["Name"] for dimension in buffered["Dimensions"]}
        assert names == {"TenantCode", "Environment", "EntityType"}

    def test_unpermitted_dimension_is_rejected(self):
        emitter = CloudWatchMetricsEmitter(region_name="us-east-1")
        try:
            emitter.emit_metric(
                PlatformMetric.SEMANTIC_QUERIES_COMPILED, dimensions={"CustomerName": "Acme"}
            )
        except ValueError as exc:
            # Unbounded dimension cardinality is both a cost and a PII risk (OWASP A09).
            assert "not in the permitted dimension set" in str(exc)
        else:  # pragma: no cover — the raise is the assertion
            raise AssertionError("An unpermitted dimension must be rejected.")

    def test_paging_metrics_are_marked_in_terraform(self):
        """The five metrics whose breach must page, not email."""
        text = _PLATFORM_ALARMS_TF.read_text()
        for metric_name in (
            "CrossTenantAccessAttempts",
            "CrossScopeAccessAttempts",
            "ResolutionScopeViolations",
            "ConfigVersionMismatchWithinRun",
            "ReconciliationVariancePct",
        ):
            match = re.search(rf"^\s{{4}}{metric_name}\s*=\s*\{{([^}}]*)\}}", text, re.MULTILINE)
            assert match, f"{metric_name} has no alarm entry."
            assert "paging = true" in match.group(1), (
                f"{metric_name} must page: a non-zero value is either an active attack or a "
                "defect that has already produced incorrect or exposed data."
            )
