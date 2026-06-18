"""
Contracts package for the Enterprise Data Lake platform.

This package defines shared data contracts, schemas, and canonical event types
used across all platform services. Contracts are the source of truth for
inter-service communication, log schemas, and pipeline stage boundaries.

Import guidance:
    from contracts.observability_contract import StructuredLogEvent, PipelineStage, RunStatus
    from contracts.pipeline_stage_contract import PipelineStageContract
    from contracts.entity_configuration_contract import EntityExtractionConfig
    from contracts.identifier_policy import (
        STABLE_ID_PATTERN,
        RUN_ID_PATTERN,
        PROHIBITED_IDENTIFIERS,
        validate_stable_id,
        validate_run_id,
    )
"""
