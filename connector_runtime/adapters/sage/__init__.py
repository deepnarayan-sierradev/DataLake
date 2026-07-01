"""
Sage ERP connector adapter sub-package.

Phase 5: SageConnector — generic multi-product Sage ERP adapter.

Supported products (Phase 5):
    - Sage Intacct (REST API, OAuth 2.0)

Future products (add without modifying this package's existing code):
    - Sage X3
    - Sage Accounting
    - Sage 100 / 200 / 300

Registration happens in sage_connector.py at import time via
@connector_registry.register("sage") and connector_registry.register_builder("sage", ...).
This __init__.py is intentionally minimal — it does not re-export anything,
keeping the package boundary clean and import paths explicit.
"""
