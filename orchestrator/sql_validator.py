"""Schema-aware SQL validator for the ``db_query`` custom tool.

The agent generates Postgres SQL against the Railway snapshot of Salesforce
data. Prompt-only column-naming rules keep failing in different shapes (PR
#170 fixed a missing ``opportunities.account_id``; the next failure will be
a wrong join, wrong table, or typo'd column name). This module validates
the SQL against the live schema BEFORE the query reaches Postgres, so the
agent gets actionable feedback ("column X doesn't exist on table Y;
available columns: [...]") and can self-correct on the next turn.

Scope (intentional, see ``docs/plans/...`` / PR description):
  1. ``non_select`` — reject anything that isn't a SELECT/WITH. Matches the
     existing safety check in ``session_runner.py:db_query`` and what
     ``db_adapter.query()`` actually supports.
  2. ``missing_table`` — every table referenced in FROM / JOIN must be in
     the supplied schema map.
  3. ``missing_column`` — every ``<table>.<column>`` qualified reference
     must exist in that table's column set. Unqualified columns must exist
     in at least one referenced table; ambiguity is tolerated (we don't
     try to be a query planner).

What this is NOT:
  - Not a full SQL parser. We use focused regex extraction over a
    string-literal-stripped copy of the query, then look up identifiers in
    the schema. sqlglot would be cleaner but isn't a project dependency and
    pulling it in is out of scope.
  - Not a security boundary. The SELECT-only check is a duplicate of the
    runtime guard, NOT a substitute for it.
  - Not exhaustive. We catch the common typo class (~80% of the
    column-naming bugs we've seen). Complex constructs (window functions
    over derived tables, lateral joins, recursive CTEs, etc.) may pass
    through unchecked. Pass-through is preferred over a false-positive.

Public API:
  ``validate_sql(sql, schema) -> dict``

  ``schema`` maps table-name → set of column names (case-insensitive).
  Empty schema → pass-through (``{"ok": True}``) so a DB-unavailable
  environment doesn't break the agent.
"""

from __future__ import annotations

import re
from typing import Dict, Iterable, List, Optional, Set, Tuple

# ---------------------------------------------------------------------------
# Tokens we never treat as column references when they appear bare in a
# select list. Keeps SELECT NULL / SELECT TRUE / SELECT COUNT(*) etc. from
# tripping the unqualified-column check.
# ---------------------------------------------------------------------------
_SQL_KEYWORDS: Set[str] = {
    "select",
    "from",
    "where",
    "group",
    "by",
    "order",
    "having",
    "limit",
    "offset",
    "join",
    "inner",
    "left",
    "right",
    "full",
    "outer",
    "cross",
    "lateral",
    "on",
    "using",
    "as",
    "and",
    "or",
    "not",
    "in",
    "is",
    "null",
    "true",
    "false",
    "distinct",
    "all",
    "case",
    "when",
    "then",
    "else",
    "end",
    "between",
    "like",
    "ilike",
    "exists",
    "asc",
    "desc",
    "with",
    "union",
    "intersect",
    "except",
    "any",
    "some",
    "interval",
    "date",
    "timestamp",
    "time",
    "current_date",
    "current_timestamp",
    "current_time",
    "now",
    "nulls",
    "first",
    "last",
    "values",
    "returning",
    "fetch",
    "next",
    "row",
    "rows",
    "only",
    "filter",
    "over",
    "partition",
    "window",
    "within",
    "ordinality",
    "cast",
    "extract",
    # EXTRACT() field names — Postgres accepts these as bare words in the
    # EXTRACT(<field> FROM <source>) form. Listing them as keywords keeps
    # the unqualified-column scan from flagging them after the internal
    # FROM/IN keyword inside EXTRACT bodies is blanked out.
    "year",
    "quarter",
    "month",
    "week",
    "day",
    "hour",
    "minute",
    "second",
    "millisecond",
    "microsecond",
    "decade",
    "century",
    "millennium",
    "epoch",
    "doy",
    "dow",
    "isodow",
    "isoyear",
    "timezone",
    "timezone_hour",
    "timezone_minute",
}


# A bareword that looks like an identifier. ``"my_col"`` (quoted) and
# backtick-style are NOT matched — we leave them alone since they're often
# used to escape reserved words or preserve case, both of which our
# case-insensitive lookup can't honor reliably.
_IDENT = r"[A-Za-z_][A-Za-z0-9_]*"

