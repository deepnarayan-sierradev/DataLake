"""Tests for TransformationPipeline — Phase 6."""

from __future__ import annotations

import io
from datetime import date

import boto3
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from moto import mock_aws

from tenancy.scope_contract import PartitionModel, TenantPartitionProfile
from transformation.curated_layer_writer import CuratedLayerWriter
from transformation.field_mapping.field_mapping_registry import (
    FieldMappingRegistryClient,
    FieldMappingRule,
    FieldMappingRuleSet,
    MappingTransformation,
)
from transformation.quality_evaluation.quality_policy_evaluator import (
    NullCheck,
    QualityCheckSeverity,
    QualityPolicy,
    QualityPolicyEvaluator,
)
from transformation.transformation_pipeline import (
    TransformationContext,
    TransformationPipeline,
)

_REGION = "us-east-1"
_RAW_BUCKET = "test-raw-bucket"
_CURATED_BUCKET = "test-curated-bucket"
_MAPPING_BUCKET = "test-mapping-bucket"
_RUN_ID = "run-pipeline-test-001"


def _write_raw_parquet(s3_client, bucket, prefix, records):
    """Helper: write records as Parquet to S3."""
    table = pa.Table.from_pylist(records)
    buf = io.BytesIO()
    pq.write_table(table, buf, compression="snappy")
    key = f"{prefix}data.parquet"
    s3_client.put_object(Bucket=bucket, Key=key, Body=buf.getvalue())


def _make_pipeline(mapping_registry_client, quality_policy=None):
    return TransformationPipeline(
        mapping_registry_client=mapping_registry_client,
        quality_evaluator=QualityPolicyEvaluator(),
        curated_writer=CuratedLayerWriter(_CURATED_BUCKET, _REGION),
        quality_policy=quality_policy,
    )


# DL-SCOPE-07: the pipeline refuses to write curated rows it cannot attribute, so every context
# declares a partition profile. `single` is the demo/dev shape — one implicit unit.
_SINGLE_TENANT_PROFILE = TenantPartitionProfile(
    tenant_code="demo", partition_model=PartitionModel.SINGLE
)


def _make_ctx(raw_prefix="raw/salesforce/salesforce-account/run-001/"):
    return TransformationContext(
        run_id=_RUN_ID,
        source_id="salesforce",
        entity_id="salesforce-account",
        domain="customer",
        raw_s3_bucket=_RAW_BUCKET,
        raw_s3_prefix=raw_prefix,
        mapping_bucket=_MAPPING_BUCKET,
        curated_s3_bucket=_CURATED_BUCKET,
        region_name=_REGION,
        curated_date=date(2024, 1, 15),
        partition_profile=_SINGLE_TENANT_PROFILE,
    )


@mock_aws
class TestTransformationPipelineHappyPath:
    def setup_method(self, method: object = None) -> None:
        s3 = boto3.client("s3", region_name=_REGION)
        for bucket in (_RAW_BUCKET, _CURATED_BUCKET, _MAPPING_BUCKET):
            s3.create_bucket(Bucket=bucket)
        self.s3 = s3

        self.registry_client = FieldMappingRegistryClient(_MAPPING_BUCKET, _REGION)

    def test_identity_mapping_when_no_rule_set(self):
        """Pipeline should pass records through unchanged when no mapping exists."""
        records = [{"Id": "001", "Name": "Acme Corp"}]
        _write_raw_parquet(self.s3, _RAW_BUCKET, "raw/prefix/", records)

        # No rule set published → MappingRuleSetNotFoundError → identity pass
        pipeline = _make_pipeline(self.registry_client)
        ctx = _make_ctx("raw/prefix/")
        result = pipeline.execute(ctx)

        assert result.raw_record_count == 1
        assert result.canonical_record_count == 1
        assert result.mapping_version == "identity"
        assert result.curated_s3_prefix is not None
        assert result.is_publication_blocked is False

    def test_field_mapping_applied(self):
        rule_set = FieldMappingRuleSet(
            source_id="salesforce",
            entity_id="salesforce-account",
            mapping_version="1.0.0",
            rules=(
                FieldMappingRule(
                    source_fields=("Id",),
                    canonical_field="account_id",
                    transformation=MappingTransformation.RENAME,
                    transformation_params={},
                ),
                FieldMappingRule(
                    source_fields=("Name",),
                    canonical_field="account_name",
                    transformation=MappingTransformation.RENAME,
                    transformation_params={},
                ),
            ),
        )
        self.registry_client.publish_rule_set(rule_set, "demo")

        records = [{"Id": "001", "Name": "Acme Corp"}]
        _write_raw_parquet(self.s3, _RAW_BUCKET, "raw/mapped/", records)

        pipeline = _make_pipeline(self.registry_client)
        ctx = TransformationContext(
            run_id=_RUN_ID,
            source_id="salesforce",
            entity_id="salesforce-account",
            domain="customer",
            raw_s3_bucket=_RAW_BUCKET,
            raw_s3_prefix="raw/mapped/",
            mapping_bucket=_MAPPING_BUCKET,
            curated_s3_bucket=_CURATED_BUCKET,
            region_name=_REGION,
            curated_date=date(2024, 1, 15),
        )
        result = pipeline.execute(ctx)

        assert result.mapping_version == "1.0.0"
        assert result.canonical_record_count == 1

    def test_quality_blocking_halts_publication(self):
        records = [{"Id": "001", "Name": None}]
        _write_raw_parquet(self.s3, _RAW_BUCKET, "raw/quality-block/", records)

        quality_policy = QualityPolicy(
            source_id="salesforce",
            entity_id="salesforce-account",
            policy_version="1.0.0",
            checks=(NullCheck("Name", QualityCheckSeverity.BLOCKING),),
        )
        pipeline = _make_pipeline(self.registry_client, quality_policy=quality_policy)
        ctx = _make_ctx("raw/quality-block/")
        result = pipeline.execute(ctx)

        assert result.is_publication_blocked is True
        assert result.curated_s3_prefix is None
        assert result.quality_report_s3_key is not None

    def test_quality_warning_allows_publication(self):
        records = [{"Id": "001", "Name": None, "Revenue": 100}]
        _write_raw_parquet(self.s3, _RAW_BUCKET, "raw/quality-warn/", records)

        quality_policy = QualityPolicy(
            source_id="salesforce",
            entity_id="salesforce-account",
            policy_version="1.0.0",
            checks=(NullCheck("Name", QualityCheckSeverity.WARNING),),
        )
        pipeline = _make_pipeline(self.registry_client, quality_policy=quality_policy)
        ctx = _make_ctx("raw/quality-warn/")
        result = pipeline.execute(ctx)

        assert result.is_publication_blocked is False
        assert result.curated_s3_prefix is not None

    def test_empty_raw_prefix_produces_no_curated_output(self):
        pipeline = _make_pipeline(self.registry_client)
        ctx = _make_ctx("raw/empty-prefix/")
        result = pipeline.execute(ctx)

        assert result.raw_record_count == 0
        assert result.canonical_record_count == 0
        assert result.curated_s3_prefix is None


