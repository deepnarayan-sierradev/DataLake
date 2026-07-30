"""Export, deletion, PHI gate, subprocessor register and transition package tests (DL-10)."""

from __future__ import annotations

from datetime import date

import boto3
import pytest
from moto import mock_aws

from conftest import RESOURCE_NAME_ENVIRONMENT
from portability.deletion_workflow import (
    DeletionNotAuthorisedError,
    DeletionRequest,
    DeletionSaga,
    DeletionStore,
    IncompleteDeletionError,
    LegalHoldActiveError,
    StepOutcome,
    s3_prefix_deleter,
)
from portability.export_service import (
    EXPORT_CAPABILITY,
    ExportCapabilityRequiredError,
    ExportFormat,
    ExportFormatStrategy,
    ExportJobRepository,
    ExportJobStatus,
    ExportLayer,
    ExportService,
    export_artefact_key,
)
from portability.phi_gate import (
    KNOWN_PHI_SOURCES,
    PLATFORM_SUBPROCESSORS,
    PhiClassification,
    PhiGateBlockedError,
    PhiOnboardingGate,
    PhiOnboardingState,
    SubprocessorRegister,
    enforce_phi_gate,
    evaluate_phi_gate,
    platform_purpose_tags,
)
from portability.transition_package import (
    REPRODUCTION_CRITICAL_COMPONENTS,
    REQUIRED_COMPONENTS,
    IncompletePackageError,
    PackageComponent,
    PackagedArtefact,
    ReproductionTestFailedError,
    TransitionPackage,
    enforce_reproducibility,
    render_infrastructure_handover,
    require_complete_package,
    source_integration_inventory,
    verify_reproducibility,
)
from tenancy.scope_contract import PartitionKind, PartitionModel, ScopeUnit, TenantPartitionProfile
from tenancy.scope_predicate import ConsumptionSurface, build_scope_claims, scope_predicate

_REGION = "us-east-1"
_CAPABILITIES = frozenset({EXPORT_CAPABILITY})


def _table(name: str, pk: str, sk: str | None = None) -> None:
    key_schema = [{"AttributeName": pk, "KeyType": "HASH"}]
    attributes = [{"AttributeName": pk, "AttributeType": "S"}]
    if sk:
        key_schema.append({"AttributeName": sk, "KeyType": "RANGE"})
        attributes.append({"AttributeName": sk, "AttributeType": "S"})
    boto3.client("dynamodb", region_name=_REGION).create_table(
        TableName=name,
        KeySchema=key_schema,
        AttributeDefinitions=attributes,
        BillingMode="PAY_PER_REQUEST",
    )


class TestFormatStrategies:
    _ROWS = [
        {"id": "1", "name": "Acme", "scope_unit_id": "franchisee-0001"},
        {"id": "2", "name": "Beta", "scope_unit_id": "franchisee-0002"},
    ]

    def test_csv_has_a_header_and_one_row_per_record(self):
        payload = b"".join(ExportFormatStrategy.to_csv(self._ROWS)).decode()
        lines = [line for line in payload.splitlines() if line]
        assert lines[0].startswith("id,name,scope_unit_id")
        assert len(lines) == 3

    def test_json_lines_are_streamable(self):
        payload = b"".join(ExportFormatStrategy.to_json_lines(self._ROWS)).decode()
        assert payload.count("\n") == 2
        assert not payload.startswith("[")

    def test_empty_input_produces_no_output(self):
        assert b"".join(ExportFormatStrategy.to_csv([])) == b""
        assert b"".join(ExportFormatStrategy.to_json_lines([])) == b""

    def test_artefact_key_is_tenant_prefixed(self):
        key = export_artefact_key("evive", "exp-1", "company", ExportFormat.CSV)
        assert key == "evive/exports/exp-1/company.csv"


_EXPORT_PREDICATE = scope_predicate(
    build_scope_claims("evive", TenantPartitionProfile(tenant_code="evive")),
    surface=ConsumptionSurface.EXPORT,
)