# Keywords that must NEVER be matched in an alias-position slot. Without
# this guard, ``FROM opportunities JOIN accounts`` would consume ``JOIN``
# as the alias of ``opportunities`` and skip the second table entirely
# (Codex P2 review on PR #178). The list is the set of SQL fragments that
# can legally follow a table name in FROM/JOIN context.
_ALIAS_FORBIDDEN_RE = (
    r"(?:JOIN|INNER|LEFT|RIGHT|FULL|OUTER|CROSS|NATURAL|LATERAL|"
    r"ON|USING|WHERE|GROUP|ORDER|HAVING|LIMIT|OFFSET|UNION|INTERSECT|"
    r"EXCEPT|FETCH|FOR|WINDOW|RETURNING)"
)

# ``<table_or_alias>.<column>`` — qualified column reference. We exclude
# ``<schema>.<table>.<column>`` here (3-part names get handled separately
# in _extract_tables).
_QUALIFIED_COL_RE = re.compile(rf"(?<![A-Za-z0-9_\.])({_IDENT})\.({_IDENT})\b")

# Bareword identifier used as a column candidate. Filtered later.
_BARE_IDENT_RE = re.compile(rf"(?<![A-Za-z0-9_\.]){_IDENT}(?![A-Za-z0-9_\(])")

# String-literal stripper. Postgres uses single quotes. Escaped quotes are
# doubled (``''``), so we match runs of non-quote chars and the doubling.
_STRING_LITERAL_RE = re.compile(r"'(?:[^']|'')*'")

# Line and block comments. PostgreSQL supports both styles.
_LINE_COMMENT_RE = re.compile(r"--[^\n]*")
_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)

# Drop dollar-quoted strings ``$$ ... $$`` and tagged variants ``$tag$ ... $tag$``
# Pre-empts a literal inside a function body bleeding identifiers.
_DOLLAR_QUOTED_RE = re.compile(r"\$([A-Za-z_]*)\$.*?\$\1\$", re.DOTALL)

# Postgres functions that legally use the keyword ``FROM`` *inside* their
# argument list as a delimiter, not as a table reference:
#   EXTRACT(YEAR FROM created_date)
#   OVERLAY(string PLACING substring FROM int)
#   POSITION(substring IN string)        -- IN, not FROM, but neutralize uniformly
#   SUBSTRING(string FROM int FOR int)
#   TRIM(BOTH ' ' FROM column)
# Without neutralization, the FROM/JOIN regex picks up the trailing
# identifier as if it were a table (Codex P2 review on PR #178).
# We match the function name + the matching close-paren via paren-depth
# tracking in ``_strip_noise``.
_FROM_INSIDE_FUNCS = (
    "extract",
    "overlay",
    "substring",
    "trim",
    "position",
)

# Cast target types Postgres recognises after ``::``. The unqualified
# scan would otherwise flag ``float`` / ``numeric`` / ``text`` etc as
# missing columns (Codex P2 review on PR #178). We strip the ``::type``
# suffix entirely so the identifier never reaches the bareword scan.
#
# Round 5: also handle multi-word Postgres types like ``double precision``,
# ``character varying``, ``time with time zone``, ``time without time
# zone``, ``timestamp with time zone``, ``bit varying``. Codex P2 review
# #5 on PR #178.
_CAST_TARGET_RE = re.compile(
    rf"""
    ::\s*
    (?:double\s+precision
       |character\s+varying
       |character\s+large\s+object
       |bit\s+varying
       |time\s+with(?:out)?\s+time\s+zone
       |timestamp\s+with(?:out)?\s+time\s+zone
       |{_IDENT}
    )
    (?:\s*\(\s*\d+(?:\s*,\s*\d+)?\s*\))?
    (?:\s*\[\s*\])*                  # array type suffix ``int[]``
    """,
    re.IGNORECASE | re.VERBOSE,
)

# CAST(<expr> AS <type>) — alternative spelling. Same multi-word concern
# as ``::``. We blank the ``AS <type>`` portion so ``CAST(amount AS
# double precision)`` doesn't trip ``precision`` as a missing column.
_CAST_AS_TARGET_RE = re.compile(
    r"""
    \bAS\s+
    (double\s+precision
     |character\s+varying
     |character\s+large\s+object
     |bit\s+varying
     |time\s+with(?:out)?\s+time\s+zone
     |timestamp\s+with(?:out)?\s+time\s+zone)
    (?:\s*\(\s*\d+(?:\s*,\s*\d+)?\s*\))?
    (?:\s*\[\s*\])*
    """,
    re.IGNORECASE | re.VERBOSE,
)