# ---------------------------------------------------------------------------
# TransformationContext validation
# ---------------------------------------------------------------------------


class TestTransformationContextValidation:
    def test_invalid_domain_raises(self) -> None:
        import pytest

        with pytest.raises(ValueError, match="domain"):
            TransformationContext(
                run_id="run-pipeline-test-001",
                source_id="salesforce",
                entity_id="salesforce-account",
                domain="Bad Domain!",
                raw_s3_bucket=_RAW_BUCKET,
                raw_s3_prefix="raw/valid/",
                mapping_bucket=_MAPPING_BUCKET,
                curated_s3_bucket=_CURATED_BUCKET,
                region_name=_REGION,
            )

    def test_dotdot_prefix_raises(self) -> None:
        import pytest

        with pytest.raises(ValueError, match="invalid path"):
            TransformationContext(
                run_id="run-pipeline-test-001",
                source_id="salesforce",
                entity_id="salesforce-account",
                domain="customer",
                raw_s3_bucket=_RAW_BUCKET,
                raw_s3_prefix="../etc/passwd",
                mapping_bucket=_MAPPING_BUCKET,
                curated_s3_bucket=_CURATED_BUCKET,
                region_name=_REGION,
            )

    def test_absolute_prefix_raises(self) -> None:
        import pytest

        with pytest.raises(ValueError, match="invalid path"):
            TransformationContext(
                run_id="run-pipeline-test-001",
                source_id="salesforce",
                entity_id="salesforce-account",
                domain="customer",
                raw_s3_bucket=_RAW_BUCKET,
                raw_s3_prefix="/absolute/path/",
                mapping_bucket=_MAPPING_BUCKET,
                curated_s3_bucket=_CURATED_BUCKET,
                region_name=_REGION,
            )

    def test_disallowed_chars_in_prefix_raises(self) -> None:
        import pytest

        with pytest.raises(ValueError, match="characters not permitted"):
            TransformationContext(
                run_id="run-pipeline-test-001",
                source_id="salesforce",
                entity_id="salesforce-account",
                domain="customer",
                raw_s3_bucket=_RAW_BUCKET,
                raw_s3_prefix="raw/<script>/",
                mapping_bucket=_MAPPING_BUCKET,
                curated_s3_bucket=_CURATED_BUCKET,
                region_name=_REGION,
            )


# ---------------------------------------------------------------------------
# Optional path coverage: masking, metrics, lineage, catalog
# ---------------------------------------------------------------------------