@mock_aws
class TestExportService:
    def _service(self) -> ExportService:
        boto3.client("s3", region_name=_REGION).create_bucket(Bucket="export-bucket")
        _table(RESOURCE_NAME_ENVIRONMENT["EXPORT_JOB_TABLE"], "tenant_code", "job_id")
        return ExportService(
            environment="dev",
            region_name=_REGION,
            artefact_bucket="export-bucket",
            repository=ExportJobRepository(environment="dev", region_name=_REGION),
        )

    def _request(self, service: ExportService, **overrides):
        base = {
            "tenant_code": "evive",
            "layer": ExportLayer.ANALYTICS,
            "export_format": ExportFormat.CSV,
            "entity_id": "company",
            "requested_by": "ops@example.test",
            "granted_capabilities": _CAPABILITIES,
            "scope_predicate": _EXPORT_PREDICATE,
        }
        return service.request_export(**{**base, **overrides})

    def test_export_requires_the_export_capability(self):
        service = self._service()
        with pytest.raises(ExportCapabilityRequiredError, match="distinct from"):
            self._request(service, granted_capabilities=frozenset({"datalake:read"}))

    def test_request_persists_a_job(self):
        service = self._service()
        job = self._request(service)
        assert job.status is ExportJobStatus.REQUESTED
        assert job.job_id.startswith("exp-")

    def test_execute_uploads_the_artefact(self):
        service = self._service()
        job = self._request(service)
        completed = service.execute(
            job,
            [{"id": "1", "scope_unit_id": "franchisee-0001"}],
            scope_predicate=_EXPORT_PREDICATE,
        )
        assert completed.status is ExportJobStatus.COMPLETED
        assert completed.row_count == 1
        assert completed.artefact_bytes > 0
        body = (
            boto3.client("s3", region_name=_REGION)
            .get_object(Bucket="export-bucket", Key=str(completed.artefact_s3_key))["Body"]
            .read()
            .decode()
        )
        assert "franchisee-0001" in body

    def test_row_level_security_applies_to_an_export(self):
        service = self._service()
        profile = TenantPartitionProfile(
            tenant_code="evive",
            partition_model=PartitionModel.PARTITIONED,
            partition_kind=PartitionKind.FRANCHISE,
        )
        units = [
            ScopeUnit(
                tenant_code="evive",
                scope_unit_id=f"franchisee-{i:04d}",
                partition_kind=PartitionKind.FRANCHISE,
                display_name=f"F{i}",
            )
            for i in (1, 2)
        ]
        claims = build_scope_claims(
            "evive",
            profile,
            granted_scope_unit_ids=frozenset({"franchisee-0001"}),
            units=units,
        )
        predicate = scope_predicate(claims, surface=ConsumptionSurface.EXPORT)
        job = self._request(service, scope_predicate=predicate)
        completed = service.execute(
            job,
            [
                {"id": "1", "scope_unit_id": "franchisee-0001"},
                {"id": "2", "scope_unit_id": "franchisee-0002"},
            ],
            scope_predicate=predicate,
        )
        assert completed.row_count == 1

    def test_a_predicate_built_for_the_wrong_surface_is_rejected(self):
        service = self._service()
        claims = build_scope_claims("demo", TenantPartitionProfile(tenant_code="demo"))
        predicate = scope_predicate(claims, surface=ConsumptionSurface.SEMANTIC_QUERY)
        with pytest.raises(ValueError, match="EXPORT surface"):
            self._request(service, scope_predicate=predicate)

    def test_json_and_parquet_formats_execute(self):
        service = self._service()
        for export_format in (ExportFormat.JSON, ExportFormat.PARQUET):
            job = self._request(service, export_format=export_format)
            completed = service.execute(job, [{"id": "1"}], scope_predicate=_EXPORT_PREDICATE)
            assert completed.status is ExportJobStatus.COMPLETED

    def test_signed_url_requires_a_completed_job(self):
        service = self._service()
        job = self._request(service)
        with pytest.raises(ValueError, match="only a completed job"):
            service.signed_download_url(job)

    def test_signed_url_is_time_limited(self):
        service = self._service()
        job = service.execute(
            self._request(service), [{"id": "1"}], scope_predicate=_EXPORT_PREDICATE
        )
        url = service.signed_download_url(job)
        assert "X-Amz-Expires" in url or "Expires=" in url

    def test_failure_records_the_error_on_the_job(self):
        service = self._service()
        job = self._request(service, delivery_bucket="does-not-exist")
        with pytest.raises(Exception):
            service.execute(job, [{"id": "1"}], scope_predicate=_EXPORT_PREDICATE)
        stored = ExportJobRepository(environment="dev", region_name=_REGION).get(
            "evive", job.job_id
        )
        assert stored is not None
        assert stored["status"] == ExportJobStatus.FAILED.value

    def test_job_listing(self):
        service = self._service()
        self._request(service)
        self._request(service, entity_id="ar-invoice")
        repository = ExportJobRepository(environment="dev", region_name=_REGION)
        assert len(repository.list_jobs("evive")) == 2


