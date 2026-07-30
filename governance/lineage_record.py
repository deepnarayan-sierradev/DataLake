"""
Data lineage record.

Captures lineage at each pipeline stage boundary and persists it to S3.
Every record traces:
  - what was produced (target dataset / S3 path)
  - what it was derived from (source datasets / S3 paths)
  - the transformation or process that produced it (pipeline stage + run_id)
  - when the lineage was captured

Lineage records are written to:
  s3://{governance_bucket}/{tenant_code}/lineage/{entity_id}/{run_id}/{stage}-lineage.json

The `{tenant_code}/` prefix is what makes the key IAM-enforceable, matching every other data
layer (DL-SEC-04). This docstring described the older unscoped layout until 2026-07-29, after
the code had already been fixed — a reader auditing isolation from the docstring would have
concluded the gap was open when it was closed.

Security (OWASP A09):
  - Lineage records never include raw data values.
  - Only S3 path references and run metadata are stored.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

import boto3
from botocore.exceptions import ClientError

from contracts.identifier_policy import DEFAULT_TENANT_CODE, validate_tenant_code
from observability.structured_logger import get_platform_logger

_logger = get_platform_logger(__name__)


def lineage_s3_key(tenant_code: str, entity_id: str, run_id: str, stage: LineageStage) -> str:
    """Tenant-prefixed lineage key, matching every other layer's convention (DL-SEC-04)."""
    validate_tenant_code(tenant_code)
    return f"{tenant_code}/lineage/{entity_id}/{run_id}/{stage.value}-lineage.json"


class LineageStage(StrEnum):
    EXTRACTION = "extraction"
    TRANSFORMATION = "transformation"
    ENTITY_RESOLUTION = "entity_resolution"
    ANALYTICS_PUBLICATION = "analytics_publication"
    SERVING_STORE_LOAD = "serving_store_load"


@dataclass(frozen=True)
class LineageNode:
    """Represents one dataset (source or target) in a lineage graph."""

    name: str  # logical dataset name (e.g., "salesforce-account-curated")
    s3_path: str  # S3 URI (e.g., s3://bucket/prefix/)
    data_layer: str  # "raw" | "curated" | "analytics"


@dataclass(frozen=True)
class LineageRecord:
    """
    Single lineage event: one or more source nodes → one target node.

    Does NOT include data values — only structural metadata.
    """

    run_id: str
    source_id: str
    entity_id: str
    pipeline_stage: LineageStage
    source_nodes: tuple[LineageNode, ...]
    target_node: LineageNode
    record_count: int
    captured_at: str  # ISO-8601 UTC
    additional_context: dict[str, str]  # e.g., mapping_version, policy_version
    tenant_code: str = DEFAULT_TENANT_CODE
    connection_id: str | None = None


class LineageEmitter:
    """
    Persists LineageRecord objects to S3.

    One instance per governance service.  Lineage records are written
    immediately after each pipeline stage completes.
    """

    def __init__(self, governance_s3_bucket: str, region_name: str) -> None:
        self._governance_bucket = governance_s3_bucket
        self._region_name = region_name
        self._s3: Any = boto3.client("s3", region_name=region_name)

    def emit(self, record: LineageRecord) -> str:
        """
        Persist a lineage record to S3.

        Returns the S3 key of the written record.
        Raises LineageEmissionError on S3 write failure.
        """
        key = lineage_s3_key(
            record.tenant_code, record.entity_id, record.run_id, record.pipeline_stage
        )

        payload = _serialise_lineage_record(record)

        try:
            self._s3.put_object(
                Bucket=self._governance_bucket,
                Key=key,
                Body=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
                ContentType="application/json",
            )
        except Exception as exc:
            raise LineageEmissionError(
                f"Failed to write lineage record for run_id={record.run_id!r}: {type(exc).__name__}"
            ) from exc

        _logger.info(
            "lineage_record_emitted",
            run_id=record.run_id,
            source_id=record.source_id,
            entity_id=record.entity_id,
            pipeline_stage=record.pipeline_stage.value,
            s3_key=key,
        )

        return key

    def load(
        self,
        run_id: str,
        entity_id: str,
        stage: LineageStage,
        tenant_code: str = DEFAULT_TENANT_CODE,
    ) -> LineageRecord:
        """
        Load a previously emitted lineage record from S3.

        Raises LineageRecordNotFoundError if not present.
        """
        key = lineage_s3_key(tenant_code, entity_id, run_id, stage)
        try:
            response = self._s3.get_object(Bucket=self._governance_bucket, Key=key)
            raw: dict[str, Any] = json.loads(response["Body"].read().decode("utf-8"))
            return _deserialise_lineage_record(raw)
        except ClientError as exc:
            error_code = exc.response.get("Error", {}).get("Code", "")
            if error_code in ("NoSuchKey", "404"):
                raise LineageRecordNotFoundError(run_id, entity_id, stage) from exc
            raise