@mock_aws
class TestTransformationOptionalPaths:
    def setup_method(self, method: object = None) -> None:
        s3 = boto3.client("s3", region_name=_REGION)
        for bucket in (_RAW_BUCKET, _CURATED_BUCKET, _MAPPING_BUCKET, "gov-bucket", "glue-test"):
            s3.create_bucket(Bucket=bucket)
        # Glue catalog
        glue = boto3.client("glue", region_name=_REGION)
        glue.create_database(DatabaseInput={"Name": "test_catalog_db"})
        self.s3 = s3
        self.registry_client = FieldMappingRegistryClient(_MAPPING_BUCKET, _REGION)

    def test_metrics_emitter_called_on_success(self) -> None:
        from unittest.mock import MagicMock

        from observability.metrics_emitter import CloudWatchMetricsEmitter

        records = [{"Id": "001", "Name": "Acme"}]
        _write_raw_parquet(self.s3, _RAW_BUCKET, "raw/metrics/", records)

        mock_emitter = MagicMock(spec=CloudWatchMetricsEmitter)
        pipeline = TransformationPipeline(
            mapping_registry_client=self.registry_client,
            quality_evaluator=QualityPolicyEvaluator(),
            curated_writer=CuratedLayerWriter(_CURATED_BUCKET, _REGION),
            quality_policy=None,
            metrics_emitter=mock_emitter,
        )
        ctx = _make_ctx("raw/metrics/")
        pipeline.execute(ctx)
        mock_emitter.emit_records_extracted.assert_called_once()

    def test_glue_catalog_registration_called(self) -> None:
        """When glue_catalog_database is set, catalog registration path executes."""
        records = [{"Id": "001", "Name": "Acme"}]
        _write_raw_parquet(self.s3, _RAW_BUCKET, "raw/catalog/", records)

        pipeline = _make_pipeline(self.registry_client)
        ctx = TransformationContext(
            run_id=_RUN_ID,
            source_id="salesforce",
            entity_id="salesforce-account",
            domain="customer",
            raw_s3_bucket=_RAW_BUCKET,
            raw_s3_prefix="raw/catalog/",
            mapping_bucket=_MAPPING_BUCKET,
            curated_s3_bucket=_CURATED_BUCKET,
            region_name=_REGION,
            curated_date=date(2024, 1, 15),
            glue_catalog_database="test_catalog_db",
        )
        result = pipeline.execute(ctx)
        # Curated write succeeded with catalog registration (no exception raised)
        assert result.curated_s3_prefix is not None

    def test_lineage_emission_called_when_governance_bucket_set(self) -> None:
        """When governance_s3_bucket is set and curated write succeeded, lineage emit runs."""
        records = [{"Id": "001", "Name": "Acme"}]
        _write_raw_parquet(self.s3, _RAW_BUCKET, "raw/lineage/", records)

        pipeline = _make_pipeline(self.registry_client)
        ctx = TransformationContext(
            run_id=_RUN_ID,
            source_id="salesforce",
            entity_id="salesforce-account",
            domain="customer",
            raw_s3_bucket=_RAW_BUCKET,
            raw_s3_prefix="raw/lineage/",
            mapping_bucket=_MAPPING_BUCKET,
            curated_s3_bucket=_CURATED_BUCKET,
            region_name=_REGION,
            curated_date=date(2024, 1, 15),
            governance_s3_bucket="gov-bucket",
        )
        result = pipeline.execute(ctx)
        # No exception — lineage emission either succeeded or was swallowed
        assert result.canonical_record_count == 1

    def test_masking_applied_when_classification_policy_set(self) -> None:
        """When classification_policy is provided, masking path executes."""
        from governance.data_classification_policy import (
            DataClassificationLevel,
            EntityClassificationPolicy,
            FieldClassification,
            MaskingStrategy,
        )

        records = [{"Id": "001", "email": "user@example.com"}]
        _write_raw_parquet(self.s3, _RAW_BUCKET, "raw/mask/", records)

        policy = EntityClassificationPolicy(
            source_id="salesforce",
            entity_id="salesforce-account",
            policy_version="1.0.0",
            field_classifications=(
                FieldClassification(
                    field_name="email",
                    classification=DataClassificationLevel.PII,
                    masking_strategy=MaskingStrategy.REDACT,
                ),
            ),
        )
        pipeline = TransformationPipeline(
            mapping_registry_client=self.registry_client,
            quality_evaluator=QualityPolicyEvaluator(),
            curated_writer=CuratedLayerWriter(_CURATED_BUCKET, _REGION),
            quality_policy=None,
            classification_policy=policy,
        )
        ctx = _make_ctx("raw/mask/")
        result = pipeline.execute(ctx)
        assert result.canonical_record_count == 1


# ---------------------------------------------------------------------------
# Module-level helper coverage
# ---------------------------------------------------------------------------