class TestDeletionAuthorisation:
    def _request(self, **overrides) -> DeletionRequest:
        base = {
            "tenant_code": "evive",
            "requested_by": "ops@example.test",
            "approved_by": "ciso@example.test",
            "typed_confirmation": "DELETE ALL DATA FOR evive",
        }
        return DeletionRequest(**{**base, **overrides})

    def test_valid_request(self):
        assert self._request().tenant_code == "evive"

    def test_self_approval_is_refused(self):
        with pytest.raises(DeletionNotAuthorisedError, match="distinct from the requester"):
            self._request(approved_by="ops@example.test")

    def test_missing_approver_is_refused(self):
        with pytest.raises(DeletionNotAuthorisedError):
            self._request(approved_by="")

    def test_wrong_typed_confirmation_is_refused(self):
        with pytest.raises(DeletionNotAuthorisedError, match="Type exactly"):
            self._request(typed_confirmation="yes")


@mock_aws
class TestDeletionSaga:
    def _saga(self, **overrides) -> DeletionSaga:
        _table(
            RESOURCE_NAME_ENVIRONMENT["DELETION_CERTIFICATE_TABLE"], "tenant_code", "certificate_id"
        )
        deleters = {
            store: (lambda tenant_code, store=store: (1, f"{store.value} verified"))
            for store in DeletionStore
        }
        base = {
            "environment": "dev",
            "region_name": _REGION,
            "deleters": deleters,
        }
        return DeletionSaga(**{**base, **overrides})

    def _request(self, **overrides) -> DeletionRequest:
        base = {
            "tenant_code": "evive",
            "requested_by": "ops@example.test",
            "approved_by": "ciso@example.test",
            "typed_confirmation": "DELETE ALL DATA FOR evive",
        }
        return DeletionRequest(**{**base, **overrides})

    def test_complete_deletion_issues_a_certificate(self):
        certificate = self._saga().execute(self._request())
        assert certificate.is_complete is True
        assert certificate.total_objects_deleted == len(DeletionStore)
        assert "# Data deletion certificate" in certificate.render_markdown()

    def test_a_store_with_no_deleter_blocks_the_certificate(self):
        _table(
            RESOURCE_NAME_ENVIRONMENT["DELETION_CERTIFICATE_TABLE"], "tenant_code", "certificate_id"
        )
        saga = DeletionSaga(
            environment="dev",
            region_name=_REGION,
            deleters={DeletionStore.S3_RAW: lambda tenant_code: (1, "ok")},
        )
        with pytest.raises(IncompleteDeletionError, match="partial certificate is worse"):
            saga.execute(self._request())

    def test_unacknowledged_hold_raises(self):
        saga = self._saga(held_stores={DeletionStore.S3_RAW: "litigation hold 2026-04"})
        with pytest.raises(LegalHoldActiveError, match="Acknowledge each held store"):
            saga.execute(self._request())

    def test_acknowledged_hold_is_retained_and_named_on_the_certificate(self):
        saga = self._saga(held_stores={DeletionStore.S3_RAW: "litigation hold 2026-04"})
        certificate = saga.execute(self._request(acknowledged_holds=(DeletionStore.S3_RAW.value,)))
        assert certificate.is_complete is True
        retained = certificate.retained_items
        assert len(retained) == 1
        assert retained[0].outcome is StepOutcome.RETAINED_UNDER_HOLD
        assert "litigation hold 2026-04" in certificate.render_markdown()

    def test_legal_obligation_retention_is_distinct_from_a_hold(self):
        saga = self._saga(legally_retained={DeletionStore.CLOUDWATCH_LOGS: "SOC 2 evidence"})
        certificate = saga.execute(self._request())
        outcomes = {s.store: s.outcome for s in certificate.steps}
        assert outcomes[DeletionStore.CLOUDWATCH_LOGS] is (StepOutcome.RETAINED_LEGAL_OBLIGATION)

    def test_a_failing_deleter_blocks_the_certificate(self):
        def boom(tenant_code):
            raise RuntimeError("bucket policy denied")

        deleters = {store: (lambda t: (1, "ok")) for store in DeletionStore}
        deleters[DeletionStore.S3_CURATED] = boom
        _table(
            RESOURCE_NAME_ENVIRONMENT["DELETION_CERTIFICATE_TABLE"], "tenant_code", "certificate_id"
        )
        saga = DeletionSaga(environment="dev", region_name=_REGION, deleters=deleters)
        with pytest.raises(IncompleteDeletionError):
            saga.execute(self._request())

    def test_certificates_are_listable_including_the_failed_attempt(self):
        _table(
            RESOURCE_NAME_ENVIRONMENT["DELETION_CERTIFICATE_TABLE"], "tenant_code", "certificate_id"
        )
        saga = DeletionSaga(
            environment="dev",
            region_name=_REGION,
            deleters={DeletionStore.S3_RAW: lambda t: (1, "ok")},
        )
        with pytest.raises(IncompleteDeletionError):
            saga.execute(self._request())
        assert len(saga.list_certificates("evive")) == 1

    def test_s3_prefix_deleter_verifies_by_relisting(self):
        s3 = boto3.client("s3", region_name=_REGION)
        s3.create_bucket(Bucket="raw-bucket")
        s3.put_object(Bucket="raw-bucket", Key="evive/hubspot/a.parquet", Body=b"x")
        s3.put_object(Bucket="raw-bucket", Key="other/hubspot/b.parquet", Body=b"y")
        deleter = s3_prefix_deleter(s3, "raw-bucket", DeletionStore.S3_RAW)
        deleted, verification = deleter("evive")
        assert deleted == 1
        assert "verified empty" in verification
        assert s3.list_objects_v2(Bucket="raw-bucket", Prefix="other/")["KeyCount"] == 1