def _with_config_versions(context: dict[str, str], config_versions: str | None) -> dict[str, str]:
    """DL-CFG-03: every lineage record carries the run's full pinned configuration set."""
    if not config_versions:
        return context
    return {**context, "config_versions": config_versions}


def build_extraction_lineage(
    run_id: str,
    source_id: str,
    entity_id: str,
    raw_s3_prefix: str,
    raw_s3_bucket: str,
    record_count: int,
    schema_version: str,
    tenant_code: str = DEFAULT_TENANT_CODE,
    config_versions: str | None = None,
) -> LineageRecord:
    """Factory: build a lineage record for an extraction stage."""
    return LineageRecord(
        run_id=run_id,
        source_id=source_id,
        entity_id=entity_id,
        pipeline_stage=LineageStage.EXTRACTION,
        source_nodes=(
            LineageNode(name=source_id, s3_path="external://source", data_layer="source"),
        ),
        target_node=LineageNode(
            name=f"{entity_id}-raw",
            s3_path=f"s3://{raw_s3_bucket}/{raw_s3_prefix}",
            data_layer="raw",
        ),
        record_count=record_count,
        tenant_code=tenant_code,
        captured_at=datetime.now(UTC).isoformat(),
        additional_context=_with_config_versions(
            {"schema_version": schema_version, "source_id": source_id}, config_versions
        ),
    )


def build_transformation_lineage(
    run_id: str,
    source_id: str,
    entity_id: str,
    raw_s3_bucket: str,
    raw_s3_prefix: str,
    curated_s3_bucket: str,
    curated_s3_prefix: str,
    record_count: int,
    mapping_version: str,
    tenant_code: str = DEFAULT_TENANT_CODE,
    config_versions: str | None = None,
) -> LineageRecord:
    """Factory: build a lineage record for a transformation stage."""
    return LineageRecord(
        run_id=run_id,
        source_id=source_id,
        entity_id=entity_id,
        pipeline_stage=LineageStage.TRANSFORMATION,
        source_nodes=(
            LineageNode(
                name=f"{entity_id}-raw",
                s3_path=f"s3://{raw_s3_bucket}/{raw_s3_prefix}",
                data_layer="raw",
            ),
        ),
        target_node=LineageNode(
            name=f"{entity_id}-curated",
            s3_path=f"s3://{curated_s3_bucket}/{curated_s3_prefix}",
            data_layer="curated",
        ),
        record_count=record_count,
        tenant_code=tenant_code,
        captured_at=datetime.now(UTC).isoformat(),
        additional_context=_with_config_versions(
            {"mapping_version": mapping_version}, config_versions
        ),
    )


def build_entity_resolution_lineage(
    run_id: str,
    source_id: str,
    entity_type: str,
    curated_s3_bucket: str,
    curated_s3_prefixes: tuple[str, ...],
    analytics_s3_bucket: str,
    analytics_s3_prefix: str,
    record_count: int,
    rule_set_version: str,
    survivorship_version: str,
    tenant_code: str = DEFAULT_TENANT_CODE,
    config_versions: str | None = None,
) -> LineageRecord:
    """Factory: build a lineage record for an entity resolution / golden record stage.

    ``curated_s3_prefixes`` accepts one entry per contributing source so that
    multi-source entities (e.g. company = Salesforce Account + NetSuite Customer)
    produce a full fan-in lineage graph rather than a single-source node.
    """
    source_nodes = tuple(
        LineageNode(
            name=f"{entity_type}-curated-source-{i + 1}",
            s3_path=f"s3://{curated_s3_bucket}/{prefix}",
            data_layer="curated",
        )
        for i, prefix in enumerate(curated_s3_prefixes)
    )
    return LineageRecord(
        run_id=run_id,
        source_id=source_id,
        entity_id=entity_type,
        pipeline_stage=LineageStage.ENTITY_RESOLUTION,
        source_nodes=source_nodes,
        target_node=LineageNode(
            name=f"{entity_type}-golden-records",
            s3_path=f"s3://{analytics_s3_bucket}/{analytics_s3_prefix}",
            data_layer="analytics",
        ),
        record_count=record_count,
        tenant_code=tenant_code,
        captured_at=datetime.now(UTC).isoformat(),
        additional_context=_with_config_versions(
            {
                "rule_set_version": rule_set_version,
                "survivorship_version": survivorship_version,
            },
            config_versions,
        ),
    )


