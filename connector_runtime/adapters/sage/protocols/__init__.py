"""
Sage adapter protocols sub-package.

Exposes the three structural-typing contracts used by SageConnector to
communicate with product-specific strategy implementations.  Using Protocol
(PEP 544) rather than ABC inheritance keeps unit test doubles lightweight
and prevents coupling between the generic connector layer and product modules.
"""
