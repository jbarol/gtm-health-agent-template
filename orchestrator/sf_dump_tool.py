"""Server-side Salesforce → Parquet materializer for the ``dump_sf_query`` custom tool.

Why this exists
---------------
Anthropic Managed Agents' ``multiagent`` dispatch is opaque: sub-agent responses
return into the Coordinator's context window. Live test of the 3,209-lead
Discovery_Call_Booked question on commit ``90b9bb5`` showed the Coordinator
hit 966K of the 1M cap on Pipeline Monitor's response alone — Pipeline Monitor
called MCP ``soqlQuery``, raw rows entered its context, and its processed
findings (structured aggregates + table data) bloated the Coordinator.

The fix is architectural: keep raw rows out of every agent's context. This
module is the handler for the ``dump_sf_query`` custom tool. It runs
server-side on Railway, paginates SF via ``simple_salesforce.query_all_iter``,
streams rows to a Parquet file in ``/mnt/session/outputs/``, and returns ONLY
a compact handle ``{file_path, count, schema, summary_stats, preview_3,
summary_text}``. The raw rows never enter any agent's context.

Design rules
------------
1. Streaming writes. ``query_all_iter`` yields one record at a time; we write
   in 1000-row batches via ``pyarrow.parquet.ParquetWriter``. Memory stays
   bounded for arbitrarily large pulls.
2. Never raise. SF auth failure, disk failure, network blip — every error
   path returns ``{file_path: None, error: "...", count: 0}`` so the calling
   agent can react gracefully.
3. Lazy imports. ``session_runner._get_sf_client`` is imported inside
   ``dump_sf_query`` to avoid circular imports (session_runner imports this
   module to dispatch the tool).
4. Schema inference from the first batch. SF returns mostly strings, ints,
   floats, booleans, and ISO datetime strings — we infer types from the first
   1000 rows and lock the schema for the remainder of the write.
5. Filename collisions defended. ``utc_iso_compact`` plus a 4-char random
   hex suffix mirror the ``result_virtualize`` filename strategy.

Returned shape (see Track G in plan ``misty-squishing-badger`` § Iteration 2)::

    {
        "file_path": "/mnt/session/outputs/sf_<label>_<ts>_<uuid4[:4]>.parquet",
        "count": 3209,
        "schema": {"Id": "str", "Status": "str", "Score": "float", ...},
        "summary_stats": {col: {min, max, mean, null_count, ...}, ...},
        "preview_3": [<first 3 rows as dicts>],
        "summary_text": "max 500 chars of plain English summary",
    }

By default the handle is shrunk aggressively for context economy (PR 9,
2026-05-14): ``summary_stats`` only carries the first 5 schema columns
PLUS any column from ``_SUMMARY_INTERESTING_COLUMNS`` (allowlist of
analytically-load-bearing SF fields). High-cardinality columns
(distinct ratio > 0.9) report ``unique_pct: "0.99"`` instead of a raw
``unique_count``. ``top_values`` arrays are capped at 10 entries.
Preview drops to 3 rows. Target: <= 8 KB per handle.

Set ``expand=True`` to restore the FULL payload: every column in
``summary_stats``, ``preview_10``, no top_values cap. Use only when the
agent truly needs the full breakdown — every unmolested call competes
with multiagent context budget.

On error::

    {
        "file_path": None,
        "error": "<error message>",
        "count": 0,
        "schema": {},
        "summary_stats": {},
        "preview_3": [],
        "summary_text": "<error summary>",
    }
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Iterator

log = logging.getLogger(__name__)


__all__ = ["dump_sf_query"]


# Rows written per Parquet batch. Big enough to amortize the per-batch
# pyarrow overhead; small enough that peak memory stays well under 50 MB
# for typical SF row widths. Matches the SF Bulk API default page size.
_BATCH_SIZE = 1000

# Hard cap on the rows we use to compute summary_stats. Computing stats over
# a full 3,209-row Lead pull is fine, but a 500K-row dump should not stall
# the tool for minutes computing histograms. Stats over the first 1000 rows
# (the first Parquet batch) are representative enough for the model to plan.
_STATS_SAMPLE_LIMIT = 1000

# Max chars in the human-readable summary_text. The model reads this verbatim
# so it can describe the artifact in prose; longer than ~500 chars wastes
# context on what is meant to be a one-line orientation.
_SUMMARY_TEXT_MAX_CHARS = 500

# Handle-size guardrails (PR 9, 2026-05-14). Each dump_sf_query handle that
# enters multiagent context costs cache_read at every subsequent turn. A
# 26 KB handle per call x 23 turns x multiple sub-threads produced 1.74M
# cache_read tokens on a single demo question. The defaults below cap a
# representative handle at ~8 KB:
#
# - summary_stats only carries the first 5 columns by ordinal PLUS any
#   ``_SUMMARY_INTERESTING_COLUMNS`` allowlist column. Other columns are
#   reachable via query_artifact against the materialized Parquet.
# - preview drops from 10 rows to 3.
# - high-cardinality columns (distinct ratio > 0.9) report
#   ``unique_pct: "0.99"`` instead of a raw integer ``unique_count``
#   (string is more compact than ``"unique_count": 39214``).
# - top_values arrays are capped at 10 entries.
#
# An ``expand=True`` flag on dump_sf_query restores the full pre-PR-9 shape.
_SUMMARY_STATS_HEAD_COUNT = 5
_PREVIEW_ROW_COUNT_DEFAULT = 3
_TOP_VALUES_HARD_CAP = 10
_HIGH_CARDINALITY_THRESHOLD = 0.9

# Allowlist of SF column names whose summary stats are analytically
# load-bearing for GTM analysis even when their schema ordinal is > 5.
# StageName, RecordType_Name, and IsClosed/IsWon drive deal segmentation;
# CloseDate / CreatedDate / LastModifiedDate drive cohorting; Amount and
# ARR_Total__c are the headline numbers; Type discriminates new business
# vs renewal; OwnerId is the rep dimension. Status and LeadSource drive
# lead-side funnel analysis (added 2026-05-14 — most Lead pulls start with
# Id/Name/Email/Phone/CreatedDate, which knocks Status/LeadSource past the
# 5-column head cap, but those two fields are the ones the Specialists
# actually segment by).
_SUMMARY_INTERESTING_COLUMNS = frozenset(
    {
        "StageName",
        "RecordType_Name",
        "Amount",
        "ARR_Total__c",
        "OwnerId",
        "CloseDate",
        "Type",
        "CreatedDate",
        "LastModifiedDate",
        "IsClosed",
        "IsWon",
        "Status",
        "LeadSource",
    }
)


def _default_output_dir() -> str:
    """Return the canonical session output directory.

    ``SESSION_OUTPUT_DIR`` env var wins (tests override the real
    ``/mnt/session/outputs`` mount). Otherwise the Railway-side default.
    """
    return os.environ.get("SESSION_OUTPUT_DIR") or "/mnt/session/outputs"


def _utc_iso_compact() -> str:
    """``20260512T065023123456`` — sortable, microsecond-precision, no punctuation."""
    return datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%S%f")


def _safe_label(label: str) -> str:
    """Sanitize a snake_case label so it's safe to use as a path component.

    Anything that isn't alphanumeric, underscore, or hyphen is dropped. Output
    is capped at 50 chars so file names stay manageable.
    """
    keep = []
    for ch in label:
        if ch.isalnum() or ch in ("_", "-"):
            keep.append(ch)
    out = "".join(keep) or "query"
    return out[:50]


def _ensure_output_dir(output_dir: str) -> tuple[str, str | None]:
    """Create ``output_dir`` if missing. Return a structured error on failure.

    Returns ``(resolved_dir, error)`` where ``error`` is None on success or a
    short message describing why the directory could not be created.

    Why no fallback? Track B's ``_dispatch_post_report`` whitelist only permits
    attachments under ``_session_output_dir()`` (i.e. ``SESSION_OUTPUT_DIR``).
    A fallback path outside that root produces a ``file_path`` the agent
    believes is attachable but ``_is_safe_attachment_path`` silently rejects —
    the dump looks successful but the user never sees the file in Slack.
    Returning an error is more honest: the operator (or the agent) sees the
    real failure and can fix permissions or set ``SESSION_OUTPUT_DIR``.
    """
    try:
        os.makedirs(output_dir, exist_ok=True)
        return output_dir, None
    except (OSError, PermissionError) as e:
        return output_dir, (
            f"Could not create output directory {output_dir}: {e}. "
            "Set SESSION_OUTPUT_DIR to a writable path or fix filesystem permissions."
        )


def _classify_value(value: Any) -> str:
    """Best-effort dtype label for a single value. Mirrors result_virtualize."""
    if value is None or value == "":
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    return "str"


def _infer_schema(rows: list[dict]) -> dict[str, str]:
    """Infer ``{column: dtype}`` from a batch of records.

    Promotes ``null`` to whatever dtype is seen first; collapses mixed numeric
    types to ``float``; falls back to ``str`` on any heterogeneity. Mirrors
    ``result_virtualize._classify_dtype`` semantics so the two modules stay
    consistent for downstream agent reasoning.
    """
    if not rows:
        return {}

    # Use the union of keys from the first batch so optional columns that
    # only appear in later rows of the first batch aren't dropped.
    columns: list[str] = []
    seen = set()
    for row in rows:
        for k in row.keys():
            if k not in seen:
                seen.add(k)
                columns.append(k)

    schema: dict[str, str] = {}
    for col in columns:
        dtype = "null"
        for row in rows:
            v = row.get(col)
            cls = _classify_value(v)
            if cls == "null":
                continue
            if dtype == "null":
                dtype = cls
                continue
            if dtype == cls:
                continue
            # Mixed: int + float → float; everything else → str.
            if {dtype, cls} == {"int", "float"}:
                dtype = "float"
                continue
            dtype = "str"
            break
        # Plan: Design J (2026-05-15). If we'd otherwise call the column a
        # plain ``str``, check whether it's actually a SF date / datetime
        # column. Inferred from the column name + a sample value matching
        # ISO-8601. This was the LastActivityDate-as-VARCHAR pain on
        # sesn_EXAMPLE — DuckDB callers had to TRY_CAST every time. Date
        # detection here makes downstream queries SQL-native.
        if dtype == "str":
            promoted = _maybe_promote_to_date(col, rows)
            if promoted is not None:
                dtype = promoted
        schema[col] = dtype
    return schema


# Plan: Design J (2026-05-15) — SF date/datetime column detection.
#
# SF returns date-typed fields as ISO-8601 strings ("2026-05-15" for Date
# and "2026-05-15T20:13:00.000+0000" for DateTime). Our inferrer classifies
# these as plain strings and writes them as ``pa.string()``, so callers
# downstream have to ``TRY_CAST(... AS DATE)`` in DuckDB. Detecting them
# here makes the Parquet column natively typed.
#
# Column-name hints catch the common SF naming conventions:
#   - Suffix __c with Date / Date_Time / DateTime
#   - Standard fields ending in Date / DateTime
#   - Specific SF system fields (CreatedDate, LastModifiedDate,
#     LastActivityDate, SystemModstamp, ConvertedDate, CloseDate)
#
# We pair the name hint with a value check so a poorly-named string
# column doesn't get force-coerced. The value check looks at up to 5
# non-null samples — if any look like ISO date/datetime, promote.
import re as _re

_DATE_HINT_SUFFIXES = (
    "date",
    "datetime",
    "date_time__c",
    "datetime__c",
    "date__c",
)
_KNOWN_DATE_FIELDS = frozenset(
    {
        "createddate",
        "lastmodifieddate",
        "lastactivitydate",
        "systemmodstamp",
        "converteddate",
        "closedate",
        "birthdate",
    }
)
_ISO_DATE_RE = _re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ISO_DATETIME_RE = _re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+\-]\d{2}:?\d{2})?$"
)


def _maybe_promote_to_date(col: str, rows: list[dict]) -> str | None:
    """Return ``"date"`` or ``"datetime"`` if this column should be promoted,
    else None. ``_classify_value`` will otherwise leave it as ``"str"``.

    Promotion requires BOTH a name hint AND at least one ISO-shaped sample
    value. Either-or alone is too aggressive (a notes field called
    ``last_activity_date_notes__c`` would otherwise trip the column-name
    hint without containing date values).
    """
    lname = col.lower()
    name_hints_date = lname in _KNOWN_DATE_FIELDS or any(
        lname.endswith(suffix) for suffix in _DATE_HINT_SUFFIXES
    )
    if not name_hints_date:
        return None

    samples_seen = 0
    has_datetime_sample = False
    has_date_sample = False
    for row in rows:
        v = row.get(col)
        if v is None or v == "":
            continue
        if not isinstance(v, str):
            return None  # already a real type (datetime obj, etc.) — let it be
        if _ISO_DATETIME_RE.match(v):
            has_datetime_sample = True
        elif _ISO_DATE_RE.match(v):
            has_date_sample = True
        else:
            return None  # mixed / non-ISO string → don't promote
        samples_seen += 1
        if samples_seen >= 5:
            break

    if has_datetime_sample:
        return "datetime"
    if has_date_sample:
        return "date"
    return None


def _strip_sf_attributes(record: dict) -> dict:
    """SF REST records include a synthetic ``attributes`` dict — drop it.

    ``simple_salesforce`` yields rows like
    ``{"attributes": {"type": "Lead", "url": "..."}, "Id": "...", ...}``.
    The ``attributes`` field is noise for our purpose and inflates the
    Parquet file. Drop it before we infer schema or write.
    """
    if "attributes" in record:
        record = {k: v for k, v in record.items() if k != "attributes"}
    return record


# Only unquote a date literal that follows a RELATIONAL operator (>, <, >=, <=).
# Date columns are range-compared; a quoted date there 400s and is the bug we
# fix. Equality (``=``) is left alone on purpose: a TEXT field can legitimately
# hold a date-looking string (e.g. ``Campaign_Code__c = '2024-01-01'``) and
# unquoting that would corrupt a valid filter (codex review, 2026-06-24).
_SOQL_QUOTED_DATE_RE = _re.compile(r"([<>]=?\s*)'(\d{4}-\d{2}-\d{2}(?:T[\d:.+Z-]*)?)'")


def _unquote_soql_date_literals(soql: str) -> str:
    """Strip surrounding single quotes from ISO date/datetime literals that are
    range-compared in a SOQL WHERE clause. SOQL date literals are bare by spec;
    quoting them 400s. Only ``<op> 'YYYY-MM-DD[T...]'`` (op in >, <, >=, <=) is
    touched — equality comparisons and other string literals are left intact so
    a text field holding a date-like value isn't corrupted."""
    repaired = _SOQL_QUOTED_DATE_RE.sub(r"\1\2", soql)
    if repaired != soql:
        log.info("[SOQL_DATE_REPAIR] unquoted date literal(s) in SOQL")
    return repaired