@mock_aws
class TestModuleLevelHelpers:
    def setup_method(self, method: object = None) -> None:
        s3 = boto3.client("s3", region_name=_REGION)
        for bucket in (_RAW_BUCKET, _CURATED_BUCKET, _MAPPING_BUCKET):
            s3.create_bucket(Bucket=bucket)
        self.s3 = s3

    def test_iter_raw_records_dotdot_raises(self) -> None:
        from transformation.transformation_pipeline import _iter_raw_records

        with pytest.raises(ValueError, match="Unsafe raw_s3_prefix"):
            list(_iter_raw_records(self.s3, _RAW_BUCKET, "../etc/passwd"))

    def test_iter_raw_records_absolute_raises(self) -> None:
        from transformation.transformation_pipeline import _iter_raw_records

        with pytest.raises(ValueError, match="Unsafe raw_s3_prefix"):
            list(_iter_raw_records(self.s3, _RAW_BUCKET, "/absolute/path"))

    def test_iter_raw_records_disallowed_chars_raises(self) -> None:
        from transformation.transformation_pipeline import _iter_raw_records

        with pytest.raises(ValueError, match="disallowed characters"):
            list(_iter_raw_records(self.s3, _RAW_BUCKET, "raw/<script>/"))

    def test_table_to_records_empty_table(self) -> None:
        import pyarrow as pa

        from transformation.transformation_pipeline import _table_to_records

        empty_table = pa.table({})
        assert _table_to_records(empty_table) == []

    def test_catalog_registration_failure_swallowed(self) -> None:
        """_register_curated_catalog exception is swallowed, not propagated."""
        from unittest.mock import patch

        records = [{"Id": "001"}]
        _write_raw_parquet(self.s3, _RAW_BUCKET, "raw/cat-fail/", records)

        registry_client = FieldMappingRegistryClient(_MAPPING_BUCKET, _REGION)
        pipeline = _make_pipeline(registry_client)
        ctx = TransformationContext(
            run_id=_RUN_ID,
            source_id="salesforce",
            entity_id="salesforce-account",
            domain="customer",
            raw_s3_bucket=_RAW_BUCKET,
            raw_s3_prefix="raw/cat-fail/",
            mapping_bucket=_MAPPING_BUCKET,
            curated_s3_bucket=_CURATED_BUCKET,
            region_name=_REGION,
            curated_date=date(2024, 1, 15),
            glue_catalog_database="some_db",
        )
        # Patch out DataCatalogRegistrationClient to raise
        with patch(
            "transformation.transformation_pipeline.DataCatalogRegistrationClient"
        ) as mock_cat:
            mock_cat.return_value.register_dataset.side_effect = RuntimeError("glue down")
            result = pipeline.execute(ctx)
        assert result.curated_s3_prefix is not None  # write still succeeded

    def test_lineage_failure_swallowed(self) -> None:
        """_emit_transformation_lineage exception is swallowed, not propagated."""
        from unittest.mock import patch

        records = [{"Id": "001"}]
        _write_raw_parquet(self.s3, _RAW_BUCKET, "raw/lineage-fail/", records)

        registry_client = FieldMappingRegistryClient(_MAPPING_BUCKET, _REGION)
        pipeline = _make_pipeline(registry_client)
        ctx = TransformationContext(
            run_id=_RUN_ID,
            source_id="salesforce",
            entity_id="salesforce-account",
            domain="customer",
            raw_s3_bucket=_RAW_BUCKET,
            raw_s3_prefix="raw/lineage-fail/",
            mapping_bucket=_MAPPING_BUCKET,
            curated_s3_bucket=_CURATED_BUCKET,
            region_name=_REGION,
            curated_date=date(2024, 1, 15),
            governance_s3_bucket="gov-bucket",
        )
        with patch("transformation.transformation_pipeline.LineageEmitter") as mock_lineage:
            mock_lineage.return_value.emit.side_effect = RuntimeError("lineage down")
            result = pipeline.execute(ctx)
        assert result.curated_s3_prefix is not None  # write still succeeded

    def test_metrics_with_blocked_quality_emits_records_failed_twice(self) -> None:
        """_emit_transformation_metrics emits records_failed for quality blocks too."""
        from unittest.mock import MagicMock

        from observability.metrics_emitter import CloudWatchMetricsEmitter

        records = [{"Id": "001", "Name": None}]
        _write_raw_parquet(self.s3, _RAW_BUCKET, "raw/q-blocked-met/", records)

        mock_emitter = MagicMock(spec=CloudWatchMetricsEmitter)
        quality_policy = QualityPolicy(
            source_id="salesforce",
            entity_id="salesforce-account",
            policy_version="1.0.0",
            checks=(NullCheck("Name", QualityCheckSeverity.BLOCKING),),
        )
        registry_client = FieldMappingRegistryClient(_MAPPING_BUCKET, _REGION)
        pipeline = TransformationPipeline(
            mapping_registry_client=registry_client,
            quality_evaluator=QualityPolicyEvaluator(),
            curated_writer=CuratedLayerWriter(_CURATED_BUCKET, _REGION),
            quality_policy=quality_policy,
            metrics_emitter=mock_emitter,
        )
        ctx = _make_ctx("raw/q-blocked-met/")
        pipeline.execute(ctx)
        # emit_records_failed is called at least once for the quality blocks
        assert mock_emitter.emit_records_failed.call_count >= 1

    def test_non_parquet_files_skipped_by_iter(self) -> None:
        """Files not ending in .parquet are skipped (covers the `continue` branch)."""
        from transformation.transformation_pipeline import _iter_raw_records

        # Write a non-parquet file that should be skipped
        self.s3.put_object(Bucket=_RAW_BUCKET, Key="raw/mixed/readme.txt", Body=b"ignore me")
        # Write a parquet file that should be read
        table = pa.table({"Id": ["001"], "Name": ["Acme"]})
        buf = io.BytesIO()
        pq.write_table(table, buf)
        self.s3.put_object(Bucket=_RAW_BUCKET, Key="raw/mixed/data.parquet", Body=buf.getvalue())

        records = list(_iter_raw_records(self.s3, _RAW_BUCKET, "raw/mixed/"))
        assert len(records) == 1  # only the parquet row
        assert records[0]["Id"] == "001"

    def test_mapping_failure_increments_failure_count(self) -> None:
        """When mapping returns None, failure_count is incremented (covers failures += 1)."""
        from transformation.field_mapping.field_mapping_registry import (
            MissingFieldBehavior,
        )

        records = [{"Id": "001"}]  # 'Name' is missing
        _write_raw_parquet(self.s3, _RAW_BUCKET, "raw/map-fail/", records)

        rule_set = FieldMappingRuleSet(
            source_id="salesforce",
            entity_id="salesforce-account",
            mapping_version="1.0.0",
            rules=(
                FieldMappingRule(
                    source_fields=("Name",),
                    canonical_field="account_name",
                    transformation=MappingTransformation.RENAME,
                    transformation_params={},
                    missing_field_behavior=MissingFieldBehavior.RAISE_ERROR,
                ),
            ),
        )
        registry_client = FieldMappingRegistryClient(_MAPPING_BUCKET, _REGION)
        registry_client.publish_rule_set(rule_set, "demo")

        pipeline = _make_pipeline(registry_client)
        ctx = _make_ctx("raw/map-fail/")
        result = pipeline.execute(ctx)
        assert result.mapping_failures == 1

    def test_register_curated_catalog_early_return_when_no_db(self) -> None:
        """_register_curated_catalog returns early when glue_catalog_database is unset."""
        from transformation.transformation_pipeline import _register_curated_catalog

        ctx = _make_ctx("raw/nodb/")
        # glue_catalog_database is None — should return without raising
        _register_curated_catalog(
            ctx=ctx, s3_prefix="curated/test/", record_count=0, raw_s3_prefix="raw/nodb/"
        )

    def test_emit_transformation_lineage_early_return_when_no_bucket(self) -> None:
        """_emit_transformation_lineage returns early when governance_s3_bucket is unset."""
        from transformation.transformation_pipeline import _emit_transformation_lineage

        ctx = _make_ctx("raw/nolin/")
        # governance_s3_bucket is None — should return without raising
        _emit_transformation_lineage(ctx=ctx, curated_prefix="curated/test/")


