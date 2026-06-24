"""Unit tests for db_adapter error taxonomy (Task 2 / F7).

Issues #283, #284, #310, #311: db_query failures must be classified
(unavailable / connection / query / permission) rather than bubbling up
as an opaque psycopg2 exception the model cannot act on.

Run:
    cd orchestrator && python3 -m pytest db_adapter_test.py -q
"""

from __future__ import annotations

import pytest

import db_adapter
from db_adapter import DbQueryError


class _FakeCur:
    def __init__(self, exc):
        self._exc = exc

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, *a, **k):
        raise self._exc

    def fetchall(self):
        return []


class _FakeConn:
    def __init__(self, exc):
        self._exc = exc

    def cursor(self, *a, **k):
        return _FakeCur(self._exc)

    def close(self):
        pass


def test_query_classifies_operational_error_as_connection(monkeypatch):
    import psycopg2

    monkeypatch.setattr(
        db_adapter,
        "_connect",
        lambda: _FakeConn(psycopg2.OperationalError("server closed")),
    )
    with pytest.raises(DbQueryError) as ei:
        db_adapter.query("SELECT 1")
    assert ei.value.kind == "connection"


def test_query_classifies_insufficient_privilege_as_permission(monkeypatch):
    import psycopg2

    monkeypatch.setattr(
        db_adapter,
        "_connect",
        lambda: _FakeConn(psycopg2.errors.InsufficientPrivilege("no access")),
    )
    with pytest.raises(DbQueryError) as ei:
        db_adapter.query("SELECT 1")
    assert ei.value.kind == "permission"


def test_query_classifies_generic_db_error_as_query(monkeypatch):
    import psycopg2

    monkeypatch.setattr(
        db_adapter,
        "_connect",
        lambda: _FakeConn(
            psycopg2.errors.UndefinedColumn('column "foo" does not exist')
        ),
    )
    with pytest.raises(DbQueryError) as ei:
        db_adapter.query("SELECT foo FROM bar")
    assert ei.value.kind == "query"


def test_query_unavailable_when_connect_fails(monkeypatch):
    import psycopg2

    def boom():
        raise psycopg2.OperationalError("could not connect to server")

    monkeypatch.setattr(db_adapter, "_connect", boom)
    with pytest.raises(DbQueryError) as ei:
        db_adapter.query("SELECT 1")
    assert ei.value.kind == "unavailable"