class TestPhiGate:
    def test_a_non_phi_source_onboards(self):
        state = PhiOnboardingState(source_id="hubspot", classification=PhiClassification.NOT_PHI)
        assert evaluate_phi_gate(state).permitted is True

    def test_an_unclassified_source_is_treated_as_phi(self):
        verdict = evaluate_phi_gate(PhiOnboardingState(source_id="mystery-source"))
        assert verdict.permitted is False
        assert any("unclassified" in reason for reason in verdict.reasons)

    def test_phi_source_without_a_baa_is_refused(self):
        state = PhiOnboardingState(
            source_id="wellsky",
            classification=PhiClassification.PHI_BEARING,
            environment_hipaa_capable=True,
        )
        with pytest.raises(PhiGateBlockedError, match="no executed BAA"):
            enforce_phi_gate(state)

    def test_phi_source_in_a_non_hipaa_environment_is_refused(self):
        state = PhiOnboardingState(
            source_id="wellsky",
            classification=PhiClassification.PHI_BEARING,
            baa_executed=True,
            baa_executed_at=date(2026, 6, 1),
            baa_counterparty="WellSky Corporation",
            environment_hipaa_capable=False,
        )
        with pytest.raises(PhiGateBlockedError, match="not confirmed HIPAA-capable"):
            enforce_phi_gate(state)

    def test_fully_satisfied_preconditions_permit_onboarding(self):
        state = PhiOnboardingState(
            source_id="wellsky",
            classification=PhiClassification.PHI_BEARING,
            baa_executed=True,
            baa_executed_at=date(2026, 6, 1),
            baa_counterparty="WellSky Corporation",
            environment_hipaa_capable=True,
        )
        assert state.preconditions_met is True
        assert enforce_phi_gate(state).permitted is True

    def test_a_baa_with_no_counterparty_is_refused(self):
        state = PhiOnboardingState(
            source_id="wellsky",
            classification=PhiClassification.PHI_BEARING,
            baa_executed=True,
            baa_executed_at=date(2026, 6, 1),
            environment_hipaa_capable=True,
        )
        assert evaluate_phi_gate(state).permitted is False

    def test_the_customer_phi_sources_are_declared(self):
        assert KNOWN_PHI_SOURCES == frozenset({"wellsky", "seniorplace"})