def _parse_soql_relationship_fields(soql: str) -> dict[str, list[str]]:
    """Extract ``Parent.Child`` relationship fields from a SOQL ``SELECT`` clause.

    Returns ``{"Owner": ["Id", "Name"], "RecordType": ["Name"], ...}``.

    Why this exists
    ---------------
    ``_flatten_sf_relationships`` needs to know which top-level keys will be
    relationship dicts BEFORE it sees them populated. If the first 1,000 rows
    have ``Owner: None`` but row 1,001 has ``Owner: {"Id": ..., "Name": ...}``,
    the schema is already locked (the first batch defines it). Without a
    pre-declared expectation, ``Owner`` is observed as a scalar null and
    ``Owner_Id`` / ``Owner_Name`` from later rows never make it into the
    Parquet schema — those values are silently dropped at flush. Codex flagged
    this on the original commit (review verdict 2026-05-14).

    Implementation note: this is a lightweight regex pass, NOT a full SOQL
    parser. It handles the dominant case of ``SELECT a, b.c, d.e FROM ...``,
    including bracketed sub-selects which we deliberately ignore (sub-selects
    materialize as their own list of dicts and our flattener leaves them
    alone). Aliases, ``TYPEOF``, and aggregate functions are out of scope —
    the worst-case failure mode is that we miss a relationship and fall back
    to the original "flatten when we see a dict" path, which is exactly the
    pre-fix behavior. We do not regress correctness for cases this parser
    doesn't cover.
    """
    import re

    # Strip parenthesized groups FIRST so sub-selects don't confuse the
    # SELECT..FROM bracket. Repeat until stable to handle nested parens. This
    # also keeps the outer SELECT-list match short-circuiting on the OUTER
    # ``FROM`` rather than the sub-select's. Codex flagged the original
    # ordering for missing relationships after a sub-select on the same row.
    cleaned = soql
    while True:
        stripped = re.sub(r"\([^()]*\)", "", cleaned)
        if stripped == cleaned:
            break
        cleaned = stripped

    # Isolate the SELECT-list between the OUTER ``SELECT`` and ``FROM``
    # (case-insensitive, multiline). Anything outside that span is irrelevant
    # for column names.
    m = re.search(r"\bSELECT\b(.*?)\bFROM\b", cleaned, flags=re.IGNORECASE | re.DOTALL)
    if not m:
        return {}
    select_clause = m.group(1)

    rel: dict[str, list[str]] = {}
    for raw in select_clause.split(","):
        token = raw.strip()
        if not token or "." not in token:
            continue
        # Plan: Design K (2026-05-15) — preserve the FULL dotted child path
        # so ``Owner.UserRole.Name`` becomes ``("Owner", "UserRole.Name")``
        # and the flattener can traverse the nested dict. Without this,
        # ``Owner_UserRole`` was emitted as a serialized OrderedDict, which
        # downstream consumers had to unwrap with regexp_extract (live trace
        # 2026-05-15 across L9xZx + 17YiJ + H9wvuyW). The depth-1 case is
        # unchanged: child without a dot stays a flat key.
        parent, child = token.split(".", 1)
        parent = parent.strip()
        child = child.strip()
        if not parent or not child:
            continue
        bucket = rel.setdefault(parent, [])
        if child not in bucket:
            bucket.append(child)
    return rel


