"""Tests for schema_introspection."""

from __future__ import annotations

from unittest.mock import MagicMock

from schema_introspection import (  # pyright: ignore[reportMissingImports]
    fetch_field_definitions,
    introspect_portco,
    render_schema_cache,
)


def test_fetch_field_definitions_returns_records():
    client = MagicMock()
    client.query_all.return_value = {
        "records": [
            {
                "QualifiedApiName": "Closed_Lost_Notes__c",
                "Label": "Closed Lost Notes",
                "DataType": "Long Text Area(32768)",
                "Length": 32768,
            },
        ]
    }
    rows = fetch_field_definitions(client, "Opportunity")
    assert rows[0]["QualifiedApiName"] == "Closed_Lost_Notes__c"
    sql = client.query_all.call_args[0][0]
    assert "FieldDefinition" in sql
    assert "Opportunity" in sql


def test_fetch_field_definitions_swallows_errors():
    client = MagicMock()
    client.query_all.side_effect = RuntimeError("boom")
    rows = fetch_field_definitions(client, "Opportunity")
    assert rows == []


def test_render_includes_free_text_section_and_constraints():
    by_sobject = {
        "Opportunity": [
            {"QualifiedApiName": "Id", "Label": "Id", "DataType": "Id", "Length": 18},
            {
                "QualifiedApiName": "Closed_Lost_Notes__c",
                "Label": "Notes",
                "DataType": "Long Text Area(32768)",
                "Length": 32768,
            },
        ]
    }
    out = render_schema_cache("acme", by_sobject)
    assert "AUTO-GENERATED" in out
    assert "Long-Text-Area" in out  # SOQL constraint warning present
    assert "Free-text candidates" in out
    assert "Closed_Lost_Notes__c" in out
    # Free-text section appears BEFORE "All fields" — agents read top-down
    assert out.index("Free-text candidates") < out.index("All fields")


def test_introspect_portco_writes_file(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORY_STORE_ROOT", str(tmp_path))
    client = MagicMock()
    client.query_all.return_value = {
        "records": [
            {"QualifiedApiName": "Id", "Label": "Id", "DataType": "Id", "Length": 18},
        ]
    }
    stats = introspect_portco(client, "acme")
    assert stats["error"] is None
    assert stats["sobjects_queried"] == 7  # one per object in INTROSPECTED_SOBJECTS
    assert stats["fields_total"] == 7
    assert (tmp_path / "acme" / "schema_cache.md").exists()


def test_introspect_portco_handles_empty_org(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORY_STORE_ROOT", str(tmp_path))
    client = MagicMock()
    client.query_all.return_value = {"records": []}
    stats = introspect_portco(client, "acme")
    assert stats["error"] == "no_sobjects_returned_data"
    # No file written when nothing came back
    assert not (tmp_path / "acme" / "schema_cache.md").exists()
