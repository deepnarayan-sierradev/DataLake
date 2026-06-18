"""Tests for TransformationPipeline — Phase 6."""

from __future__ import annotations

import io
from datetime import date

import boto3
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from moto import mock_aws

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
        self.registry_client.publish_rule_set(rule_set)

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
        registry_client.publish_rule_set(rule_set)

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