def _flatten_sf_relationships(
    record: dict,
    expected_relationships: dict[str, list[str]] | None = None,
) -> dict:
    """Promote SF relationship sub-fields to top-level ``Parent_Child`` columns.

    SOQL queries that include relationship fields return nested dicts:

        {"Id": "006...", "Owner": {"Id": "005...", "Name": "Jane",
                                   "attributes": {"type": "User", ...}}}

    Before this flattening pass the schema inferrer saw ``Owner`` as a
    non-scalar value, defaulted it to ``str`` dtype, and ``_coerce_for_parquet``
    wrote it as ``"{'Id': '005...', 'Name': 'Jane', ...}"`` — a Python
    ``repr`` of the dict. Downstream readers (query_artifact / DuckDB,
    Python pandas, the user opening the .xlsx) couldn't filter or pivot
    on ``Owner.Name`` without re-parsing that string.

    PR #170 fixed the immediate crash in ``result_virtualize`` by
    JSON-encoding nested dicts before they hit set/dict-key code paths.
    This is the deeper cleanup: relationship dicts become real columns
    (``Owner_Id``, ``Owner_Name``, ``RecordType_Name``, etc.) so the
    Parquet file is queryable in the shape callers expect.

    Recursion: only one level. Salesforce relationship traversal supports
    deeper (e.g. ``Account.Owner.Manager.Name``) but a single dot is by
    far the dominant case in our SOQL. Recursing N levels risks column-
    name collisions if callers ever query the same leaf field via two
    different paths. The ``attributes`` sub-key on every relationship
    dict is dropped to match the top-level strip.

    Null-first batches
    ------------------
    ``expected_relationships`` (parsed from the SOQL by
    ``_parse_soql_relationship_fields``) lets the flattener emit the SAME
    ``Parent_Child`` keys even when the relationship is null in early rows.
    Without it, ``Owner: None`` in the first 1,000 rows would lock the
    Parquet schema with a scalar ``Owner`` column, and later rows where
    ``Owner`` arrives as a dict would silently lose their ``Owner_Id`` /
    ``Owner_Name`` cells at flush time. With the map, the parent key is
    always dropped and the child keys always emitted (as None when the
    relationship is null), so the schema stays stable across the whole
    stream. Codex review caught this gap on the original commit.
    """
    flat: dict = {}
    expected = expected_relationships or {}
    for key, value in record.items():
        if isinstance(value, dict) and "records" in value and "totalSize" in value:
            # Salesforce child/aggregate subquery envelope — e.g.
            # ``(SELECT Id, Name FROM Contacts)`` returns
            # ``{"totalSize": N, "done": bool, "records": [...]}``. The comma-
            # split SOQL parser never captures these as relationships, so before
            # this branch they fell through to the generic dict path and the
            # ``records`` LIST was written as a Python repr (#328, #330). Emit a
            # queryable ``<Parent>_count`` int and a ``<Parent>_json`` JSON
            # string instead — both DuckDB- and pandas-readable.
            child_records = value.get("records") or []
            cleaned = [
                {k: v for k, v in (rec or {}).items() if k != "attributes"}
                for rec in child_records
            ]
            flat[f"{key}_count"] = value.get("totalSize", len(cleaned))
            flat[f"{key}_json"] = json.dumps(cleaned, default=str)
            continue
        if key in expected:
            # Pre-declared relationship: always emit the Parent_Child columns,
            # whether the relationship is populated, null, or missing. This
            # keeps the Parquet schema stable across batches. Plan: Design K
            # (2026-05-15) — child paths can contain dots (e.g. "UserRole.Name"),
            # which means traversing one more level into the dict.
            if isinstance(value, dict):
                for sub_key in expected[key]:
                    flat[f"{key}_{sub_key.replace('.', '_')}"] = _resolve_nested(
                        value, sub_key
                    )
                # Also surface any sub-fields the dict carries that SOQL didn't
                # explicitly mention (rare but harmless), excluding attributes.
                # Depth-1 only for the undeclared-leaf surfacing — we don't
                # speculatively traverse arbitrarily deep dicts.
                declared_top = {p.split(".", 1)[0] for p in expected[key]}
                for sub_key, sub_value in value.items():
                    if sub_key == "attributes" or sub_key in declared_top:
                        continue
                    flat[f"{key}_{sub_key}"] = sub_value
            else:
                # None or some unexpected scalar — emit declared sub-fields as None.
                for sub_key in expected[key]:
                    flat[f"{key}_{sub_key.replace('.', '_')}"] = None
        elif isinstance(value, dict):
            # Undeclared relationship dict (parser missed it, or SOQL named the
            # parent without dot-paths). Fall back to inline flattening so we
            # don't regress to the pre-fix Python-repr behavior.
            for sub_key, sub_value in value.items():
                if sub_key == "attributes":
                    continue
                flat[f"{key}_{sub_key}"] = sub_value
        else:
            flat[key] = value
    return flat


