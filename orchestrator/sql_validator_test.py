"""Tests for orchestrator/sql_validator.py.

The validator is opportunistic (regex-based) — it catches the common
column-naming typos that prompt-only rules can't reliably prevent, while
passing through more exotic SQL constructs rather than false-positive.
These tests cover the 80% case the PR description targets, plus a few
defensive edge cases (string literals, comments, CTEs).

Run:
    cd orchestrator && python3 -m pytest sql_validator_test.py -v
"""

from __future__ import annotations

from sql_validator import validate_sql


# A small synthetic schema mirroring the live snapshot shape (just enough
# to exercise the joins + qualified/unqualified paths).
SCHEMA = {
    "opportunities": {
        "id",
        "name",
        "account_id",
        "stage_name",
        "amount",
        "created_date",
        "close_date",
        "owner_id",
    },
    "accounts": {
        "id",
        "name",
        "industry",
        "billing_country",
        "created_date",
    },
    "leads": {
        "id",
        "email",
        "company",
        "status",
        "created_date",
        "discovery_call_booked__c",
    },
}


# ---------------------------------------------------------------------------
# Pass-through paths
# ---------------------------------------------------------------------------


def test_empty_schema_passes_through_for_valid_select():
    """A DB-unavailable environment should not block a valid SELECT.

    The empty-schema branch lets the validator degrade gracefully when
    the snapshot hasn't been built. Column/table existence checks are
    skipped, but the read-only guard remains in force regardless of
    schema availability. Codex P1 round 5 review on PR #178.
    """
    result = validate_sql("SELECT id FROM opportunities", schema={})
    assert result == {"ok": True}


def test_empty_schema_still_rejects_non_select():
    """The non_select check runs even with no schema — read-only is
    not contingent on schema availability.
    """
    result = validate_sql("DELETE FROM opportunities", schema={})
    assert result["ok"] is False
    assert result["code"] == "non_select"


def test_empty_schema_still_rejects_mutating_cte():
    """A mutating CTE must be rejected even when the schema is empty."""
    sql = "WITH d AS (DELETE FROM leads RETURNING id) SELECT id FROM d"
    result = validate_sql(sql, schema={})
    assert result["ok"] is False
    assert result["code"] == "non_select"


def test_clean_select_passes():
    sql = "SELECT id, name, stage_name FROM opportunities WHERE amount > 1000"
    assert validate_sql(sql, SCHEMA) == {"ok": True}


def test_qualified_join_passes():
    sql = """
    SELECT o.id, o.name, a.industry
    FROM opportunities o
    JOIN accounts a ON o.account_id = a.id
    WHERE a.billing_country = 'US'
    """
    assert validate_sql(sql, SCHEMA) == {"ok": True}


def test_table_name_used_as_qualifier_passes():
    """Bare table name (no alias) must also work as a qualifier."""
    sql = "SELECT opportunities.id, opportunities.name FROM opportunities"
    assert validate_sql(sql, SCHEMA) == {"ok": True}


def test_string_literal_with_column_name_passes():
    """A column name appearing inside a string literal must not break
    validation. Stripping string literals before identifier extraction
    is the defense — without it, ``'discovery_call_booked'`` would get
    picked up as a column candidate.
    """
    sql = "SELECT id FROM leads WHERE status = 'not_a_real_column_name'"
    assert validate_sql(sql, SCHEMA) == {"ok": True}


def test_count_star_passes():
    """COUNT(*) and other aggregate functions must not flag."""
    sql = "SELECT COUNT(*) FROM opportunities WHERE amount > 0"
    assert validate_sql(sql, SCHEMA) == {"ok": True}


def test_unqualified_unique_column_passes():
    """An unqualified column that exists on a single referenced table
    should pass — the agent often writes ``SELECT name FROM accounts``
    without bothering to qualify.
    """
    sql = "SELECT name, industry FROM accounts"
    assert validate_sql(sql, SCHEMA) == {"ok": True}


def test_select_star_passes():
    sql = "SELECT * FROM opportunities"
    assert validate_sql(sql, SCHEMA) == {"ok": True}


def test_table_star_passes():
    sql = "SELECT o.* FROM opportunities o"
    assert validate_sql(sql, SCHEMA) == {"ok": True}


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


