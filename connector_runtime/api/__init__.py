"""
Control-plane API Lambda package for the Enterprise Data Lake SaaS platform.

Implements the multi-tenant REST surface used by tenant operators to:
  - provision a new tenant
  - register/list entity extraction configurations for a tenant
  - trigger an extraction pipeline run
  - query run status/history

See connector_runtime.api.control_plane_handler for the Lambda entry point.
"""

from __future__ import annotations