def _resolve_nested(d: dict, path: str):
    """Resolve a dotted ``path`` against a nested dict, returning None on miss.

    Plan: Design K (2026-05-15). For depth-2 relationship traversal in the
    Parquet flattener — ``_resolve_nested({"UserRole": {"Name": "AE"}}, "UserRole.Name")``
    returns ``"AE"``. A missing intermediate key returns None; a non-dict
    intermediate also returns None so we don't trip on legitimately-null
    relationships (e.g. ``Owner.UserRole`` is None for unassigned users).
    """
    current: object = d
    for part in path.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
        if current is None:
            return None
    return current


def _coerce_for_parquet(value: Any, dtype: str) -> Any:
    """Coerce a value to the inferred dtype before writing to Parquet.

    pyarrow rejects mixed types within a column, so we cast on the way in.
    ``None`` and empty-string stay as None.
    """
    if value is None or value == "":
        return None
    try:
        if dtype == "int":
            return int(value)
        if dtype == "float":
            return float(value)
        if dtype == "bool":
            return bool(value)
        if dtype == "date":
            return _coerce_iso_date(value)
        if dtype == "datetime":
            return _coerce_iso_datetime(value)
        # str / null: JSON-encode containers so a stray dict/list (an
        # unflattened relationship or OrderedDict) is queryable JSON, never a
        # Python repr like ``OrderedDict([...])`` (#303, #305, #325).
        if isinstance(value, (dict, list)):
            return json.dumps(value, default=str)
        return str(value)
    except (TypeError, ValueError):
        if isinstance(value, (dict, list)):
            return json.dumps(value, default=str)
        return str(value)