def test_non_select_rejected():
    sql = "DELETE FROM opportunities WHERE id = 'foo'"
    result = validate_sql(sql, SCHEMA)
    assert result["ok"] is False
    assert result["code"] == "non_select"
    assert "delete" in result["error"].lower()


def test_insert_rejected():
    sql = "INSERT INTO leads (email) VALUES ('x@y.com')"
    result = validate_sql(sql, SCHEMA)
    assert result["ok"] is False
    assert result["code"] == "non_select"


def test_update_rejected():
    sql = "UPDATE opportunities SET amount = 0"
    result = validate_sql(sql, SCHEMA)
    assert result["ok"] is False
    assert result["code"] == "non_select"


def test_empty_sql_rejected():
    result = validate_sql("   \n  ", SCHEMA)
    assert result["ok"] is False
    assert result["code"] == "non_select"


def test_missing_table_caught():
    sql = "SELECT id FROM nonexistent_table"
    result = validate_sql(sql, SCHEMA)
    assert result["ok"] is False
    assert result["code"] == "missing_table"
    assert result["details"]["table"] == "nonexistent_table"
    # Available tables list should be useful for self-correction.
    assert "opportunities" in result["details"]["available_tables"]


def test_missing_column_qualified():
    """The PR #170 failure mode: opportunities.account_id was missing on
    one snapshot. Here we simulate the inverse — a column that's actually
    NOT in the schema.
    """
    sql = "SELECT o.bogus_column FROM opportunities o"
    result = validate_sql(sql, SCHEMA)
    assert result["ok"] is False
    assert result["code"] == "missing_column"
    assert result["details"]["table"] == "opportunities"
    assert result["details"]["column"] == "bogus_column"
    # The available-columns list is the actionable feedback.
    assert "account_id" in result["details"]["available_columns"]


def test_missing_column_unqualified():
    sql = "SELECT bogus_column FROM opportunities"
    result = validate_sql(sql, SCHEMA)
    assert result["ok"] is False
    assert result["code"] == "missing_column"
    assert result["details"]["column"] == "bogus_column"


def test_unqualified_ambiguous_column_handled_gracefully():
    """``id`` exists on both opportunities and accounts. An unqualified
    reference to it across a join should pass (the validator doesn't try
    to be a query planner — that's Postgres's job once the SQL is valid).
    """
    sql = """
    SELECT id, name
    FROM opportunities o
    JOIN accounts a ON o.account_id = a.id
    """
    # ``name`` is also ambiguous (on both tables). Validator should pass
    # — both names exist somewhere in the referenced tables.
    assert validate_sql(sql, SCHEMA) == {"ok": True}


def test_unknown_qualifier_caught():
    """``x.id`` where ``x`` is neither a table nor a declared alias —
    classic typo / forgotten join. Should be reported as missing_table.
    """
    sql = "SELECT x.id FROM opportunities o"
    result = validate_sql(sql, SCHEMA)
    assert result["ok"] is False
    assert result["code"] == "missing_table"
    assert result["details"]["qualifier"] == "x"


def test_column_exists_on_other_table_hints_join():
    """A bareword that exists on an *unreferenced* table should be
    reported with a hint pointing at where it lives — actionable
    self-correction signal.
    """
    sql = "SELECT industry FROM opportunities"
    result = validate_sql(sql, SCHEMA)
    assert result["ok"] is False
    assert result["code"] == "missing_column"
    # The hint must name accounts as the actual home of ``industry``.
    assert "accounts" in result["details"]["found_on_tables"]
    assert "join" in result["error"].lower()


# ---------------------------------------------------------------------------
# Defensive / edge cases
# ---------------------------------------------------------------------------


def test_comments_stripped():
    """SQL line comments must not introduce false identifier matches."""
    sql = """
    -- this comment mentions bogus_column and nonexistent_table
    SELECT id FROM opportunities
    """
    assert validate_sql(sql, SCHEMA) == {"ok": True}


def test_block_comments_stripped():
    sql = "SELECT id /* bogus_column */ FROM opportunities"
    assert validate_sql(sql, SCHEMA) == {"ok": True}