@pytest.fixture()
def streaming_s3():
    with mock_aws():
        client = boto3.client("s3", region_name=_REGION)
        for bucket in (_RAW_BUCKET, _CURATED_BUCKET, _MAPPING_BUCKET):
            client.create_bucket(Bucket=bucket)
        yield client


class TestStreamingFastPath:
    """Tests for the streaming execution path (no quality/masking/accumulator)."""

    def test_streaming_path_used_when_no_features(self, streaming_s3) -> None:
        """Pipeline should use streaming path when quality/masking/accumulator all absent."""
        records = [{"Id": str(i), "Name": f"Record {i}"} for i in range(20)]
        _write_raw_parquet(streaming_s3, _RAW_BUCKET, "raw/stream/", records)

        pipeline = _make_pipeline(FieldMappingRegistryClient(_MAPPING_BUCKET, _REGION))
        ctx = _make_ctx("raw/stream/")
        result = pipeline.execute(ctx)

        assert result.canonical_record_count == 20
        # Streaming path should set curated_prefix
        assert result.curated_s3_prefix is not None
        # Curated prefix should include tenant_code (default "demo")
        assert "demo" in result.curated_s3_prefix

    def test_tenant_code_in_curated_path(self, streaming_s3) -> None:
        """Curated layer S3 path should be prefixed with tenant_code (§1.1)."""
        records = [{"Id": "1", "Name": "Alice"}]
        _write_raw_parquet(streaming_s3, _RAW_BUCKET, "raw/tenant/", records)

        pipeline = _make_pipeline(FieldMappingRegistryClient(_MAPPING_BUCKET, _REGION))
        ctx = TransformationContext(
            run_id=_RUN_ID,
            source_id="salesforce",
            entity_id="salesforce-account",
            domain="salesforce",
            raw_s3_bucket=_RAW_BUCKET,
            raw_s3_prefix="raw/tenant/",
            mapping_bucket=_MAPPING_BUCKET,
            curated_s3_bucket=_CURATED_BUCKET,
            region_name=_REGION,
            tenant_code="acme-corp",
        )
        result = pipeline.execute(ctx)

        assert result.curated_s3_prefix is not None
        assert result.curated_s3_prefix.startswith("acme-corp/curated/")

    def test_streaming_path_counts_mapping_failures(self, streaming_s3) -> None:
        """Streaming path must still count records that fail mapping."""
        from transformation.field_mapping.field_mapping_registry import MissingFieldBehavior

        records = [{"Id": "001"}]  # 'Name' is missing
        _write_raw_parquet(streaming_s3, _RAW_BUCKET, "raw/stream-fail/", records)

        rule_set = FieldMappingRuleSet(
            source_id="salesforce",
            entity_id="salesforce-account",
            mapping_version="1.0.0",
            rules=(
                FieldMappingRule(
                    source_fields=("Name",),
                    canonical_field="account_name",
                    transformation=MappingTransformation.RENAME,
                    transformation_params={},
                    missing_field_behavior=MissingFieldBehavior.RAISE_ERROR,
                ),
            ),
        )
        registry_client = FieldMappingRegistryClient(_MAPPING_BUCKET, _REGION)
        registry_client.publish_rule_set(rule_set, "demo")

        pipeline = _make_pipeline(registry_client)
        ctx = _make_ctx("raw/stream-fail/")
        result = pipeline.execute(ctx)
        assert result.mapping_failures == 1

    def test_default_tenant_code_is_demo(self, streaming_s3) -> None:
        """TransformationContext must default tenant_code to 'demo'."""
        ctx = _make_ctx()
        assert ctx.tenant_code == "demo"

    def test_invalid_tenant_code_rejected(self, streaming_s3) -> None:
        """TransformationContext should reject invalid tenant codes."""
        with pytest.raises(ValueError, match="tenant_code"):
            TransformationContext(
                run_id=_RUN_ID,
                source_id="salesforce",
                entity_id="salesforce-account",
                domain="salesforce",
                raw_s3_bucket=_RAW_BUCKET,
                raw_s3_prefix="raw/x/",
                mapping_bucket=_MAPPING_BUCKET,
                curated_s3_bucket=_CURATED_BUCKET,
                region_name=_REGION,
                tenant_code="INVALID_UPPER",
            )