def _coerce_iso_date(value: Any):
    """Parse an ISO-8601 date string to ``datetime.date``. Fallback: keep str.

    Design J fallback: any value the inferrer couldn't classify cleanly
    keeps its original repr (stringified). pyarrow's date32 column will
    reject mixed types, so the fallback path returns a str — and that
    pushes the whole column back to string at write time via the
    schema-mismatch path. Acceptable: the user sees the original behavior,
    not a crash.
    """
    from datetime import date, datetime

    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
        except ValueError:
            return str(value)
    return str(value)


def _coerce_iso_datetime(value: Any):
    """Parse an ISO-8601 datetime string to ``datetime``. Fallback: keep str."""
    from datetime import datetime, timezone

    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            # Normalize to UTC so pyarrow doesn't trip on mixed tz.
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc).replace(tzinfo=None)
        except ValueError:
            return str(value)
    return str(value)


def _summary_stats_for_sample(
    sample: list[dict], schema: dict[str, str], total_count: int
) -> dict[str, dict]:
    """Compute per-column summary stats from a sample, scaled to the full count.

    Reuses the same shape as ``result_virtualize.compute_summary_stats`` so the
    Coordinator prompt has ONE contract for both code paths. ``null_count`` is
    scaled from the sample to the full row count so the model sees a realistic
    "X% of rows have this column null" number even on huge dumps.

    Lazy import keeps test cold-start fast for paths that don't compute stats.
    """
    if not sample:
        return {}

    from result_virtualize import compute_summary_stats

    sample_stats = compute_summary_stats(sample)
    sample_n = len(sample)
    if sample_n == 0 or sample_n == total_count:
        return sample_stats

    # Scale null_count proportionally so the model gets a count that matches
    # the full population, not the sample. Other fields (min/max/mean/
    # top_values/unique_count) stay as the sample estimate — labelled as
    # such via a tag the renderer can pick up if desired.
    scale = total_count / sample_n
    for col, col_stats in sample_stats.items():
        if "null_count" in col_stats:
            col_stats["null_count"] = int(round(col_stats["null_count"] * scale))
        # Mark the stat as sample-derived so the model knows.
        col_stats["sampled_from"] = sample_n
        col_stats["total_rows"] = total_count
    return sample_stats