def test_schema_qualified_table_accepted():
    """``public.opportunities`` is a real Postgres pattern; the validator
    should treat it as ``opportunities`` (we don't track schemas).
    """
    sql = "SELECT id FROM public.opportunities"
    # Currently the validator pulls the qualifier as a schema and uses
    # the second piece as the table name — that works for unqualified
    # column refs.
    assert validate_sql(sql, SCHEMA) == {"ok": True}


def test_with_cte_passes():
    """CTE references should be tolerated. Column lookup inside the CTE
    body still runs against real tables; references to the CTE name
    itself are accepted with an open column set.
    """
    sql = """
    WITH big_opps AS (
        SELECT id, name, amount FROM opportunities WHERE amount > 100000
    )
    SELECT id, name FROM big_opps
    """
    assert validate_sql(sql, SCHEMA) == {"ok": True}


def test_with_cte_invalid_inner_column_passes_by_design():
    """When a CTE is in scope, unqualified barewords are forgiven.

    A bareword inside the CTE body that doesn't exist on the real
    table (``bogus_inner`` on ``opportunities``) and a legitimate CTE
    output column declared via ``WITH c(foo) AS ...`` look identical
    to a regex-based scanner. Codex round 3 review on PR #178 flagged
    that rejecting the second pattern is worse than accepting the
    first: false positives on valid SQL block the agent entirely,
    while a CTE-body typo still surfaces via the eventual Postgres
    error.

    This test pins the chosen tradeoff: with a CTE in scope, the
    typed-inside-the-body case passes through. Qualified-column
    typos (``opportunities.bogus``) are still caught regardless of
    CTE scope — see ``test_missing_column_qualified``.
    """
    sql = """
    WITH big_opps AS (
        SELECT id, name, bogus_inner FROM opportunities WHERE amount > 100000
    )
    SELECT id FROM big_opps
    """
    assert validate_sql(sql, SCHEMA) == {"ok": True}


def test_qualified_column_typo_still_caught_inside_cte():
    """Qualified typos remain caught even with a CTE in scope —
    ``opportunities.bogus`` is unambiguous regardless of whether a CTE
    might pass through that name.
    """
    sql = """
    WITH big_opps AS (
        SELECT id FROM opportunities WHERE opportunities.bogus > 0
    )
    SELECT id FROM big_opps
    """
    result = validate_sql(sql, SCHEMA)
    assert result["ok"] is False
    assert result["code"] == "missing_column"
    assert result["details"]["table"] == "opportunities"
    assert result["details"]["column"] == "bogus"


def test_case_insensitive_table_match():
    """Postgres folds unquoted identifiers to lower-case; the validator
    must too.
    """
    sql = "SELECT ID, NAME FROM OPPORTUNITIES WHERE AMOUNT > 0"
    assert validate_sql(sql, SCHEMA) == {"ok": True}


def test_derived_table_alias_accepted():
    """``FROM (SELECT ...) alias`` is a common pattern. The alias must
    be registered so qualified references against it (``alias.col``)
    don't flag as a bogus qualifier.
    """
    sql = """
    SELECT t.id, t.amount
    FROM (SELECT id, amount FROM opportunities WHERE amount > 100) t
    """
    assert validate_sql(sql, SCHEMA) == {"ok": True}


def test_in_subquery_passes():
    """``WHERE x IN (SELECT ...)`` should pass — the inner SELECT's
    identifiers are validated alongside the outer query's tables.
    """
    sql = (
        "SELECT id FROM opportunities "
        "WHERE account_id IN (SELECT id FROM accounts WHERE industry = 'SaaS')"
    )
    assert validate_sql(sql, SCHEMA) == {"ok": True}


# ---------------------------------------------------------------------------
# Codex review regressions on PR #178
# ---------------------------------------------------------------------------


def test_select_alias_as_form_passes():
    """``SELECT COUNT(*) AS total FROM opps`` must pass.

    Codex P1 finding: the unqualified-column scan reached ``total`` and
    flagged it as missing because select-list aliases weren't being
    collected and skipped. Aliases are projections (outputs), not column
    references.
    """
    sql = "SELECT COUNT(*) AS total FROM opportunities"
    assert validate_sql(sql, SCHEMA) == {"ok": True}


