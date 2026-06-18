"""
Connector runtime package for the Enterprise Data Lake platform.

This package provides the metadata-driven connector framework that powers
all source extractions. The architecture is:

  ConnectorInterface (abstract) — contracts every source adapter must satisfy
  ConnectorCapabilities          — capability declaration for runtime routing
  FieldContract                  — output of metadata discovery
  QueryContract                  — output of query building (parameterized)
  ExtractionRecord               — single record from extraction stream
  ExtractionErrorClassification  — transient vs deterministic error taxonomy

Phase 1 scope: interfaces and contracts only.
Phase 3 scope: SalesforceConnector adapter.
Phase 4 scope: NetSuiteConnector and MySqlRdsConnector adapters.
"""
