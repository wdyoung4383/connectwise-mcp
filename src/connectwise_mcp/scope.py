"""Catalog scope: which operations this server's catalog *documents*.

The catalog is documentation, not access control. ``cw_get`` and ``cw_call``
execute whatever path the caller names, whether or not it appears here. That is
deliberate: the bundled spec trails the live ConnectWise release, so gating
execution on it would block endpoints that actually exist.

Scope therefore only decides what ``search_endpoints`` and ``describe_endpoint``
can find. Both knobs below default to everything. Narrow them and rerun
``scripts/generate_catalog.py`` to ship a smaller catalog.
"""

from __future__ import annotations

# OpenAPI tags to document. ``None`` documents every tag in the spec.
# To narrow, assign a set of tag names, e.g. {"Tickets", "Companies"}.
SELECTED_CATEGORIES: set[str] | None = None

# HTTP methods to document. Narrow to {"get"} for a read-only catalog.
#
# DELETE is deliberately absent. Deletes in ConnectWise are not recoverable
# through the API, so the destructive verb stays off the surface entirely:
# it is excluded from the catalog here AND rejected by writer.WRITE_METHODS,
# so neither discovery nor execution can reach it.
INCLUDED_METHODS: frozenset[str] = frozenset({"get", "post", "patch", "put"})