def build_serving_store_lineage(
    run_id: str,
    source_id: str,
    entity_id: str,
    analytics_s3_bucket: str,
    analytics_s3_prefix: str,
    table_name: str,
    record_count: int,
    tenant_code: str = DEFAULT_TENANT_CODE,
    config_versions: str | None = None,
) -> LineageRecord:
    """Factory: build a lineage record for a serving store load stage."""
    return LineageRecord(
        run_id=run_id,
        source_id=source_id,
        entity_id=entity_id,
        pipeline_stage=LineageStage.SERVING_STORE_LOAD,
        source_nodes=(
            LineageNode(
                name=f"{entity_id}-analytics",
                s3_path=f"s3://{analytics_s3_bucket}/{analytics_s3_prefix}",
                data_layer="analytics",
            ),
        ),
        target_node=LineageNode(
            name=f"{entity_id}-serving-store",
            s3_path=f"rds://{table_name}",
            data_layer="serving",
        ),
        record_count=record_count,
        tenant_code=tenant_code,
        captured_at=datetime.now(UTC).isoformat(),
        additional_context=_with_config_versions({"table_name": table_name}, config_versions),
    )


def build_analytics_publication_lineage(
    run_id: str,
    source_id: str,
    entity_id: str,
    source_s3_bucket: str,
    source_s3_prefix: str,
    analytics_s3_bucket: str,
    analytics_s3_prefix: str,
    record_count: int,
    tenant_code: str = DEFAULT_TENANT_CODE,
    config_versions: str | None = None,
) -> LineageRecord:
    """Factory: build a lineage record for an analytics layer publication stage."""
    return LineageRecord(
        run_id=run_id,
        source_id=source_id,
        entity_id=entity_id,
        pipeline_stage=LineageStage.ANALYTICS_PUBLICATION,
        source_nodes=(
            LineageNode(
                name=f"{entity_id}-curated",
                s3_path=f"s3://{source_s3_bucket}/{source_s3_prefix}",
                data_layer="curated",
            ),
        ),
        target_node=LineageNode(
            name=f"{entity_id}-analytics",
            s3_path=f"s3://{analytics_s3_bucket}/{analytics_s3_prefix}",
            data_layer="analytics",
        ),
        record_count=record_count,
        tenant_code=tenant_code,
        captured_at=datetime.now(UTC).isoformat(),
        additional_context=_with_config_versions({}, config_versions),
    )


def _serialise_lineage_record(record: LineageRecord) -> dict[str, Any]:
    return {
        "run_id": record.run_id,
        "source_id": record.source_id,
        "entity_id": record.entity_id,
        "pipeline_stage": record.pipeline_stage.value,
        "source_nodes": [
            {"name": n.name, "s3_path": n.s3_path, "data_layer": n.data_layer}
            for n in record.source_nodes
        ],
        "target_node": {
            "name": record.target_node.name,
            "s3_path": record.target_node.s3_path,
            "data_layer": record.target_node.data_layer,
        },
        "record_count": record.record_count,
        "captured_at": record.captured_at,
        "additional_context": record.additional_context,
        "tenant_code": record.tenant_code,
        "connection_id": record.connection_id,
    }


def _deserialise_lineage_record(raw: dict[str, Any]) -> LineageRecord:
    return LineageRecord(
        run_id=raw["run_id"],
        source_id=raw["source_id"],
        entity_id=raw["entity_id"],
        pipeline_stage=LineageStage(raw["pipeline_stage"]),
        source_nodes=tuple(
            LineageNode(name=n["name"], s3_path=n["s3_path"], data_layer=n["data_layer"])
            for n in raw["source_nodes"]
        ),
        target_node=LineageNode(
            name=raw["target_node"]["name"],
            s3_path=raw["target_node"]["s3_path"],
            data_layer=raw["target_node"]["data_layer"],
        ),
        record_count=raw["record_count"],
        captured_at=raw["captured_at"],
        additional_context=raw.get("additional_context", {}),
        tenant_code=raw.get("tenant_code", DEFAULT_TENANT_CODE),
        connection_id=raw.get("connection_id"),
    )


class LineageEmissionError(Exception):
    """Raised when a lineage record cannot be written to S3."""


class LineageRecordNotFoundError(Exception):
    def __init__(self, run_id: str, entity_id: str, stage: LineageStage) -> None:
        super().__init__(f"Lineage record not found: {entity_id}/{run_id}/{stage.value}")