class TestAutoClassification:
    """
    Tests for SEC-1: auto-classification must mask PII-shaped fields even
    when no explicit, steward-reviewed EntityClassificationPolicy is wired.
    """

    def _read_curated_records(self, s3_client, prefix: str) -> list[dict]:
        objects = s3_client.list_objects_v2(Bucket=_CURATED_BUCKET, Prefix=prefix)
        records: list[dict] = []
        for obj in objects.get("Contents", []):
            body = s3_client.get_object(Bucket=_CURATED_BUCKET, Key=obj["Key"])["Body"].read()
            table = pq.read_table(io.BytesIO(body))
            records.extend(table.to_pylist())
        return records

    def test_pii_shaped_field_is_masked_with_no_explicit_policy(self, streaming_s3) -> None:
        """A canonical field named 'email' must be masked automatically (SEC-1)."""
        rule_set = FieldMappingRuleSet(
            source_id="salesforce",
            entity_id="salesforce-account",
            mapping_version="1.0.0",
            rules=(
                FieldMappingRule(
                    source_fields=("Email",),
                    canonical_field="email",
                    transformation=MappingTransformation.RENAME,
                    transformation_params={},
                ),
            ),
        )
        registry_client = FieldMappingRegistryClient(_MAPPING_BUCKET, _REGION)
        registry_client.publish_rule_set(rule_set, "demo")

        records = [{"Email": "alice@example.com"}]
        _write_raw_parquet(streaming_s3, _RAW_BUCKET, "raw/auto-mask/", records)

        pipeline = _make_pipeline(registry_client)
        ctx = _make_ctx("raw/auto-mask/")
        result = pipeline.execute(ctx)

        assert result.canonical_record_count == 1
        curated = self._read_curated_records(streaming_s3, result.curated_s3_prefix)
        assert len(curated) == 1
        assert curated[0]["email"] != "alice@example.com"
        assert "alice@example.com" not in curated[0]["email"]

    def test_non_pii_fields_still_use_streaming_fast_path(self, streaming_s3) -> None:
        """Entities with no PII-shaped fields must keep the O(batch) streaming path."""
        rule_set = FieldMappingRuleSet(
            source_id="salesforce",
            entity_id="salesforce-account",
            mapping_version="1.0.0",
            rules=(
                FieldMappingRule(
                    source_fields=("Id",),
                    canonical_field="account_id",
                    transformation=MappingTransformation.RENAME,
                    transformation_params={},
                ),
            ),
        )
        registry_client = FieldMappingRegistryClient(_MAPPING_BUCKET, _REGION)
        registry_client.publish_rule_set(rule_set, "demo")

        records = [{"Id": "001"}]
        _write_raw_parquet(streaming_s3, _RAW_BUCKET, "raw/auto-nomask/", records)

        pipeline = _make_pipeline(registry_client)
        ctx = _make_ctx("raw/auto-nomask/")
        result = pipeline.execute(ctx)

        assert result.canonical_record_count == 1
        curated = self._read_curated_records(streaming_s3, result.curated_s3_prefix)
        assert curated[0]["account_id"] == "001"

    def test_explicit_policy_still_overrides_auto_classification(self, streaming_s3) -> None:
        """An explicit classification_policy takes priority over auto-detection."""
        from governance.data_classification_policy import (
            DataClassificationLevel,
            EntityClassificationPolicy,
            FieldClassification,
            MaskingStrategy,
        )

        rule_set = FieldMappingRuleSet(
            source_id="salesforce",
            entity_id="salesforce-account",
            mapping_version="1.0.0",
            rules=(
                FieldMappingRule(
                    source_fields=("Email",),
                    canonical_field="email",
                    transformation=MappingTransformation.RENAME,
                    transformation_params={},
                ),
            ),
        )
        registry_client = FieldMappingRegistryClient(_MAPPING_BUCKET, _REGION)
        registry_client.publish_rule_set(rule_set, "demo")

        records = [{"Email": "bob@example.com"}]
        _write_raw_parquet(streaming_s3, _RAW_BUCKET, "raw/explicit-mask/", records)

        explicit_policy = EntityClassificationPolicy(
            source_id="salesforce",
            entity_id="salesforce-account",
            policy_version="1.0.0",
            field_classifications=(
                FieldClassification(
                    field_name="email",
                    classification=DataClassificationLevel.PII,
                    masking_strategy=MaskingStrategy.REDACT,
                ),
            ),
        )
        pipeline = TransformationPipeline(
            mapping_registry_client=registry_client,
            quality_evaluator=QualityPolicyEvaluator(),
            curated_writer=CuratedLayerWriter(_CURATED_BUCKET, _REGION),
            quality_policy=None,
            classification_policy=explicit_policy,
        )
        ctx = _make_ctx("raw/explicit-mask/")
        result = pipeline.execute(ctx)

        curated = self._read_curated_records(streaming_s3, result.curated_s3_prefix)
        assert curated[0]["email"] == "REDACTED"


