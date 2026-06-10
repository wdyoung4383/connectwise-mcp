"""Helpers and guidance for the ConnectWise ``conditions`` query language.

This is the biggest footgun for an LLM driving the API, so the rules live here
and are surfaced in tool docstrings.

Syntax cheatsheet (passed as the ``conditions`` query param):
    field op value [and|or field op value ...]

  - Operators: =  !=  <  <=  >  >=  and  or  not  contains  like  in
  - String values are single-quoted:        status/name = 'Open'
  - Numbers are bare:                        company/id = 123
  - Nested fields use slashes:               company/identifier = 'ACME'
  - Dates/times use square brackets, UTC:    lastUpdated > [2026-01-01T00:00:00Z]
  - Booleans:                                closedFlag = false
  - Grouping with parentheses:               (status/name='Open' or status/name='New') and board/id=1
  - Membership:                              id in (1,2,3)

Notes:
  - ``conditions`` filters the top-level object; ``childConditions`` filters
    child collections; ``customFieldConditions`` filters custom fields.
"""

from __future__ import annotations

CONDITIONS_HELP = __doc__


def quote(value) -> str:
    """Render a Python value as a ConnectWise conditions literal."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if value is None:
        return "null"
    return "'" + str(value).replace("'", "\\'") + "'"


def eq(field: str, value) -> str:
    """Build a single ``field = value`` clause with correct quoting."""
    return f"{field} = {quote(value)}"


def join_and(*clauses: str) -> str:
    return " and ".join(c for c in clauses if c)
