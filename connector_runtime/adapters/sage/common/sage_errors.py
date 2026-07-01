"""
Shared exception hierarchy for the Sage ERP connector adapter.

These exceptions are raised by product-specific strategy implementations
(metadata clients, query engines) and caught/classified by the shared
SageConnector.classify_extraction_error().

Placing them here — rather than in any product module — avoids the
cross-product import coupling that arises when one product module (e.g. X3)
must import error types from another (e.g. Intacct).

Hierarchy:
    SageMetadataError
      └── SageMetadataDeterministicError
      └── SageMetadataTransientError
    SageQueryBuildError
"""

from __future__ import annotations


class SageMetadataError(Exception):
    """Base class for Sage metadata/schema discovery failures."""


class SageMetadataDeterministicError(SageMetadataError):
    """
    Deterministic metadata failure — the object path does not exist or the
    API returned an unrecognisable schema.  No amount of retrying will fix
    this; the extraction run should fail fast and route to the DLQ.
    """


class SageMetadataTransientError(SageMetadataError):
    """
    Transient metadata failure — a network, timeout, or 5xx error occurred
    while fetching the schema endpoint.  The run is retry-eligible.
    """


class SageQueryBuildError(Exception):
    """
    Raised when a product query engine cannot build a valid query due to
    invalid inputs (bad field name, missing watermark field, etc.).
    """
