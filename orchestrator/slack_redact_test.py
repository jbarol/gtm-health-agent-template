"""Unit tests for slack_redact (Task 7 / F5).

Issues #293, #318: absolute container filesystem paths must not reach Slack.

Run:
    cd orchestrator && python3 -m pytest slack_redact_test.py -q
"""

from __future__ import annotations

from slack_redact import redact_paths


def test_redacts_mnt_path():
    assert (
        redact_paths("wrote /mnt/session/outputs/sf_dump_9.parquet ok")
        == "wrote …/sf_dump_9.parquet ok"
    )


def test_redacts_app_path_with_line_suffix():
    assert (
        redact_paths("crash in /app/orchestrator/session_runner.py:1813")
        == "crash in …/session_runner.py:1813"
    )


def test_redacts_tmp_and_home_paths():
    assert redact_paths("/tmp/gtm/x.csv") == "…/x.csv"
    assert redact_paths("/Users/jb/secret/report.xlsx") == "…/report.xlsx"


def test_leaves_plain_text_untouched():
    assert redact_paths("win rate 23% this quarter") == "win rate 23% this quarter"


def test_handles_empty_and_none():
    assert redact_paths("") == ""
    assert redact_paths(None) == ""
