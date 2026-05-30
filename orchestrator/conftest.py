"""Pytest configuration for orchestrator tests.

Does two things at collection time, BEFORE any test imports orchestrator modules:

1. Loads the real .env so smoke_test.py (which runs against live APIs) gets the
   user's actual credentials. Same dotenv loader as config.py — keep in sync.

2. Stubs slack_bolt.App. Without this, `import slack_bot` triggers a real
   auth.test call to Slack at module load (slack_bolt's App() default behavior)
   and unit tests fail with `BoltError: token is invalid`.

The slack_bolt stub only affects modules that haven't been imported yet.
smoke_test.py uses slack_sdk directly (not slack_bolt), so it gets real Slack
access when run separately. Unit tests that mock send_notification still work
because that's a thin wrapper above slack_sdk that we patch per-test.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

# Step 1: load real .env. setdefault means shell env vars still win.
_dotenv = Path(__file__).parent.parent / ".env"
if _dotenv.exists():
    for _line in _dotenv.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip())


# Step 2: stub slack_bolt.App so module imports don't hit Slack.
def _stub_slack_bolt():
    if "slack_bolt" in sys.modules:
        return

    fake_app = MagicMock(name="slack_bolt.App")
    fake_app.client = MagicMock()

    slack_bolt = MagicMock(name="slack_bolt")
    slack_bolt.App = MagicMock(return_value=fake_app)
    sys.modules["slack_bolt"] = slack_bolt

    socket_mode = MagicMock(name="slack_bolt.adapter.socket_mode")
    socket_mode.SocketModeHandler = MagicMock()
    sys.modules["slack_bolt.adapter"] = MagicMock(socket_mode=socket_mode)
    sys.modules["slack_bolt.adapter.socket_mode"] = socket_mode


_stub_slack_bolt()


# Skip pytest collection of smoke_test.py — it's a live-API smoke runner
# designed to be invoked directly as ``python orchestrator/smoke_test.py``,
# not via pytest discovery. Without this, pytest matches the ``*_test.py``
# glob, picks up the `test_messages_sonnet` / `test_coordinator_session`
# helpers, and either hits real Anthropic/Slack APIs (slow + flaky) or
# fails with "fixture not found" on the `def test(name, fn):` helper
# (renamed to `_check` 2026-05-15, but collect_ignore is the durable fix).
collect_ignore = ["smoke_test.py"]


# Step 3: register custom pytest markers used by orchestrator tests so
# pytest doesn't emit PytestUnknownMarkWarning. Currently:
#   - ``smoke``: end-to-end mocked smoke tests (e.g. ``batch_smoke_test.py``).
#     Run selectively with ``pytest -m smoke``.
def pytest_configure(config):  # noqa: D401
    config.addinivalue_line(
        "markers",
        "smoke: end-to-end mocked smoke test (run selectively with -m smoke)",
    )