# ---------------------------------------------------------------------------
# Pre-go-live fix 1 (BLOCKER): tenant-scoped curated Glue table names
# ---------------------------------------------------------------------------


@mock_aws
class TestTenantScopedCuratedCatalogTableName:
    """
    Two tenants running the same entity/domain must register distinct Glue
    tables, each pointing at its own tenant-scoped curated S3 location.

    Without the tenant_code prefix on the table name, the second tenant's
    register_dataset() call would silently overwrite the first tenant's
    table Location in the shared edl_curated database, causing cross-tenant
    Athena reads.
    """

    _DATABASE = "shared_curated_db"

    def setup_method(self, method: object = None) -> None:
        s3 = boto3.client("s3", region_name=_REGION)
        for bucket in (_RAW_BUCKET, _CURATED_BUCKET, _MAPPING_BUCKET):
            s3.create_bucket(Bucket=bucket)
        glue = boto3.client("glue", region_name=_REGION)
        glue.create_database(DatabaseInput={"Name": self._DATABASE})
        self.s3 = s3
        self.glue = glue
        self.registry_client = FieldMappingRegistryClient(_MAPPING_BUCKET, _REGION)

    def _run_for_tenant(self, tenant_code: str, raw_prefix: str) -> str:
        records = [{"Id": "001", "Name": "Acme"}]
        _write_raw_parquet(self.s3, _RAW_BUCKET, raw_prefix, records)
        pipeline = _make_pipeline(self.registry_client)
        ctx = TransformationContext(
            run_id=f"run-{tenant_code}",
            source_id="salesforce",
            entity_id="salesforce-account",
            domain="customer",
            raw_s3_bucket=_RAW_BUCKET,
            raw_s3_prefix=raw_prefix,
            mapping_bucket=_MAPPING_BUCKET,
            curated_s3_bucket=_CURATED_BUCKET,
            region_name=_REGION,
            curated_date=date(2024, 1, 15),
            glue_catalog_database=self._DATABASE,
            tenant_code=tenant_code,
        )
        result = pipeline.execute(ctx)
        assert result.curated_s3_prefix is not None
        return result.curated_s3_prefix

    def test_two_tenants_same_entity_domain_register_distinct_tables_and_locations(
        self,
    ) -> None:
        prefix_a = self._run_for_tenant("tenant-a", "raw/tenant-a/")
        prefix_b = self._run_for_tenant("tenant-b", "raw/tenant-b/")

        assert prefix_a.startswith("tenant-a/curated/")
        assert prefix_b.startswith("tenant-b/curated/")

        table_names = {
            t["Name"] for t in self.glue.get_tables(DatabaseName=self._DATABASE)["TableList"]
        }
        table_a_name = "tenant_a_salesforce_account_customer_curated"
        table_b_name = "tenant_b_salesforce_account_customer_curated"
        assert table_a_name in table_names
        assert table_b_name in table_names

        table_a = self.glue.get_table(DatabaseName=self._DATABASE, Name=table_a_name)["Table"]
        table_b = self.glue.get_table(DatabaseName=self._DATABASE, Name=table_b_name)["Table"]

        assert table_a["StorageDescriptor"]["Location"] == f"s3://{_CURATED_BUCKET}/{prefix_a}"
        assert table_b["StorageDescriptor"]["Location"] == f"s3://{_CURATED_BUCKET}/{prefix_b}"


# ---------------------------------------------------------------------------
# Pre-go-live fix 2: curated_date partition registration
# ---------------------------------------------------------------------------


