"""
The one declaration of which DynamoDB tables hold tenant data (DL-PORT-04, DL-SEC-02).

Two things needed this list and neither could derive it: the deletion saga, which must sweep every
tenant-keyed table or refuse to certify, and the IAM tenant boundary, whose `dynamodb:LeadingKeys`
condition applies to exactly these tables. Before this existed each repository named its own table
in its own module constant and nothing enumerated them, so "did we cover every table" was answered
by grep.

`EdlDeletionCertificate` is deliberately **excluded from the sweep**: it is the evidence the
deletion happened, so deleting it as part of the deletion would destroy the record that proves
compliance (SOW §24.7 requires written confirmation to survive).
"""

from __future__ import annotations

from typing import Final

# Partition key is `tenant_code` itself — queryable by equality.
TENANT_KEYED_TABLES: Final[tuple[str, ...]] = (
    "EdlBackfillJob",
    "EdlBrandRegistry",
    "EdlConfigGovernance",
    "EdlConfigRestatement",
    "EdlDataQualityException",
    "EdlEffectiveConfig",
    "EdlExportJob",
    "EdlQualityPolicyAttachment",
    "EdlReconciliationReport",
    "EdlSavedQuery",
    "EdlScopeUnit",
    "EdlSemanticApproval",
    "EdlSemanticModel",
    "EdlServingCredentialClaim",
    "EdlServingStoreConfig",
    "EdlSourceConnection",
    "EdlSourceOnboardingRegistry",
    "EdlSubprocessorRegister",
    "EdlTenantUsage",
    "EdlTwinIndex",
    "EdlWebhookEventDedup",
    "EdlWorkflowCircuitBreaker",
    "EdlWorkflowDefinition",
    "EdlWorkflowDestination",
    "EdlWorkflowExecution",
    "EdlWorkflowIdempotency",
    "EdlWorkflowTask",
)

# Partition key is `tenant_scoped_key(tenant_code, ...)`, i.e. `tenant#...` — prefix-matched, not
# queried, which is why `dynamodb:LeadingKeys` needs both the bare and the `#`-suffixed form.
TENANT_SCOPED_KEY_TABLES: Final[tuple[str, ...]] = (
    "EdlEntityExtractionConfig",
    "EdlWatermarkRepository",
)

# Keyed by `run_id`, with `tenant_code` as an attribute and a tenant GSI. Included in the sweep
# because it holds tenant data, excluded from the LeadingKeys condition because its partition key
# is not tenant-derived.
TENANT_ATTRIBUTED_TABLES: Final[tuple[str, ...]] = ("EdlRunAuditLog",)

# The GSI those tables expose for tenant-scoped reads. Named here beside the table list so a
# sweep cannot fall back to a prefix scan that silently matches nothing.
TENANT_ATTRIBUTED_INDEX: Final[str] = "tenant-started-index"

# Holds the proof that a deletion happened; never swept by one.
DELETION_EVIDENCE_TABLES: Final[tuple[str, ...]] = ("EdlDeletionCertificate",)

TENANT_SCOPED_TABLES: Final[tuple[str, ...]] = (
    TENANT_KEYED_TABLES + TENANT_SCOPED_KEY_TABLES + TENANT_ATTRIBUTED_TABLES
)

ALL_PLATFORM_TABLES: Final[frozenset[str]] = frozenset(
    TENANT_SCOPED_TABLES + DELETION_EVIDENCE_TABLES + ("EdlEntityTypeRegistry",)
)