def validate_sql(sql: str, schema: Dict[str, Set[str]]) -> dict:
    """Validate ``sql`` against ``schema``.

    Args:
        sql: Raw SQL string as the agent emitted it.
        schema: Map of table-name (lower-case) → set of column names
            (lower-case). An empty map disables validation: the function
            returns ``{"ok": True}`` so a DB-unavailable environment
            degrades to a pass-through.

    Returns:
        ``{"ok": True}`` on pass, OR
        ``{"ok": False, "error": str, "code": str, "details": dict}`` on
        fail. ``code`` is one of: ``non_select``, ``missing_table``,
        ``missing_column``. ``details`` carries machine-readable context
        the agent can use to recover (table name, column name, available
        alternatives).
    """
    if not sql or not sql.strip():
        return _err(
            "Empty SQL string",
            "non_select",
            details={},
        )

    # Strip comments + string literals so identifier extraction doesn't
    # see column-name-shaped substrings inside string values like
    # ``WHERE name = 'discovery_call_booked'``.
    stripped = _strip_noise(sql)

    # ---- 1. non_select --------------------------------------------------
    # Read-only guard. Runs unconditionally — even when ``schema`` is
    # empty (DB unavailable) we still reject non-SELECT and mutating
    # CTEs, because the SELECT-only contract isn't conditional on
    # schema availability. Codex P1 round 5 review on PR #178.
    first_keyword = _first_keyword(stripped)
    if first_keyword not in ("select", "with"):
        return _err(
            f"Only SELECT/WITH queries are allowed; got '{first_keyword or '<empty>'}'",
            "non_select",
            details={"first_keyword": first_keyword},
        )

    # Even WITH queries can be mutating in Postgres — data-modifying CTEs
    # such as ``WITH d AS (DELETE FROM leads RETURNING id) SELECT id FROM d``
    # are valid SQL but violate the ``db_query`` read-only contract.
    # Reject any mutating keyword appearing anywhere in the (noise-
    # stripped) SQL. Codex P1 round 4 review on PR #178.
    if first_keyword == "with":
        mutating = re.search(
            r"\b(DELETE|UPDATE|INSERT|MERGE|TRUNCATE|DROP|ALTER|CREATE|GRANT|REVOKE)\b",
            stripped,
            re.IGNORECASE,
        )
        if mutating:
            keyword = mutating.group(1).upper()
            return _err(
                f"Mutating CTE detected ('{keyword}') — db_query is read-only",
                "non_select",
                details={"mutating_keyword": keyword},
            )

    if not schema:
        # Read-only guard already enforced above. Without a schema we
        # can't check column/table existence — degrade to pass-through
        # for the rest of the validations.
        return {"ok": True}

    # ---- 2. missing_table -----------------------------------------------
    # Normalize schema to lower-case keys for case-insensitive matching.
    schema_lc: Dict[str, Set[str]] = {
        t.lower(): {c.lower() for c in cols} for t, cols in schema.items()
    }

    cte_names = _extract_cte_names(stripped)
    tables, aliases = _extract_tables_and_aliases(stripped)
    derived_aliases = _extract_derived_table_aliases(stripped)

    # CTE names + derived-table aliases get added to the virtual schema
    # with an open column set (any column lookup against them is allowed
    # — parsing CTE/subquery bodies is out of scope). Both are treated
    # as "open-column-set tables" downstream.
    for cte in cte_names:
        schema_lc.setdefault(cte.lower(), set())
    for alias in derived_aliases:
        schema_lc.setdefault(alias.lower(), set())
        # Register as a table so qualified ``alias.col`` references
        # don't trigger "unknown table/alias" errors.
        if alias.lower() not in tables:
            tables.append(alias.lower())
        # Treat CTE column-set as a wildcard sentinel
        # (empty set + "is CTE" → skip column checks for it).

    missing_table = _find_missing_table(tables, schema_lc, cte_names)
    if missing_table is not None:
        return _err(
            f"Table '{missing_table}' does not exist. "
            f"Available tables: {sorted(t for t in schema_lc if t not in cte_names)}",
            "missing_table",
            details={
                "table": missing_table,
                "available_tables": sorted(t for t in schema_lc if t not in cte_names),
            },
        )

    # ---- 3. missing_column ----------------------------------------------
    # Build a (alias-or-table) → table map for qualified-column lookup.
    # Aliases live in ``aliases``; the bare table-name is also valid as a
    # qualifier (``opportunities.id`` works even if you said
    # ``opportunities o``).
    qualifier_to_table: Dict[str, str] = {}
    for alias, table in aliases.items():
        qualifier_to_table[alias.lower()] = table.lower()
    for table in tables:
        qualifier_to_table.setdefault(table.lower(), table.lower())

    qualified_err = _check_qualified_columns(
        stripped, qualifier_to_table, schema_lc, cte_names
    )
    if qualified_err is not None:
        return qualified_err

    # Unqualified columns: every bareword in select/where/group/order/etc
    # that isn't a keyword, function call, alias, table name, or literal
    # must exist in at least one referenced table's column set.
    select_aliases = _extract_select_aliases(stripped)
    unqualified_err = _check_unqualified_columns(
        stripped, tables, aliases, schema_lc, cte_names, select_aliases
    )
    if unqualified_err is not None:
        return unqualified_err

    return {"ok": True}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _err(message: str, code: str, details: dict) -> dict:
    """Build a validator error dict."""
    return {"ok": False, "error": message, "code": code, "details": details}