@mock_aws
class TestPhiOnboardingGate:
    def _gate(self, hipaa_capable: bool = False) -> PhiOnboardingGate:
        _table(RESOURCE_NAME_ENVIRONMENT["SOURCE_ONBOARDING_TABLE"], "source_id")
        return PhiOnboardingGate(
            environment="dev", region_name=_REGION, hipaa_capable=hipaa_capable
        )

    def test_known_phi_source_defaults_to_phi_bearing(self):
        gate = self._gate()
        assert gate.state_for("wellsky").classification is PhiClassification.PHI_BEARING

    def test_unknown_source_defaults_to_unclassified(self):
        gate = self._gate()
        assert gate.state_for("mystery").classification is PhiClassification.UNCLASSIFIED

    def test_classification_is_recorded_with_its_actor(self):
        gate = self._gate()
        gate.classify("hubspot", PhiClassification.NOT_PHI, classified_by="dpo@example.test")
        assert gate.state_for("hubspot").classification is PhiClassification.NOT_PHI

    def test_classification_requires_an_actor(self):
        gate = self._gate()
        with pytest.raises(ValueError, match="must name the person"):
            gate.classify("hubspot", PhiClassification.NOT_PHI, classified_by="")

    def test_onboarding_wellsky_is_blocked_until_a_baa_is_recorded(self):
        gate = self._gate(hipaa_capable=True)
        with pytest.raises(PhiGateBlockedError):
            gate.guard_onboarding("wellsky")
        gate.record_baa(
            "wellsky",
            counterparty="WellSky Corporation",
            executed_on=date(2026, 6, 1),
            recorded_by="counsel@example.test",
        )
        assert gate.guard_onboarding("wellsky").permitted is True

    def test_recording_a_baa_requires_a_counterparty(self):
        gate = self._gate()
        with pytest.raises(ValueError, match="counterparty"):
            gate.record_baa(
                "wellsky", counterparty="", executed_on=date(2026, 6, 1), recorded_by="a"
            )

    def test_non_phi_source_onboards_without_a_baa(self):
        gate = self._gate()
        gate.classify("hubspot", PhiClassification.NOT_PHI, classified_by="dpo@example.test")
        assert gate.guard_onboarding("hubspot").permitted is True