def _shrink_summary_stats(
    summary_stats: dict[str, dict],
    schema: dict[str, str],
    *,
    total_count: int,
) -> dict[str, dict]:
    """Apply PR 9 size caps to a full ``summary_stats`` dict.

    Returns a NEW dict — does not mutate the input. Keeps the first
    ``_SUMMARY_STATS_HEAD_COUNT`` columns by schema ordinal PLUS any
    column in the ``_SUMMARY_INTERESTING_COLUMNS`` allowlist. For each
    retained column, caps any ``top_values`` array at
    ``_TOP_VALUES_HARD_CAP`` and converts a high-cardinality
    ``unique_count`` (>0.9 distinct ratio) into a string
    ``unique_pct: "0.99"`` sentinel.

    Why a string-keyed ``unique_pct: "0.99"``? The model only needs the
    "this is an id-like column" signal. ``unique_count: 39214`` carries
    no analytical value beyond that signal and inflates the JSON
    handle. ``"0.99"`` is two short tokens; raw counts are five-plus.

    Sample-derived denominator
    --------------------------
    For dumps larger than ``_STATS_SAMPLE_LIMIT`` the per-column
    ``unique_count`` is computed against the sample, not the full row
    count. ``_summary_stats_for_sample`` tags each col with
    ``sampled_from`` (the sample size, e.g. 1000) and ``total_rows``
    (the full population). The high-cardinality check uses
    ``sampled_from`` as the denominator when present so a 100K-row dump
    with a 1000-row sample correctly collapses an id-like column whose
    sample-distinct count is ~1000. Falling back to ``total_count``
    only when no sample tag is present (small dump, full-population
    stats) preserves the original behavior for the small-pull path
    (codex review caught this on the initial commit).
    """
    if not summary_stats:
        return {}

    ordered_cols = list(schema.keys()) if schema else list(summary_stats.keys())
    head = set(ordered_cols[:_SUMMARY_STATS_HEAD_COUNT])
    allow = _SUMMARY_INTERESTING_COLUMNS

    shrunk: dict[str, dict] = {}
    for col in ordered_cols:
        if col not in summary_stats:
            continue
        if col not in head and col not in allow:
            continue
        col_stats = dict(summary_stats[col])  # shallow copy — don't mutate caller

        # Cap top_values arrays. High-cardinality columns produced 50+ entry
        # arrays pre-PR-9; 10 covers the analytic need.
        top = col_stats.get("top_values")
        if isinstance(top, list) and len(top) > _TOP_VALUES_HARD_CAP:
            col_stats["top_values"] = top[:_TOP_VALUES_HARD_CAP]

        # Replace integer unique_count with a unique_pct sentinel when the
        # column is high-cardinality. The sentinel encodes the same signal
        # ("this is an id-like column") in fewer characters. Use the sample
        # size as the denominator when stats are sample-derived; total_count
        # otherwise. Without this, a 100K-row dump with a 1000-row sample
        # gets distinct_ratio = sampled_unique_count / 100000 ≈ 0.01 and
        # the high-cardinality column escapes the shrink — exactly the case
        # this defense exists to handle.
        unique_count = col_stats.get("unique_count")
        if isinstance(unique_count, int):
            sampled_from = col_stats.get("sampled_from")
            denom = (
                sampled_from
                if isinstance(sampled_from, int) and sampled_from > 0
                else total_count
            )
            if denom > 0:
                distinct_ratio = unique_count / denom
                if distinct_ratio > _HIGH_CARDINALITY_THRESHOLD:
                    col_stats.pop("unique_count")
                    col_stats["unique_pct"] = "0.99"

        shrunk[col] = col_stats
    return shrunk


def _build_summary_text(
    *, count: int, file_name: str, schema: dict[str, str], summary_stats: dict
) -> str:
    """One-sentence orientation the model can quote verbatim in its prose.

    Example::

        Materialized 3,209 rows to sf_leads_2026-05-12T06-50.parquet.
        9 columns. Top Status: 'Converted-Qualified' (1,035 / 32.3%).

    When ``count > _STATS_SAMPLE_LIMIT``, ``summary_stats`` was computed from
    a sample, so ``top_values[*].count`` is a sample count. We scale it to the
    full row count and label the estimate so the orientation text is grounded
    in the real population, not the sample.
    """
    parts = [f"Materialized {count:,} rows to {file_name}.", f"{len(schema)} columns."]

    sampled = count > _STATS_SAMPLE_LIMIT
    sample_n = min(count, _STATS_SAMPLE_LIMIT) if sampled else count

    # Pick the lowest-cardinality string column with a top value.
    for col, stats in summary_stats.items():
        top = stats.get("top_values")
        if top and isinstance(top, list) and top:
            top_val = top[0]
            value = top_val.get("value")
            tc = top_val.get("count", 0)
            if count <= 0:
                parts.append(f"Top {col}: '{value}' ({tc:,}).")
            elif sampled and sample_n > 0:
                # ``tc`` is a sample count — scale to the full population.
                scaled = int(round(tc * count / sample_n))
                pct = scaled / count * 100
                parts.append(
                    f"Top {col}: '{value}' (~{scaled:,} / {pct:.1f}%, "
                    f"estimated from {sample_n:,}-row sample)."
                )
            else:
                pct = (tc / count) * 100
                parts.append(f"Top {col}: '{value}' ({tc:,} / {pct:.1f}%).")
            break

    text = " ".join(parts)
    return text[:_SUMMARY_TEXT_MAX_CHARS]


def _empty_error_result(error: str, *, count: int = 0, expand: bool = False) -> dict:
    """The single error-return shape so callers can rely on stable keys.

    The preview key is ``preview_10`` when ``expand=True`` (full payload
    contract), otherwise ``preview_3`` (PR 9 default — keeps the handle
    small even on error paths).
    """
    preview_key = "preview_10" if expand else "preview_3"
    return {
        "file_path": None,
        "error": error,
        "count": count,
        "schema": {},
        "summary_stats": {},
        preview_key: [],
        "summary_text": f"dump_sf_query failed: {error[:400]}",
    }


def _iter_records(sf_client, soql: str) -> Iterator[dict]:
    """Wrap ``Salesforce.query_all_iter`` so callers can mock the SF client.

    Tests inject a fake client whose ``query_all_iter`` yields synthetic
    rows; production uses ``simple_salesforce.Salesforce.query_all_iter``
    which streams from the SF REST API page-by-page without holding the
    full result set in memory.
    """
    yield from sf_client.query_all_iter(soql)


