"""Tests for SourceOnboardingRegistryClient — Phase 10."""

from __future__ import annotations

import boto3
import pytest
from moto import mock_aws

from governance.source_onboarding_registry import (
    OnboardingGate,
    OnboardingGateStatus,
    OnboardingSourceNotFoundError,
    OnboardingValidationError,
    SourceOnboardingRegistryClient,
)

_REGION = "us-east-1"
_ENV = "dev"


@mock_aws
class TestSourceOnboardingRegistry:
    def setup_method(self, method=None):
        # Create the onboarding DynamoDB table
        boto3.client("dynamodb", region_name=_REGION).create_table(
            TableName=f"{_ENV}-source-onboarding-registry",
            KeySchema=[{"AttributeName": "source_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "source_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        self.registry = SourceOnboardingRegistryClient(_ENV, _REGION)

    def test_register_source_succeeds(self):
        rec = self.registry.register_source("salesforce", "data-eng", "critical", "pii")
        assert rec.source_id == "salesforce"
        assert rec.owner == "data-eng"
        assert rec.sla_tier == "critical"

    def test_duplicate_registration_raises(self):
        self.registry.register_source("salesforce", "data-eng", "critical", "pii")
        from botocore.exceptions import ClientError

        with pytest.raises(ClientError):
            self.registry.register_source("salesforce", "data-eng", "critical", "pii")

    def test_invalid_source_id_raises(self):
        with pytest.raises(OnboardingValidationError, match="stable identifier"):
            self.registry.register_source("INVALID SOURCE", "data-eng", "standard", "internal")

    def test_all_gates_start_as_pending(self):
        self.registry.register_source("netsuite", "data-eng", "standard", "internal")
        state = self.registry.get_state("netsuite")
        for gate in OnboardingGate:
            assert state.gate_statuses[gate.value] == OnboardingGateStatus.PENDING.value

    def test_advance_first_gate_to_passed(self):
        self.registry.register_source("salesforce", "data-eng", "critical", "pii")
        transition = self.registry.advance_gate(
            "salesforce",
            OnboardingGate.SOURCE_REGISTRATION,
            OnboardingGateStatus.PASSED,
            reviewer="alice@example.com",
        )
        assert transition.new_status == OnboardingGateStatus.PASSED
        state = self.registry.get_state("salesforce")
        assert state.gate_statuses[OnboardingGate.SOURCE_REGISTRATION.value] == "passed"

    def test_cannot_pass_gate_when_prior_gate_pending(self):
        self.registry.register_source("salesforce", "data-eng", "critical", "pii")
        # Try to pass CREDENTIAL_REGISTRATION before SOURCE_REGISTRATION
        with pytest.raises(OnboardingValidationError, match="prior gate"):
            self.registry.advance_gate(
                "salesforce",
                OnboardingGate.CREDENTIAL_REGISTRATION,
                OnboardingGateStatus.PASSED,
                reviewer="alice",
            )

    def test_waiver_requires_notes(self):
        self.registry.register_source("salesforce", "data-eng", "critical", "pii")
        with pytest.raises(OnboardingValidationError, match="justification"):
            self.registry.advance_gate(
                "salesforce",
                OnboardingGate.SOURCE_REGISTRATION,
                OnboardingGateStatus.WAIVED,
                reviewer="alice",
                notes="",  # empty notes
            )

    def test_waiver_with_notes_succeeds(self):
        self.registry.register_source("salesforce", "data-eng", "critical", "pii")
        self.registry.advance_gate(
            "salesforce",
            OnboardingGate.SOURCE_REGISTRATION,
            OnboardingGateStatus.WAIVED,
            reviewer="alice",
            notes="Waived for sandbox environment testing",
        )
        state = self.registry.get_state("salesforce")
        assert state.gate_statuses[OnboardingGate.SOURCE_REGISTRATION.value] == "waived"

    def test_activation_not_permitted_with_pending_gates(self):
        self.registry.register_source("salesforce", "data-eng", "critical", "pii")
        assert self.registry.is_source_activation_permitted("salesforce") is False

    def test_activation_permitted_after_all_gates_passed(self):
        self.registry.register_source("salesforce", "data-eng", "critical", "pii")
        from governance.source_onboarding_registry import _GATE_ORDER_LIST

        for gate in _GATE_ORDER_LIST:
            self.registry.advance_gate(
                "salesforce", gate, OnboardingGateStatus.PASSED, reviewer="alice"
            )
        assert self.registry.is_source_activation_permitted("salesforce") is True

    def test_get_state_nonexistent_source_raises(self):
        with pytest.raises(OnboardingSourceNotFoundError):
            self.registry.get_state("nonexistent-source")

    def test_is_activation_permitted_nonexistent_returns_false(self):
        result = self.registry.is_source_activation_permitted("nonexistent-source")
        assert result is False

    def test_failed_gate_status_allowed_without_prior_gates(self):
        self.registry.register_source("salesforce", "data-eng", "critical", "pii")
        # FAILED can be set at any time without prior gates
        self.registry.advance_gate(
            "salesforce",
            OnboardingGate.SECURITY_GOVERNANCE,
            OnboardingGateStatus.FAILED,
            reviewer="security-bot",
            notes="Security scan failed",
        )
        state = self.registry.get_state("salesforce")
        assert state.gate_statuses[OnboardingGate.SECURITY_GOVERNANCE.value] == "failed"