def _strip_noise(sql: str) -> str:
    """Strip comments, string literals, dollar-quoted strings, function
    bodies that contain syntactic ``FROM``, and cast targets.

    The output is still recognizable SQL — only the contents of string-y
    constructs (and the bodies of EXTRACT/OVERLAY/TRIM/etc.) are blanked
    out. Length is preserved (replace-with-space) so byte offsets in
    error messages line up.
    """
    out = sql
    out = _BLOCK_COMMENT_RE.sub(lambda m: " " * len(m.group(0)), out)
    out = _LINE_COMMENT_RE.sub(lambda m: " " * len(m.group(0)), out)
    out = _DOLLAR_QUOTED_RE.sub(lambda m: " " * len(m.group(0)), out)
    out = _STRING_LITERAL_RE.sub(lambda m: " " * len(m.group(0)), out)
    out = _CAST_TARGET_RE.sub(lambda m: " " * len(m.group(0)), out)
    out = _CAST_AS_TARGET_RE.sub(lambda m: " " * len(m.group(0)), out)
    out = _neutralize_from_inside_funcs(out)
    return out


def _neutralize_from_inside_funcs(sql: str) -> str:
    """Blank the internal ``FROM`` / ``FOR`` / ``IN`` keywords used by
    EXTRACT / OVERLAY / SUBSTRING / TRIM / POSITION as argument
    delimiters.

    Without this, the FROM/JOIN regex matches ``FROM created_date`` in
    ``EXTRACT(YEAR FROM created_date)`` and registers ``created_date`` as
    a table. Codex P2 review on PR #178.

    We only blank the keyword (length-preserved), NOT the column
    identifier — so the bareword scan still sees ``created_date`` and
    validates it against the referenced tables. Codex round 3 follow-up:
    fully-blanked function bodies were producing false negatives like
    ``EXTRACT(YEAR FROM bogus_col) FROM opportunities``.
    """
    out = list(sql)
    lower = sql.lower()
    n = len(sql)
    # Keywords that act as delimiters inside the targeted functions.
    # FROM is the primary culprit; OVERLAY uses PLACING/FROM/FOR;
    # POSITION uses IN. Blanking them keeps the surrounding columns
    # visible for validation.
    internal_kw_re = re.compile(
        r"\b(FROM|FOR|IN|PLACING|BOTH|LEADING|TRAILING)\b", re.IGNORECASE
    )
    for func in _FROM_INSIDE_FUNCS:
        flen = len(func)
        i = 0
        while True:
            idx = lower.find(func, i)
            if idx == -1:
                break
            # Anchor: must be word-start.
            if idx > 0 and (sql[idx - 1].isalnum() or sql[idx - 1] == "_"):
                i = idx + flen
                continue
            # Skip whitespace until ``(``.
            j = idx + flen
            while j < n and sql[j].isspace():
                j += 1
            if j >= n or sql[j] != "(":
                i = idx + flen
                continue
            # Walk paren depth from ``(``.
            depth = 1
            k = j + 1
            while k < n and depth > 0:
                c = sql[k]
                if c == "(":
                    depth += 1
                elif c == ")":
                    depth -= 1
                k += 1
            # Blank only the internal delimiter keywords (FROM, FOR, IN,
            # PLACING, etc.) — leave columns and literals visible to the
            # downstream scans.
            body_start, body_end = j + 1, k - 1
            body = sql[body_start:body_end]
            for m in internal_kw_re.finditer(body):
                for p in range(m.start(), m.end()):
                    out[body_start + p] = " "
            i = k
    return "".join(out)