@mock_aws
class TestSubprocessorRegister:
    def test_register_publishes_and_lists(self):
        _table(
            RESOURCE_NAME_ENVIRONMENT["SUBPROCESSOR_TABLE"], "register_scope", "subprocessor_name"
        )
        register = SubprocessorRegister(environment="dev", region_name=_REGION)
        assert register.publish() == len(PLATFORM_SUBPROCESSORS)
        assert len(register.list_register()) == len(PLATFORM_SUBPROCESSORS)

    def test_markdown_names_every_subprocessor(self):
        _table(
            RESOURCE_NAME_ENVIRONMENT["SUBPROCESSOR_TABLE"], "register_scope", "subprocessor_name"
        )
        rendered = SubprocessorRegister(environment="dev", region_name=_REGION).render_markdown()
        for subprocessor in PLATFORM_SUBPROCESSORS:
            assert subprocessor.name in rendered

    def test_no_llm_provider_is_listed_while_dl04_is_deferred(self):
        assert not any(s.category.value == "llm_provider" for s in PLATFORM_SUBPROCESSORS)

    def test_every_subprocessor_states_a_purpose(self):
        assert all(s.purpose for s in PLATFORM_SUBPROCESSORS)

    def test_purpose_tags_cover_the_runtime_roles(self):
        tags = platform_purpose_tags("dev")
        assert len(tags) >= 5
        extraction = next(t for t in tags if "extraction" in t.role_name)
        assert extraction.permits("raw") is True
        assert extraction.permits("analytics") is False


class TestTransitionPackage:
    def _package(self, components=None, entity_id: str = "company") -> TransitionPackage:
        package = TransitionPackage(tenant_code="evive", export_format=ExportFormat.PARQUET)
        for component in components or REQUIRED_COMPONENTS:
            package.add(
                PackagedArtefact(
                    component=component,
                    relative_path=f"{component.value}/{entity_id}.out",
                    content_bytes=128,
                    content_hash="a" * 64,
                    entity_id=entity_id,
                )
            )
        return package

    def test_complete_package_passes(self):
        package = self._package()
        assert package.missing_components == frozenset()
        assert require_complete_package(package) is package

    def test_incomplete_package_is_refused(self):
        package = self._package(components=[PackageComponent.DATASETS])
        with pytest.raises(IncompletePackageError, match="not a capability"):
            require_complete_package(package)

    def test_manifest_lists_missing_components(self):
        manifest = self._package(components=[PackageComponent.DATASETS]).render_manifest()
        assert "## Missing components" in manifest
        assert "semantic_model" in manifest

    def test_manifest_totals(self):
        package = self._package()
        assert package.total_bytes == 128 * len(REQUIRED_COMPONENTS)
        assert package.entities_covered() == frozenset({"company"})

    def test_reproduction_test_passes_with_critical_components(self):
        package = self._package(components=REPRODUCTION_CRITICAL_COMPONENTS)
        result = verify_reproducibility(package, "company")
        assert result.reproducible is True

    def test_reproduction_test_fails_without_the_mapping(self):
        components = REPRODUCTION_CRITICAL_COMPONENTS - {PackageComponent.FIELD_MAPPINGS}
        package = self._package(components=components)
        with pytest.raises(ReproductionTestFailedError, match="field_mappings"):
            enforce_reproducibility(package, "company")

    def test_reproduction_test_is_per_entity(self):
        package = self._package(components=REPRODUCTION_CRITICAL_COMPONENTS, entity_id="company")
        assert verify_reproducibility(package, "ar-invoice").reproducible is False

    def test_json_manifest_round_trips(self):
        payload = self._package().to_json()
        assert '"tenant_code": "evive"' in payload
        assert '"missing_components": []' in payload


class TestInfrastructureHandover:
    def test_handover_names_the_prevent_destroy_resources(self):
        rendered = render_infrastructure_handover(
            "evive",
            account_id="087972550871",
            region="us-east-1",
            terraform_state_bucket="datalake-terraform-state-dev-use1",
            terraform_lock_table="datalake-terraform-state-lock-dev",
        )
        assert RESOURCE_NAME_ENVIRONMENT["ENTITY_CONFIG_TABLE"] in rendered
        assert "zero-diff" in rendered
        assert "Rotate every credential" in rendered

    def test_handover_validates_the_tenant_code(self):
        with pytest.raises(ValueError, match="tenant code format"):
            render_infrastructure_handover(
                "Bad_Tenant",
                account_id="1",
                region="us-east-1",
                terraform_state_bucket="b",
                terraform_lock_table="t",
            )

    def test_source_inventory_is_generated_from_the_registry(self):
        import connector_runtime.adapters.hubspot.hubspot_connector  # noqa: F401
        from connector_runtime.source_capabilities import source_capability_registry

        rendered = source_integration_inventory(source_capability_registry.all_declarations())
        assert "hubspot" in rendered
        assert "| Source | Capabilities |" in rendered


