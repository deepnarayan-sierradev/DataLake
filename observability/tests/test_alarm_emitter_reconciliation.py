"""
Alarm↔emitter reconciliation guard (FR-F0.6 / OBS-01).

Fails if any CloudWatch alarm in the platform metric namespace watches a metric
that no `CloudWatchMetricsEmitter` method emits — a "dead alarm" that can never
fire. This is the CI check that keeps the two sides in sync as either changes.
"""

from __future__ import annotations

import re
from pathlib import Path

from observability.metrics_emitter import PLATFORM_METRIC_NAMESPACE, CloudWatchMetricsEmitter

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ALARMS_TF = _REPO_ROOT / "infrastructure" / "modules" / "observability" / "main.tf"
_EMITTER_SRC = _REPO_ROOT / "observability" / "metrics_emitter.py"


def _platform_alarm_metrics() -> set[str]:
    text = _ALARMS_TF.read_text()
    metrics: set[str] = set()
    for block in re.split(r'resource\s+"aws_cloudwatch_metric_alarm"', text)[1:]:
        metric = re.search(r'metric_name\s*=\s*"([^"]+)"', block)
        namespace = re.search(r'namespace\s*=\s*"([^"]+)"', block)
        if metric and namespace and namespace.group(1) == PLATFORM_METRIC_NAMESPACE:
            metrics.add(metric.group(1))
    return metrics


def _emitter_metrics() -> set[str]:
    return set(re.findall(r'metric_name="([^"]+)"', _EMITTER_SRC.read_text()))


class TestAlarmEmitterReconciliation:
    def test_no_dead_alarms(self):
        dead = _platform_alarm_metrics() - _emitter_metrics()
        assert not dead, (
            f"Dead alarms watch metrics no emitter emits: {sorted(dead)}. "
            "Add an emit_* method to CloudWatchMetricsEmitter or remove the alarm."
        )

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