def _first_keyword(sql: str) -> Optional[str]:
    """Return the first SQL keyword (lower-case) ignoring leading whitespace
    and parentheses. ``(SELECT ...)`` is treated as SELECT.
    """
    # Skip leading whitespace and open-parens (a parenthesized SELECT/WITH
    # is fine — common in CTE or set-operation contexts).
    i = 0
    n = len(sql)
    while i < n and (sql[i].isspace() or sql[i] == "("):
        i += 1
    m = re.match(_IDENT, sql[i:])
    if not m:
        return None
    return m.group(0).lower()


def _extract_cte_names(sql: str) -> Set[str]:
    """Return the set of CTE names declared by a ``WITH cte AS (...)`` clause.

    Catches: ``WITH a AS (...) SELECT ...``,
    ``WITH RECURSIVE a AS (...), b AS (...) SELECT ...``.
    """
    # Quick gate: no WITH at the front, no CTEs.
    if _first_keyword(sql) != "with":
        return set()

    names: Set[str] = set()
    # ``<name>`` followed by optional column list, then ``AS``, then ``(``.
    # We don't try to balance parens — we just collect names. The CTE body
    # itself gets validated via the recursive walk over the whole stripped
    # query (its FROM/JOIN identifiers will be picked up alongside the
    # outer query's).
    pattern = re.compile(
        rf"\b({_IDENT})\s*(?:\([^)]*\))?\s+AS\s*\(",
        re.IGNORECASE,
    )
    # Limit search to the WITH prefix — once we hit the outer SELECT, stop.
    # Easiest heuristic: stop at the first top-level SELECT keyword that's
    # NOT inside parens. We don't track parens here; instead, collect all
    # ``name AS (`` patterns and trust the regex to be conservative enough.
    for m in pattern.finditer(sql):
        name = m.group(1).lower()
        if name in _SQL_KEYWORDS:
            continue
        names.add(name)
    return names


def _extract_tables_and_aliases(
    sql: str,
) -> Tuple[List[str], Dict[str, str]]:
    """Extract every table referenced in FROM / JOIN, plus their aliases.

    Handles:
      - ``FROM opportunities``
      - ``FROM opportunities o``
      - ``FROM opportunities AS o``
      - ``FROM public.opportunities``  (schema-qualified → table = "opportunities")
      - Chained joins: ``FROM a JOIN b ON ... JOIN c ON ...``
      - Comma joins: ``FROM a, b WHERE a.x = b.y`` — Codex P2 round 4.

    Does NOT handle:
      - Subqueries in FROM: ``FROM (SELECT ...) sub`` — we ignore the
        derived-table alias for column lookup and let the inner SELECT's
        identifiers be checked at the top level.
      - Lateral joins.

    Returns:
        (tables, aliases) where ``tables`` is the list of distinct table
        names (lower-case) and ``aliases`` maps alias → table.
    """
    tables: List[str] = []
    aliases: Dict[str, str] = {}

    # Pattern: FROM/JOIN, then either
    #   (a) an identifier optionally schema-qualified, optionally with an
    #       alias, OR
    #   (b) an open-paren (subquery — we skip).
    # The alias is optional and may use ``AS``. The alias slot uses a
    # negative lookahead to forbid SQL continuation keywords
    # (JOIN/WHERE/ON/etc.), otherwise ``FROM opportunities JOIN accounts``
    # would consume ``JOIN`` as the alias of ``opportunities`` and
    # ``finditer`` would then start scanning past ``JOIN``, missing the
    # ``accounts`` table entirely. Codex P2 review on PR #178.
    table_ref_re = re.compile(
        rf"""
        \b(FROM|JOIN)\s+
        (?!\()                      # skip ``FROM (`` — subquery
        (?:({_IDENT})\.)?           # optional schema qualifier
        ({_IDENT})                  # the table name
        (?:                         # optional alias
            \s+
            (?:AS\s+)?
            (?!{_ALIAS_FORBIDDEN_RE}\b)
            ({_IDENT})
        )?
        """,
        re.IGNORECASE | re.VERBOSE,
    )

    seen: Set[str] = set()
    for m in table_ref_re.finditer(sql):
        _kind, _schema, table_name, alias = m.groups()
        table_lc = table_name.lower()
        # Filter out keywords that can appear in a JOIN ... ON ... position
        # (``LEFT JOIN`` already consumed by JOIN; ``ON`` only follows
        # JOIN). We don't have a JOIN keyword false-positive here because
        # FROM/JOIN are anchored.
        if table_lc in _SQL_KEYWORDS:
            continue
        if table_lc not in seen:
            tables.append(table_lc)
            seen.add(table_lc)
        if alias:
            alias_lc = alias.lower()
            # Skip alias if it's actually a continuation keyword
            # (``FROM opportunities WHERE`` would match ``WHERE`` as alias
            # without this guard).
            if alias_lc in _SQL_KEYWORDS:
                continue
            aliases[alias_lc] = table_lc
        # Comma-join continuation. After matching ``FROM opportunities o``
        # walk forward looking for ``, accounts a`` patterns. Stops at
        # the first non-comma SQL keyword. Codex P2 review #4 on PR #178.
        _consume_comma_joins(
            sql,
            start=m.end(),
            tables=tables,
            aliases=aliases,
            seen=seen,
        )

    return tables, aliases