def test_sum_alias_passes():
    sql = "SELECT SUM(amount) AS total_amount FROM opportunities"
    assert validate_sql(sql, SCHEMA) == {"ok": True}


def test_unaliased_join_passes():
    """``FROM opportunities JOIN accounts ON ...`` (no aliases) must pass.

    Codex P2 finding: the optional-alias group in the table-extraction
    regex greedily consumed the ``JOIN`` keyword as the alias of the
    first table, then ``finditer`` advanced past it and never registered
    the second table.
    """
    sql = (
        "SELECT industry FROM opportunities "
        "JOIN accounts ON opportunities.account_id = accounts.id"
    )
    assert validate_sql(sql, SCHEMA) == {"ok": True}


def test_unaliased_left_join_passes():
    sql = (
        "SELECT industry FROM opportunities "
        "LEFT JOIN accounts ON opportunities.account_id = accounts.id"
    )
    assert validate_sql(sql, SCHEMA) == {"ok": True}


def test_unaliased_join_with_where_clause():
    """Codex P2 sibling case: the keyword-after-table guard must also
    forbid WHERE/GROUP/ORDER/HAVING from being absorbed as aliases.
    """
    sql = "SELECT id FROM opportunities WHERE amount > 100"
    assert validate_sql(sql, SCHEMA) == {"ok": True}


def test_extract_year_from_column_passes():
    """``EXTRACT(YEAR FROM created_date)`` must pass.

    Codex P2 round 2: the ``FROM`` inside EXTRACT was being matched by
    the FROM/JOIN regex and ``created_date`` registered as a table.
    Function bodies that use ``FROM`` as an internal delimiter (EXTRACT,
    OVERLAY, SUBSTRING, TRIM, POSITION) must be neutralized before the
    table-extraction pass.
    """
    sql = "SELECT EXTRACT(YEAR FROM created_date) AS yr FROM opportunities"
    assert validate_sql(sql, SCHEMA) == {"ok": True}


def test_extract_quarter_passes():
    sql = "SELECT EXTRACT(QUARTER FROM close_date) FROM opportunities"
    assert validate_sql(sql, SCHEMA) == {"ok": True}


def test_substring_from_for_passes():
    sql = "SELECT SUBSTRING(name FROM 1 FOR 3) FROM accounts"
    assert validate_sql(sql, SCHEMA) == {"ok": True}


def test_cast_to_float_passes():
    """``SUM(amount)::float AS total`` must pass.

    Codex P2 round 2: the ``::float`` cast target was being scanned by
    the bareword pass and flagged as a missing column. Cast targets
    (``::numeric``, ``::float``, ``::text``, etc.) need to be stripped.
    """
    sql = "SELECT SUM(amount)::float AS total FROM opportunities"
    assert validate_sql(sql, SCHEMA) == {"ok": True}


def test_cast_to_numeric_with_precision_passes():
    sql = "SELECT amount::numeric(10,2) FROM opportunities"
    assert validate_sql(sql, SCHEMA) == {"ok": True}


def test_cast_to_text_passes():
    sql = "SELECT id::text FROM opportunities"
    assert validate_sql(sql, SCHEMA) == {"ok": True}


def test_combined_extract_and_cast_passes():
    """The two fixes compose without interfering with each other."""
    sql = (
        "SELECT EXTRACT(YEAR FROM created_date) AS yr, "
        "SUM(amount)::float AS total "
        "FROM opportunities "
        "GROUP BY EXTRACT(YEAR FROM created_date)"
    )
    assert validate_sql(sql, SCHEMA) == {"ok": True}


def test_extract_with_bogus_column_still_caught():
    """The earlier ``EXTRACT`` fix blanked the entire function body, so
    typos inside (``EXTRACT(YEAR FROM bogus_col)``) were silently passing
    through. The refined fix blanks ONLY the internal delimiter keyword
    (FROM/FOR/IN/PLACING/etc), leaving the column visible for validation.
    Codex round 3 review on PR #178.
    """
    sql = "SELECT EXTRACT(YEAR FROM bogus_col) FROM opportunities"
    result = validate_sql(sql, SCHEMA)
    assert result["ok"] is False
    assert result["code"] == "missing_column"
    assert result["details"]["column"] == "bogus_col"


