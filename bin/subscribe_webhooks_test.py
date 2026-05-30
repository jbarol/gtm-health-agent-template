"""Tests for ``bin/subscribe-webhooks.py`` (Plan #44 Task #14).

Filename has a hyphen, so we load by path (mirroring
``rollback_agent_test.py``). Tests mock the Anthropic SDK so no network
calls happen.

Mocking convention (post-2026-05-13 review HIGH #1):
  The fake Anthropic client is built from a real ``anthropic.Anthropic``
  instance and constrained via ``spec=`` so the top-level ``client.webhooks``
  attribute does NOT exist — webhooks live at ``client.beta.webhooks``.
  This makes the "wrong path" regression fail loud at test time.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import anthropic
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "bin" / "subscribe-webhooks.py"


def _load_subscribe_module():
    """Load ``bin/subscribe-webhooks.py`` by path (hyphen in filename)."""
    for p in (REPO_ROOT / "agents", REPO_ROOT / "orchestrator"):
        if str(p) not in sys.path:
            sys.path.insert(0, str(p))

    spec = importlib.util.spec_from_file_location("subscribe_webhooks", SCRIPT_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def script():
    return _load_subscribe_module()


def _fake_client(*, existing=None, fail_list=False, fail_create=False):
    """Build a MagicMock anthropic.Anthropic with controllable webhook state.

    Uses ``spec=anthropic.Anthropic`` so the top-level ``webhooks``
    attribute is NOT auto-created — the script must reach the resource
    via ``client.beta.webhooks`` (closing-review HIGH #1, 2026-05-13).
    """
    client = MagicMock(spec=anthropic.Anthropic, name="anthropic.Anthropic")
    # ``beta`` needs its own real anchor; spec=anthropic.Anthropic only
    # constrains the top level. We attach a fresh MagicMock so the
    # ``beta.webhooks.<method>`` calls land somewhere we can configure.
    client.beta = MagicMock(name="anthropic.Anthropic.beta")
    webhooks = client.beta.webhooks
    if fail_list:
        webhooks.list.side_effect = RuntimeError("list died")
    else:
        webhooks.list.return_value = existing or []

    if fail_create:
        webhooks.create.side_effect = RuntimeError("create died")
    else:
        webhooks.create.return_value = SimpleNamespace(
            id="wh_test",
            signing_key="whsec_xyz",
        )

    webhooks.delete.return_value = None
    return client


def test_subscribe_dry_run_plans_without_create(script, capsys):
    client = _fake_client()

    count = script._subscribe(
        client, "https://example.com/webhooks/anthropic", apply=False
    )

    assert count == len(script.SUBSCRIBED_EVENT_TYPES)
    client.beta.webhooks.create.assert_not_called()
    out = capsys.readouterr().out
    assert "[DRY]" in out


def test_subscribe_apply_creates_each_event_type(script, capsys):
    client = _fake_client()

    count = script._subscribe(
        client, "https://example.com/webhooks/anthropic", apply=True
    )

    assert count == len(script.SUBSCRIBED_EVENT_TYPES)
    assert client.beta.webhooks.create.call_count == len(script.SUBSCRIBED_EVENT_TYPES)
    out = capsys.readouterr().out
    assert "[OK]" in out
    assert "whsec_xyz" in out


def test_top_level_webhooks_attribute_does_not_exist_on_real_sdk():
    """Regression guard for closing-review HIGH #1, 2026-05-13.

    A previous version called ``client.webhooks.create`` — that
    attribute does NOT exist on the real Anthropic SDK; webhooks live
    at ``client.beta.webhooks``. The MagicMock used in other tests is
    constrained with ``spec=anthropic.Anthropic`` so the bug fails
    loud rather than silently passing via attribute auto-creation.
    """
    client = MagicMock(spec=anthropic.Anthropic, name="anthropic.Anthropic")
    with pytest.raises(AttributeError):
        _ = client.webhooks  # pyright: ignore[reportGeneralTypeIssues]


def test_subscribe_skips_existing(script, capsys):
    existing = [
        SimpleNamespace(
            id="wh_old",
            url="https://example.com/webhooks/anthropic",
            events=["session.status_terminated"],
        )
    ]
    client = _fake_client(existing=existing)

    count = script._subscribe(
        client, "https://example.com/webhooks/anthropic", apply=True
    )

    # 4 total event types; 1 already existed → create 3.
    assert count == len(script.SUBSCRIBED_EVENT_TYPES) - 1
    out = capsys.readouterr().out
    assert "[SKIP]" in out
    assert "session.status_terminated" in out


def test_delete_removes_matching_subscriptions(script, capsys):
    existing = [
        SimpleNamespace(
            id="wh_a",
            url="https://example.com/webhooks/anthropic",
            events=["session.status_terminated"],
        ),
        SimpleNamespace(
            id="wh_b",
            url="https://example.com/webhooks/anthropic",
            events=["vault_credential.refresh_failed"],
        ),
        # Different URL — should NOT be deleted.
        SimpleNamespace(
            id="wh_c",
            url="https://other.example.com/webhooks/anthropic",
            events=["session.status_terminated"],
        ),
    ]
    client = _fake_client(existing=existing)

    removed = script._delete(
        client, "https://example.com/webhooks/anthropic", apply=True
    )

    assert removed == 2
    delete_call_ids = [
        call.args[0] for call in client.beta.webhooks.delete.call_args_list
    ]
    assert delete_call_ids == ["wh_a", "wh_b"]


def test_delete_dry_run_does_not_delete(script):
    existing = [
        SimpleNamespace(
            id="wh_a",
            url="https://example.com/webhooks/anthropic",
            events=["session.status_terminated"],
        )
    ]
    client = _fake_client(existing=existing)

    removed = script._delete(
        client, "https://example.com/webhooks/anthropic", apply=False
    )

    assert removed == 1
    client.beta.webhooks.delete.assert_not_called()


def test_main_errs_without_target_url(script, monkeypatch, capsys):
    monkeypatch.delenv("ANTHROPIC_WEBHOOK_URL", raising=False)

    # Patch the client builder so we don't hit env-keyed Anthropic init.
    monkeypatch.setattr(script, "_build_client", lambda: _fake_client())

    rc = script.main(["--dry-run"])

    assert rc == 1
    err = capsys.readouterr().err
    assert "ANTHROPIC_WEBHOOK_URL" in err


def test_main_dry_run_smoke(script, monkeypatch, capsys):
    monkeypatch.setenv(
        "ANTHROPIC_WEBHOOK_URL", "https://example.com/webhooks/anthropic"
    )
    monkeypatch.setattr(script, "_build_client", lambda: _fake_client())

    rc = script.main(["--dry-run"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "Next steps" in out
    # Dry-run hint references --apply.
    assert "--apply" in out