def _consume_comma_joins(
    sql: str,
    start: int,
    tables: List[str],
    aliases: Dict[str, str],
    seen: Set[str],
) -> None:
    """Extract additional comma-joined table refs after a FROM clause.

    Walks forward from ``start`` looking for ``, <ident> [<alias>]``
    patterns. Stops on the first non-comma SQL keyword (WHERE, GROUP,
    ON, etc.) so we don't accidentally chew into the WHERE clause.

    Mutates ``tables`` / ``aliases`` / ``seen`` in place.
    """
    n = len(sql)
    i = start
    comma_ref_re = re.compile(
        rf"""
        \s*,\s*
        (?!\()                      # comma-subquery is rare; skip
        (?:({_IDENT})\.)?           # optional schema qualifier
        ({_IDENT})                  # the table name
        (?:                         # optional alias
            \s+
            (?:AS\s+)?
            (?!{_ALIAS_FORBIDDEN_RE}\b)
            ({_IDENT})
        )?
        """,
        re.IGNORECASE | re.VERBOSE,
    )
    while i < n:
        # Skip whitespace.
        while i < n and sql[i].isspace():
            i += 1
        if i >= n or sql[i] != ",":
            return
        m = comma_ref_re.match(sql, i)
        if not m:
            return
        _schema, table_name, alias = m.groups()
        table_lc = table_name.lower()
        if table_lc in _SQL_KEYWORDS:
            return
        if table_lc not in seen:
            tables.append(table_lc)
            seen.add(table_lc)
        if alias:
            alias_lc = alias.lower()
            if alias_lc not in _SQL_KEYWORDS:
                aliases[alias_lc] = table_lc
        i = m.end()


def _extract_derived_table_aliases(sql: str) -> Set[str]:
    """Return aliases for ``FROM (...) alias`` / ``JOIN (...) alias`` subqueries.

    The subquery body itself is part of the SQL stream — its FROM/JOIN
    identifiers get extracted by ``_extract_tables_and_aliases`` as if
    they were top-level. The only thing this function adds is the
    alias-after-close-paren, so a qualified ``t.col`` reference doesn't
    flag as a bogus qualifier.

    Caveat: we don't track paren-balance. A LATERAL subquery whose alias
    sits an arbitrary distance after its close-paren may be missed. The
    common case is ``FROM (SELECT ...) alias`` immediately after the
    close, which this regex catches.
    """
    aliases: Set[str] = set()
    pattern = re.compile(
        r"""
        \b(?:FROM|JOIN)\s+
        \(                              # open-paren of subquery
        """,
        re.IGNORECASE | re.VERBOSE,
    )
    # Find each (FROM|JOIN) ( and walk forward to find the matching )
    # then read the next identifier as the alias.
    for m in pattern.finditer(sql):
        i = m.end()  # position right after ``(``
        depth = 1
        n = len(sql)
        while i < n and depth > 0:
            c = sql[i]
            if c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
            i += 1
        # i is now positioned right after the matching ``)``.
        # Skip whitespace and optional ``AS``.
        while i < n and sql[i].isspace():
            i += 1
        # Optional ``AS``
        if sql[i : i + 3].lower() == "as ":
            i += 3
            while i < n and sql[i].isspace():
                i += 1
        am = re.match(_IDENT, sql[i:])
        if not am:
            continue
        alias = am.group(0)
        if alias.lower() in _SQL_KEYWORDS:
            continue
        aliases.add(alias.lower())
    return aliases