@mock_aws
class TestDynamoDbSweepActuallyDeletes:
    """
    The sweep reported success while the tenant's audit rows survived (2026-07-29).

    `datalake-run-audit-log-<env>` is keyed on `run_id`, so the deleter's
    `begins_with(run_id, "tenant#")` filter
    matched nothing — and returning 0 with no error meant the saga counted the step complete and
    issued the certificate. A certificate is a compliance artefact given to a customer; that one
    would have asserted deletion of rows still present.

    These read the table after deleting, which is the only assertion that could have caught it.
    """

    def _audit_table(self) -> object:
        dynamodb = boto3.resource("dynamodb", region_name=_REGION)
        dynamodb.create_table(
            TableName=RESOURCE_NAME_ENVIRONMENT["AUDIT_LOG_TABLE"],
            KeySchema=[
                {"AttributeName": "run_id", "KeyType": "HASH"},
                {"AttributeName": "stage", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "run_id", "AttributeType": "S"},
                {"AttributeName": "stage", "AttributeType": "S"},
                {"AttributeName": "tenant_code", "AttributeType": "S"},
                {"AttributeName": "started_at", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "tenant-started-index",
                    "KeySchema": [
                        {"AttributeName": "tenant_code", "KeyType": "HASH"},
                        {"AttributeName": "started_at", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                }
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        table = dynamodb.Table(RESOURCE_NAME_ENVIRONMENT["AUDIT_LOG_TABLE"])
        for index in range(4):
            table.put_item(
                Item={
                    "run_id": f"run-{index}",
                    "stage": "extraction",
                    "tenant_code": "evive",
                    "started_at": f"2026-07-29T0{index}:00:00Z",
                }
            )
        table.put_item(
            Item={
                "run_id": "run-other",
                "stage": "extraction",
                "tenant_code": "acme-corp",
                "started_at": "2026-07-29T09:00:00Z",
            }
        )
        return table

    def test_a_run_id_keyed_table_is_actually_swept(self) -> None:
        from portability.deletion_workflow import dynamodb_tenant_item_deleter

        table = self._audit_table()
        dynamodb = boto3.resource("dynamodb", region_name=_REGION)
        deleted, detail = dynamodb_tenant_item_deleter(
            dynamodb, (RESOURCE_NAME_ENVIRONMENT["AUDIT_LOG_TABLE"],)
        )("evive")

        assert deleted == 4
        assert "verified" in detail
        assert table.scan()["Count"] == 1  # only the other tenant's row

    def test_another_tenants_rows_are_never_touched(self) -> None:
        from portability.deletion_workflow import dynamodb_tenant_item_deleter

        table = self._audit_table()
        dynamodb = boto3.resource("dynamodb", region_name=_REGION)
        dynamodb_tenant_item_deleter(dynamodb, (RESOURCE_NAME_ENVIRONMENT["AUDIT_LOG_TABLE"],))(
            "evive"
        )
        survivors = {item["tenant_code"] for item in table.scan()["Items"]}
        assert survivors == {"acme-corp"}

    def test_an_unrecognised_key_shape_raises_rather_than_reporting_zero(self) -> None:
        """A table the sweep cannot address must block the certificate, not pass silently."""
        from portability.deletion_workflow import (
            IncompleteDeletionError,
            dynamodb_tenant_item_deleter,
        )

        dynamodb = boto3.resource("dynamodb", region_name=_REGION)
        dynamodb.create_table(
            TableName="DatalakeMysteryTable",
            KeySchema=[{"AttributeName": "widget_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "widget_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        with pytest.raises(IncompleteDeletionError, match="not a recognised tenant shape"):
            dynamodb_tenant_item_deleter(dynamodb, ("DatalakeMysteryTable",))("evive")