def test_with_cte_column_list_pattern_passes():
    """``WITH c(foo) AS (...) SELECT foo FROM c`` declares ``foo`` as
    a CTE output via the column-list syntax. We don't parse the list,
    but the open-set CTE scope must forgive the unqualified ``foo``.
    Codex round 3 review on PR #178.
    """
    sql = "WITH c(foo) AS (SELECT id FROM opportunities) SELECT foo FROM c"
    assert validate_sql(sql, SCHEMA) == {"ok": True}


def test_with_cte_aliased_inner_output_passes():
    """A CTE that aliases an inner column should pass when the outer
    SELECT references the alias.
    """
    sql = "WITH c AS (SELECT id AS thing FROM opportunities) SELECT thing FROM c"
    assert validate_sql(sql, SCHEMA) == {"ok": True}


def test_mutating_cte_rejected():
    """Data-modifying CTEs (``WITH d AS (DELETE ...)``) violate the
    db_query read-only contract and must be rejected. Codex P1 round 4
    review on PR #178.
    """
    sql = "WITH d AS (DELETE FROM leads RETURNING id) SELECT id FROM d"
    result = validate_sql(sql, SCHEMA)
    assert result["ok"] is False
    assert result["code"] == "non_select"
    assert "DELETE" in result["error"]


def test_insert_cte_rejected():
    sql = (
        "WITH d AS (INSERT INTO leads (email) VALUES ('x@y.com') RETURNING id) "
        "SELECT id FROM d"
    )
    result = validate_sql(sql, SCHEMA)
    assert result["ok"] is False
    assert result["code"] == "non_select"


def test_update_cte_rejected():
    sql = "WITH d AS (UPDATE leads SET status = 'x' RETURNING id) SELECT id FROM d"
    result = validate_sql(sql, SCHEMA)
    assert result["ok"] is False
    assert result["code"] == "non_select"


def test_comma_join_passes():
    """``FROM a, b WHERE ...`` is valid pre-ANSI join syntax. Codex P2
    round 4 review on PR #178.
    """
    sql = "SELECT a.id FROM opportunities o, accounts a WHERE o.account_id = a.id"
    assert validate_sql(sql, SCHEMA) == {"ok": True}


def test_three_way_comma_join_passes():
    sql = (
        "SELECT a.id FROM opportunities o, accounts a, leads l "
        "WHERE o.account_id = a.id AND l.email = 'x@y.com'"
    )
    assert validate_sql(sql, SCHEMA) == {"ok": True}


def test_function_call_with_space_before_paren_passes():
    """``COUNT (*)`` with whitespace before the paren is valid SQL and
    must not flag as a missing column. Codex P3 round 4 review on PR
    #178.
    """
    sql = "SELECT COUNT (*) FROM opportunities"
    assert validate_sql(sql, SCHEMA) == {"ok": True}


def test_date_trunc_with_space_passes():
    sql = "SELECT date_trunc ('month', created_date) FROM opportunities"
    assert validate_sql(sql, SCHEMA) == {"ok": True}


def test_cast_to_double_precision_passes():
    """Multi-word Postgres cast types must not flag.

    Codex P2 round 5 review on PR #178: ``::double precision`` was
    rejected because the cast-strip regex only consumed the first word
    and the bareword scan then flagged ``precision`` as a missing
    column. Same applies to ``CAST(... AS double precision)``.
    """
    sql = "SELECT amount::double precision FROM opportunities"
    assert validate_sql(sql, SCHEMA) == {"ok": True}


def test_cast_as_double_precision_passes():
    sql = "SELECT CAST(amount AS double precision) FROM opportunities"
    assert validate_sql(sql, SCHEMA) == {"ok": True}


def test_cast_to_timestamp_with_time_zone_passes():
    sql = "SELECT created_date::timestamp with time zone FROM opportunities"
    assert validate_sql(sql, SCHEMA) == {"ok": True}


def test_cast_to_character_varying_passes():
    sql = "SELECT id::character varying FROM opportunities"
    assert validate_sql(sql, SCHEMA) == {"ok": True}


def test_cast_to_array_type_passes():
    """``::int[]`` is a valid Postgres array-type cast."""
    sql = "SELECT id::int[] FROM opportunities"
    assert validate_sql(sql, SCHEMA) == {"ok": True}