def _find_missing_table(
    tables: Iterable[str],
    schema_lc: Dict[str, Set[str]],
    cte_names: Set[str],
) -> Optional[str]:
    """Return the first table that's not in ``schema_lc`` and not a CTE."""
    cte_lc = {c.lower() for c in cte_names}
    for t in tables:
        if t in cte_lc:
            continue
        if t not in schema_lc:
            return t
    return None


def _extract_select_aliases(sql: str) -> Set[str]:
    """Return the set of column aliases introduced by ``AS <ident>``.

    The Codex P1 review on PR #178 caught the case where
    ``SELECT COUNT(*) AS total FROM opportunities`` was rejected because
    ``total`` ran through the unqualified-column scan and failed to match
    any column in ``opportunities``. Aliases are *outputs* of the
    select-list, not column references — we need to skip them.

    Conservative implementation:
      - Only matches the explicit ``AS <ident>`` form. Bare-word aliases
        (``SELECT amount total FROM ...``) are too ambiguous to detect
        reliably with regex (the third token could be a real column being
        projected, or it could be a typo).
      - The match is global across the whole SQL. ``AS`` is also valid
        in derived-table-alias contexts (``FROM (...) AS sub``); those
        aliases are also "outputs" that shouldn't be flagged as columns,
        so collecting them here doesn't hurt.
    """
    aliases: Set[str] = set()
    pattern = re.compile(rf"\bAS\s+({_IDENT})\b", re.IGNORECASE)
    for m in pattern.finditer(sql):
        name = m.group(1).lower()
        if name in _SQL_KEYWORDS:
            continue
        aliases.add(name)
    return aliases


def _check_qualified_columns(
    sql: str,
    qualifier_to_table: Dict[str, str],
    schema_lc: Dict[str, Set[str]],
    cte_names: Set[str],
) -> Optional[dict]:
    """Return an error dict if any ``<qualifier>.<column>`` reference is
    bogus, else None.

    A qualifier is bogus if:
      - it isn't a known alias or table name AND isn't a CTE name
      - OR it IS a known alias/table but the column doesn't exist on that
        table
    """
    cte_lc = {c.lower() for c in cte_names}
    for m in _QUALIFIED_COL_RE.finditer(sql):
        qualifier, column = m.group(1).lower(), m.group(2).lower()
        # ``*`` from ``t.*`` is consumed by the column group above —
        # actually no, ``*`` doesn't match _IDENT. Good. We naturally skip it.
        if column == "*":
            continue
        # Skip schema.table patterns where the "qualifier" is actually a
        # schema name and "column" is a table. The qualifier won't be in
        # ``qualifier_to_table``, but we should not flag it as a missing
        # column — that's a missing-table problem (already handled above).
        # Heuristic: if "column" matches a known table name and qualifier
        # is NOT in qualifier_to_table, assume schema-qualified table.
        if (
            qualifier not in qualifier_to_table
            and qualifier not in cte_lc
            and column in schema_lc
        ):
            continue
        # CTE qualifier — accept any column (we don't parse CTE bodies).
        if qualifier in cte_lc:
            continue
        # Skip function-style: ``EXTRACT(YEAR FROM ...)`` is handled by
        # the regex lookahead, but ``date.column``-style false positives
        # are rare. If the qualifier looks like a SQL keyword (e.g. someone
        # wrote ``date.created_at`` accidentally), report it.
        if qualifier in _SQL_KEYWORDS:
            continue
        if qualifier not in qualifier_to_table:
            # Bogus qualifier — neither a table nor a known alias.
            return _err(
                f"Unknown table/alias '{qualifier}' in '{qualifier}.{column}'. "
                f"Available: {sorted(qualifier_to_table.keys())}",
                "missing_table",
                details={
                    "qualifier": qualifier,
                    "column": column,
                    "available_qualifiers": sorted(qualifier_to_table.keys()),
                },
            )
        table = qualifier_to_table[qualifier]
        cols = schema_lc.get(table, set())
        if not cols:
            # Table has no columns recorded (e.g. CTE-shaped or absent
            # from schema). Skip column check.
            continue
        if column not in cols:
            return _err(
                f"Column '{column}' does not exist on table '{table}'. "
                f"Available columns: {sorted(cols)}",
                "missing_column",
                details={
                    "table": table,
                    "column": column,
                    "available_columns": sorted(cols),
                },
            )
    return None