def dump_sf_query(
    soql: str,
    portco_key: str,
    label: str,
    output_dir: str | None = None,
    expand: bool = False,
) -> dict:
    """Materialize a Salesforce SOQL query to a Parquet file.

    Args:
        soql: Full SOQL query (e.g. ``SELECT Id, Name, Status FROM Lead WHERE
            CreatedDate >= 2025-09-01T00:00:00Z``).
        portco_key: Portco identifier for credential lookup (e.g. ``"acme"``).
        label: Short snake_case label for the output file (e.g.
            ``"leads_discovery_call_booked"``). Sanitized to alphanumeric +
            underscore + hyphen.
        output_dir: Directory for the Parquet file. Defaults to
            ``SESSION_OUTPUT_DIR`` env, then ``/mnt/session/outputs``. Created
            if missing. If creation fails (permission error, read-only FS),
            returns a structured error — no fallback path, because any path
            outside ``SESSION_OUTPUT_DIR`` is silently rejected by Track B's
            ``_dispatch_post_report`` whitelist and the user would never see
            the attachment in Slack.
        expand: When False (default, PR 9), return a shrunk handle <= 8 KB —
            ``summary_stats`` only carries the first 5 columns plus any
            allowlist column, ``preview_3`` carries 3 rows, top_values
            arrays cap at 10, high-cardinality columns report
            ``unique_pct: "0.99"`` instead of a raw ``unique_count``. When
            True, restore the full payload (every column in summary_stats,
            preview_10, no top_values cap). Opt in only when the agent
            truly needs the full breakdown — every unmolested call competes
            with multiagent context budget.

    Returns:
        On success (default, ``expand=False``)::

            {
                "file_path": "/mnt/session/outputs/sf_<label>_<ts>_<uuid4>.parquet",
                "count": <int>,
                "schema": {column: dtype, ...},
                "summary_stats": {first 5 + allowlist cols: {...}, ...},
                "preview_3": [<first 3 rows as dicts>],
                "summary_text": "<= 500 chars plain English summary",
            }

        On success (``expand=True``)::

            {
                ...,
                "summary_stats": {every column: {...}, ...},
                "preview_10": [<first 10 rows as dicts>],
                ...,
            }

        On failure (auth, network, disk, malformed SOQL) — never raises::

            {
                "file_path": None,
                "error": "<message>",
                "count": 0,
                "schema": {},
                "summary_stats": {},
                "preview_3": [],  # or preview_10 when expand=True
                "summary_text": "dump_sf_query failed: ...",
            }
    """
    # Lazy imports — keep the module import cheap and break the
    # session_runner ↔ sf_dump_tool circular dependency.
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError as e:
        return _empty_error_result(f"pyarrow not installed: {e}", expand=expand)

    try:
        from session_runner import _get_sf_client
    except ImportError as e:
        return _empty_error_result(f"session_runner import failed: {e}", expand=expand)

    # Build output path before we hit SF so we can short-circuit on a
    # broken filesystem without spending a single SOQL roundtrip.
    out_dir = output_dir or _default_output_dir()
    resolved_dir, _err = _ensure_output_dir(out_dir)
    if _err is not None:
        # No fallback — any path outside SESSION_OUTPUT_DIR fails Track B's
        # _is_safe_attachment_path whitelist silently. Return the structured
        # error so the agent and operator see the dump didn't succeed.
        return _empty_error_result(_err, expand=expand)

    fname = (
        f"sf_{_safe_label(label)}_{_utc_iso_compact()}_{uuid.uuid4().hex[:4]}.parquet"
    )
    file_path = os.path.join(resolved_dir, fname)

    # Resolve SF credentials. Every failure here returns a structured error;
    # we never raise out of this function.
    try:
        sf_client = _get_sf_client(portco_key)
    except Exception as e:
        # Includes SalesforceAuthenticationFailed, missing-credential
        # RuntimeError from _get_sf_client, network failures, etc.
        return _empty_error_result(f"sf_auth_failed: {e}", expand=expand)

    # Auto-unquote SOQL date/datetime literals (#296, #298, #321, #323). SF's
    # REST API rejects quoted ISO dates in WHERE clauses (CreatedDate >=
    # '2024-01-01' → 400). String literals (Status = 'Open') keep their quotes.
    # Repairing here turns a guaranteed 400-and-retry loop into a clean read.
    soql = _unquote_soql_date_literals(soql)

    # Parse SOQL once so the flattener can emit consistent ``Parent_Child``
    # column names even when early rows have a null relationship. Without
    # this, a null-first batch locks the schema with a scalar parent column
    # and later populated rows silently lose their relationship cells at
    # flush time (codex review caught this on the original commit).
    expected_relationships = _parse_soql_relationship_fields(soql)

    writer = None
    schema = {}
    batch_buffer: list[dict] = []
    sample_for_stats: list[dict] = []
    preview: list[dict] = []
    count = 0

    def _flush(final: bool = False) -> None:
        """Write the current batch to Parquet. Open writer on first call."""
        nonlocal writer
        if not batch_buffer:
            return
        # Coerce each cell to the inferred dtype so pyarrow accepts the column.
        coerced: dict[str, list] = {col: [] for col in schema.keys()}
        for row in batch_buffer:
            for col, dtype in schema.items():
                coerced[col].append(_coerce_for_parquet(row.get(col), dtype))
        # Build a pyarrow Table from the column-major coerced data.
        pa_fields = []
        pa_arrays = []
        for col, dtype in schema.items():
            if dtype == "int":
                pa_type = pa.int64()
            elif dtype == "float":
                pa_type = pa.float64()
            elif dtype == "bool":
                pa_type = pa.bool_()
            elif dtype == "date":
                pa_type = pa.date32()
            elif dtype == "datetime":
                # us precision matches simple_salesforce's typical ms granularity
                # plus headroom; tz=None because we normalized to UTC-naive in
                # _coerce_iso_datetime.
                pa_type = pa.timestamp("us")
            else:
                pa_type = pa.string()
            pa_fields.append(pa.field(col, pa_type))
            try:
                pa_arrays.append(pa.array(coerced[col], type=pa_type))
            except (pa.ArrowInvalid, pa.ArrowTypeError):
                # Plan: Design J fallback (2026-05-15). If pyarrow can't accept
                # the column at the inferred typed-dtype (e.g. one row failed
                # ISO parsing and got passed through as str), fall back to
                # pa.string() and stringify each cell. Better degraded than
                # crashed. The schema-mismatch escape hatch existed implicitly
                # before Design J because everything was str; we preserve it.
                pa_fields[-1] = pa.field(col, pa.string())
                pa_arrays.append(
                    pa.array(
                        [None if v is None else str(v) for v in coerced[col]],
                        type=pa.string(),
                    )
                )
        table = pa.Table.from_arrays(pa_arrays, schema=pa.schema(pa_fields))
        if writer is None:
            writer = pq.ParquetWriter(file_path, table.schema)
        writer.write_table(table)
        batch_buffer.clear()

    try:
        for raw in _iter_records(sf_client, soql):
            # Relationship dicts (Owner, RecordType, etc.) become flat
            # Owner_Id / Owner_Name / RecordType_Name columns BEFORE the
            # schema inferrer sees them. Otherwise pyarrow gets a dict in
            # a "str" column and serializes it as Python repr — unqueryable
            # downstream. Strip the SF attributes envelope first, then
            # flatten so the per-relationship attributes dicts disappear too.
            record = _flatten_sf_relationships(
                _strip_sf_attributes(raw), expected_relationships
            )
            count += 1
            if len(preview) < 10:
                preview.append(record)
            if len(sample_for_stats) < _STATS_SAMPLE_LIMIT:
                sample_for_stats.append(record)
            batch_buffer.append(record)

            # First time we hit the batch size we lock the schema, then
            # flush. Subsequent batches reuse the locked schema.
            if not schema and len(batch_buffer) >= _BATCH_SIZE:
                schema = _infer_schema(batch_buffer)
                _flush()
            elif schema and len(batch_buffer) >= _BATCH_SIZE:
                _flush()
    except Exception as e:
        # Mid-iteration failure — close any partially-written file and return
        # an error result. We do NOT keep a half-baked Parquet file; the
        # downstream agent should retry the whole query if it wants.
        try:
            if writer is not None:
                writer.close()
        except Exception:
            pass
        try:
            if os.path.exists(file_path):
                os.remove(file_path)
        except Exception:
            pass
        return _empty_error_result(f"sf_query_failed: {e}", count=count, expand=expand)

    # Final flush — handle the (very common) case where total rows < batch.
    if batch_buffer:
        if not schema:
            schema = _infer_schema(batch_buffer)
        _flush(final=True)

    try:
        if writer is not None:
            writer.close()
    except Exception as e:
        return _empty_error_result(
            f"parquet_close_failed: {e}", count=count, expand=expand
        )

    # Zero-row case — return a clean empty handle with no file (writer was
    # never opened). The model gets a clear "nothing to deliver" signal.
    if count == 0:
        preview_key = "preview_10" if expand else "preview_3"
        return {
            "file_path": None,
            "count": 0,
            "schema": {},
            "summary_stats": {},
            preview_key: [],
            "summary_text": f"dump_sf_query returned 0 rows for label='{label}'.",
        }

    summary_stats_full = _summary_stats_for_sample(sample_for_stats, schema, count)
    # summary_text picks the most informative low-cardinality column. Compute
    # it from the FULL stats dict so we don't lose orientation prose just
    # because a useful column is past ordinal 5 and not on the allowlist.
    summary_text = _build_summary_text(
        count=count,
        file_name=os.path.basename(file_path),
        schema=schema,
        summary_stats=summary_stats_full,
    )

    # Write a sibling .xlsx so the post_report attachment layer can swap
    # Parquet → xlsx as the last step before Slack upload. The model never
    # sees this path — it keeps reasoning about the Parquet handle for
    # query_artifact. Silent fallback on any failure: xlsx delivery is a
    # UX nice-to-have, the Parquet path must remain intact.
    try:
        from xlsx_export import parquet_to_xlsx_sibling

        parquet_to_xlsx_sibling(file_path)
    except Exception:
        log.debug("xlsx export side-effect skipped (non-fatal)")

    if expand:
        # Full pre-PR-9 payload — caller asked for it explicitly.
        return {
            "file_path": file_path,
            "count": count,
            "schema": schema,
            "summary_stats": summary_stats_full,
            "preview_10": preview,
            "summary_text": summary_text,
        }

    # Default (PR 9): caps applied. summary_stats is trimmed to the first
    # _SUMMARY_STATS_HEAD_COUNT columns + allowlist columns, top_values
    # arrays cap at _TOP_VALUES_HARD_CAP, high-cardinality unique_count
    # collapses to a unique_pct sentinel, preview drops to 3 rows.
    summary_stats_small = _shrink_summary_stats(
        summary_stats_full, schema, total_count=count
    )
    return {
        "file_path": file_path,
        "count": count,
        "schema": schema,
        "summary_stats": summary_stats_small,
        "preview_3": preview[:_PREVIEW_ROW_COUNT_DEFAULT],
        "summary_text": summary_text,
    }