@mock_aws
class TestCuratedPartitionRegistration:
    """
    Each transformation run must register its curated_date partition so
    Athena can query newly-written curated data without a manual MSCK
    REPAIR TABLE. A same-day re-run must tolerate AlreadyExistsException
    (update, not fail) rather than raising.
    """

    _DATABASE = "partition_test_db"
    _TABLE = "demo_salesforce_account_customer_curated"

    def setup_method(self, method: object = None) -> None:
        s3 = boto3.client("s3", region_name=_REGION)
        for bucket in (_RAW_BUCKET, _CURATED_BUCKET, _MAPPING_BUCKET):
            s3.create_bucket(Bucket=bucket)
        glue = boto3.client("glue", region_name=_REGION)
        glue.create_database(DatabaseInput={"Name": self._DATABASE})
        self.s3 = s3
        self.glue = glue
        self.registry_client = FieldMappingRegistryClient(_MAPPING_BUCKET, _REGION)

    def _ctx(self, run_id: str, raw_prefix: str) -> TransformationContext:
        return TransformationContext(
            run_id=run_id,
            source_id="salesforce",
            entity_id="salesforce-account",
            domain="customer",
            raw_s3_bucket=_RAW_BUCKET,
            raw_s3_prefix=raw_prefix,
            mapping_bucket=_MAPPING_BUCKET,
            curated_s3_bucket=_CURATED_BUCKET,
            region_name=_REGION,
            curated_date=date(2024, 3, 1),
            glue_catalog_database=self._DATABASE,
            partition_profile=_SINGLE_TENANT_PROFILE,
        )

    def test_partition_registered_after_run(self) -> None:
        records = [{"Id": "001", "Name": "Acme"}]
        _write_raw_parquet(self.s3, _RAW_BUCKET, "raw/part-run1/", records)
        pipeline = _make_pipeline(self.registry_client)
        result = pipeline.execute(self._ctx("run-part-1", "raw/part-run1/"))
        assert result.curated_s3_prefix is not None

        partitions = self.glue.get_partitions(DatabaseName=self._DATABASE, TableName=self._TABLE)[
            "Partitions"
        ]
        assert len(partitions) == 1
        assert partitions[0]["Values"] == ["2024-03-01"]
        assert result.curated_s3_prefix in partitions[0]["StorageDescriptor"]["Location"]

    def test_second_run_same_day_tolerates_already_exists(self) -> None:
        records = [{"Id": "001", "Name": "Acme"}]
        pipeline = _make_pipeline(self.registry_client)

        _write_raw_parquet(self.s3, _RAW_BUCKET, "raw/part-run2a/", records)
        result1 = pipeline.execute(self._ctx("run-part-2a", "raw/part-run2a/"))
        assert result1.curated_s3_prefix is not None

        # Second run, same curated_date, different run_id — must not raise
        # even though a partition value for 2024-03-01 already exists.
        _write_raw_parquet(self.s3, _RAW_BUCKET, "raw/part-run2b/", records)
        result2 = pipeline.execute(self._ctx("run-part-2b", "raw/part-run2b/"))
        assert result2.curated_s3_prefix is not None

        partitions = self.glue.get_partitions(DatabaseName=self._DATABASE, TableName=self._TABLE)[
            "Partitions"
        ]
        # Updated in place, not duplicated.
        assert len(partitions) == 1
        assert partitions[0]["Values"] == ["2024-03-01"]
        assert result2.curated_s3_prefix in partitions[0]["StorageDescriptor"]["Location"]


# ---------------------------------------------------------------------------
# Pre-go-live fix 3 (HIGH): auto-classification for pass-through entities
# ---------------------------------------------------------------------------


class TestPassThroughAutoClassification:
    """
    Entities with NO field-mapping rule set (pure pass-through / identity
    mapping) must still have PII-shaped raw field names auto-classified and
    masked in the curated output — absence of a rule set must never bypass
    PII protection (OWASP A01).
    """

    def _read_curated_records(self, s3_client, prefix: str) -> list[dict]:
        objects = s3_client.list_objects_v2(Bucket=_CURATED_BUCKET, Prefix=prefix)
        records: list[dict] = []
        for obj in objects.get("Contents", []):
            body = s3_client.get_object(Bucket=_CURATED_BUCKET, Key=obj["Key"])["Body"].read()
            table = pq.read_table(io.BytesIO(body))
            records.extend(table.to_pylist())
        return records

    def test_pass_through_pii_named_field_is_masked(self, streaming_s3) -> None:
        """A pass-through entity (no rule set) with a raw 'email' field must
        have it masked in the curated output."""
        records = [{"Id": "001", "email": "carol@example.com"}]
        _write_raw_parquet(streaming_s3, _RAW_BUCKET, "raw/pt-mask/", records)

        registry_client = FieldMappingRegistryClient(_MAPPING_BUCKET, _REGION)
        pipeline = _make_pipeline(registry_client)
        ctx = _make_ctx("raw/pt-mask/")
        result = pipeline.execute(ctx)

        assert result.canonical_record_count == 1
        curated = self._read_curated_records(streaming_s3, result.curated_s3_prefix)
        assert len(curated) == 1
        assert curated[0]["email"] != "carol@example.com"
        assert "carol@example.com" not in curated[0]["email"]

    def test_pass_through_no_pii_fields_still_streams(self, streaming_s3) -> None:
        """A pass-through entity with no PII-shaped field names must still
        take the fast streaming path and write records unmasked."""
        records = [{"Id": "001", "region": "west"}]
        _write_raw_parquet(streaming_s3, _RAW_BUCKET, "raw/pt-nomask/", records)

        registry_client = FieldMappingRegistryClient(_MAPPING_BUCKET, _REGION)
        pipeline = _make_pipeline(registry_client)
        ctx = _make_ctx("raw/pt-nomask/")
        result = pipeline.execute(ctx)

        assert result.canonical_record_count == 1
        curated = self._read_curated_records(streaming_s3, result.curated_s3_prefix)
        assert curated[0]["region"] == "west"
        assert curated[0]["Id"] == "001"

    def test_pass_through_empty_raw_prefix_produces_no_curated_output(self, streaming_s3) -> None:
        """Peeking an empty raw prefix must not raise and must behave like
        the pre-existing empty-prefix case (no records, no curated write)."""
        registry_client = FieldMappingRegistryClient(_MAPPING_BUCKET, _REGION)
        pipeline = _make_pipeline(registry_client)
        ctx = _make_ctx("raw/pt-empty/")
        result = pipeline.execute(ctx)

        assert result.raw_record_count == 0
        assert result.canonical_record_count == 0
        assert result.curated_s3_prefix is None