def _check_unqualified_columns(
    sql: str,
    tables: List[str],
    aliases: Dict[str, str],
    schema_lc: Dict[str, Set[str]],
    cte_names: Set[str],
    select_aliases: Set[str],
) -> Optional[dict]:
    """Return an error dict if an unqualified bareword references no
    column in any referenced table, else None.

    Strategy: enumerate every bareword identifier in the (noise-stripped)
    query, filter out keywords/functions/aliases/table names/numeric
    literals, then for each remaining word check that at least one
    referenced table's column set contains it.

    CTE handling: a CTE's column set is an unknown — we'd need to parse
    its body to know what columns it produces. So we treat CTEs as
    open-column-set: any bareword that isn't found in a real referenced
    table is forgiven IF the query references any CTE. Real-table
    columns are still validated. The user's worked example proves this
    is the right call: ``WITH big_opps AS (...) SELECT id FROM big_opps``
    must validate the CTE BODY's identifiers against ``opportunities``
    (a real table) while accepting ``id`` in the outer SELECT as
    possibly a CTE output.
    """
    # If there are no tables at all, there's nothing to check (and the
    # SQL is probably degenerate anyway — would have hit missing_table).
    if not tables:
        return None

    cte_lc = {c.lower() for c in cte_names}

    # Split tables into "real" (column set known) and CTE/unknown.
    # We validate against the real ones; CTE/unknown contribute an
    # open-column-set escape hatch.
    real_tables: List[str] = []
    has_open_set = False
    for t in tables:
        if t in cte_lc:
            has_open_set = True
            continue
        cols = schema_lc.get(t)
        if not cols:
            # Unknown table or table with no recorded columns. Treat as
            # open column set — don't false-positive.
            has_open_set = True
            continue
        real_tables.append(t)

    # If there are no real tables to validate against, we have nothing
    # to compare unqualified columns to. Skip.
    if not real_tables:
        return None

    # Union of all columns from real referenced tables.
    all_cols: Set[str] = set()
    for t in real_tables:
        all_cols.update(schema_lc.get(t, set()))

    # Skip identifiers used as table aliases or table names — those are
    # being USED as qualifiers, not referenced as columns.
    aliases_lc = set(aliases.keys())
    tables_lc = set(tables)

    # Find all qualified references and exclude both sides (qualifier
    # already counted in aliases/tables; column already validated above).
    qualified_spans: List[Tuple[int, int]] = [
        (m.start(), m.end()) for m in _QUALIFIED_COL_RE.finditer(sql)
    ]

    def _in_qualified(pos: int) -> bool:
        for start, end in qualified_spans:
            if start <= pos < end:
                return True
        return False

    # Walk barewords.
    for m in _BARE_IDENT_RE.finditer(sql):
        word = m.group(0)
        lower = word.lower()
        if _in_qualified(m.start()):
            continue
        if lower in _SQL_KEYWORDS:
            continue
        if lower in aliases_lc or lower in tables_lc:
            continue
        if lower in cte_lc:
            continue
        # Select-list output aliases (e.g. ``AS total`` in
        # ``SELECT COUNT(*) AS total FROM opps``) are projections, not
        # column references. They legitimately don't exist as schema
        # columns. Codex P1 review on PR #178.
        if lower in select_aliases:
            continue
        # Numeric literals don't match _IDENT, so we're safe there.
        # Skip if the next non-whitespace char is ``(`` — that's a
        # function call. Codex P3 review on PR #178: ``COUNT (*)`` with
        # a space before the paren was being flagged as a missing
        # column. Walk past intervening whitespace before checking.
        end = m.end()
        while end < len(sql) and sql[end].isspace():
            end += 1
        if end < len(sql) and sql[end] == "(":
            continue
        if lower in all_cols:
            continue
        # Look for ownership on OTHER tables in the schema — used as a
        # diagnostic hint in the error path below.
        owners = sorted(t for t, cols in schema_lc.items() if lower in cols)
        # When a CTE or derived table is in scope, the bareword may be
        # a CTE output column. CTE bodies can introduce arbitrary
        # column names via ``WITH c(foo) AS (...)`` or aliasing inside
        # the body — we don't parse those, so we forgive the bareword.
        # Codex P2 review #3 on PR #178: ``WITH c(foo) AS (...) SELECT
        # foo FROM c`` was being rejected because ``foo`` exists on no
        # real table.
        if has_open_set:
            continue
        if owners:
            hint = f" Found on: {owners}. Did you forget to join?"
        else:
            hint = ""
        return _err(
            f"Column '{word}' not found on any referenced table "
            f"({sorted(tables)}).{hint}",
            "missing_column",
            details={
                "column": lower,
                "referenced_tables": sorted(tables),
                "found_on_tables": owners,
            },
        )

    return None
