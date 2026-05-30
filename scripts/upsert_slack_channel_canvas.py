#!/usr/bin/env python3
"""Upsert the "How to Ask" canvas in every GTM portco Slack channel.

Source-of-truth content lives at ``docs/slack/channel-canvas.md``. This
script reads that file, walks ``portco_config.json`` for active portco
channels, and either creates a new channel canvas via
``conversations.canvases.create`` or replaces an existing one via
``canvases.edit``.

Per-channel canvas IDs are persisted to ``.canvases/state.json`` so the
script knows which channels already have a canvas. On first run for a
channel, the script also calls ``conversations.info`` to opportunistically
pick up a canvas that may already exist on the channel (so we never
create a duplicate).

Required Slack scopes (already granted on the new bot token):
    canvases:read, canvases:write, channels:manage, channels:read,
    groups:read

Usage:
    python scripts/upsert_slack_channel_canvas.py
    python scripts/upsert_slack_channel_canvas.py --channel C12345678
    python scripts/upsert_slack_channel_canvas.py --dry-run
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

# ---------- Paths ----------

REPO_ROOT = Path(__file__).resolve().parent.parent
CONTENT_FILE = REPO_ROOT / "docs" / "slack" / "channel-canvas.md"
PORTCO_CONFIG = REPO_ROOT / "portco_config.json"
STATE_DIR = REPO_ROOT / ".canvases"
STATE_FILE = STATE_DIR / "state.json"

CANVAS_TITLE = "GTM Health Bot — How to Ask"

# ---------- Logging ----------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("upsert_slack_channel_canvas")


# ---------- State persistence ----------


@dataclass
class CanvasState:
    """Local cache of channel_id -> canvas_id.

    Lives at ``.canvases/state.json`` and is committed to the repo so CI
    runs don't lose the mapping between deploys.
    """

    canvases: dict[str, str] = field(default_factory=dict)

    @classmethod
    def load(cls) -> "CanvasState":
        if not STATE_FILE.exists():
            return cls()
        try:
            raw = json.loads(STATE_FILE.read_text())
            return cls(canvases=dict(raw.get("canvases", {})))
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("State file unreadable (%s); starting fresh.", exc)
            return cls()

    def save(self) -> None:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(
            json.dumps({"canvases": self.canvases}, indent=2, sort_keys=True) + "\n"
        )

    def get(self, channel_id: str) -> str | None:
        return self.canvases.get(channel_id)

    def set(self, channel_id: str, canvas_id: str) -> None:
        self.canvases[channel_id] = canvas_id


# ---------- Slack helpers ----------


def load_content() -> str:
    if not CONTENT_FILE.exists():
        raise FileNotFoundError(
            f"Canvas content not found at {CONTENT_FILE}. Did you delete "
            "docs/slack/channel-canvas.md?"
        )
    return CONTENT_FILE.read_text()


def load_portco_channels() -> list[tuple[str, str]]:
    """Return ``[(portco_key, channel_id), ...]`` for active portcos."""

    if not PORTCO_CONFIG.exists():
        raise FileNotFoundError(f"Portco config missing: {PORTCO_CONFIG}")
    raw = json.loads(PORTCO_CONFIG.read_text())
    out: list[tuple[str, str]] = []
    for key, cfg in raw.get("portcos", {}).items():
        channel = cfg.get("slack_channel")
        if not channel:
            continue
        if cfg.get("status") != "active":
            # Skip pending_crm / archived portcos. They have no live channel
            # to pin docs into.
            continue
        out.append((key, channel))
    return out


def lookup_existing_canvas(client: WebClient, channel_id: str) -> str | None:
    """Best-effort lookup of an existing channel canvas via
    ``conversations.info``.

    Slack returns ``properties.canvas.file_id`` when a channel canvas
    already exists. If the call fails (missing scope, channel not found)
    we return ``None`` and the caller will create a new canvas.
    """

    try:
        resp = client.conversations_info(channel=channel_id)
    except SlackApiError as exc:
        log.warning(
            "conversations.info failed for %s: %s",
            channel_id,
            exc.response.get("error") if exc.response else exc,
        )
        return None

    channel = resp.get("channel") or {}
    props = channel.get("properties") or {}
    canvas = props.get("canvas") or {}
    return canvas.get("file_id")


def build_document_content(markdown: str) -> dict[str, str]:
    """Slack canvas document_content payload — markdown variant.

    The Slack canvases API accepts ``{"type": "markdown", "markdown": ...}``
    directly, so we don't need to convert to rich-text blocks. If a future
    Slack release changes this contract, swap to the rich-text-blocks
    representation here.
    """

    return {"type": "markdown", "markdown": markdown}


def create_canvas(
    client: WebClient, channel_id: str, content: str, *, dry_run: bool
) -> str | None:
    payload = {
        "channel_id": channel_id,
        "title": CANVAS_TITLE,
        "document_content": build_document_content(content),
    }
    if dry_run:
        log.info(
            "[dry-run] conversations.canvases.create payload for %s:\n%s",
            channel_id,
            json.dumps(payload, indent=2),
        )
        return None
    resp = client.conversations_canvases_create(**payload)
    canvas_id = resp.get("canvas_id")
    if not canvas_id:
        raise RuntimeError(
            f"conversations.canvases.create returned no canvas_id: {resp.data}"
        )
    return canvas_id


def edit_canvas(
    client: WebClient,
    canvas_id: str,
    content: str,
    *,
    dry_run: bool,
) -> None:
    """Replace the entire canvas body.

    The ``replace`` operation without a ``section_id`` substitutes the
    full canvas content — exactly the upsert semantics we want.
    """

    payload = {
        "canvas_id": canvas_id,
        "changes": [
            {
                "operation": "replace",
                "document_content": build_document_content(content),
            }
        ],
    }
    if dry_run:
        log.info(
            "[dry-run] canvases.edit payload for %s:\n%s",
            canvas_id,
            json.dumps(payload, indent=2),
        )
        return
    client.canvases_edit(**payload)


# ---------- Orchestration ----------


def upsert_channel(
    client: WebClient,
    state: CanvasState,
    portco_key: str,
    channel_id: str,
    content: str,
    *,
    dry_run: bool,
) -> dict[str, Any]:
    """Create or update the canvas for one channel. Returns a result dict."""

    canvas_id = state.get(channel_id)
    if not canvas_id and not dry_run:
        # First time we've seen this channel — see if Slack already has a
        # channel canvas we should adopt instead of duplicating.
        canvas_id = lookup_existing_canvas(client, channel_id)
        if canvas_id:
            log.info(
                "Adopted existing canvas %s on channel %s (portco=%s)",
                canvas_id,
                channel_id,
                portco_key,
            )
            state.set(channel_id, canvas_id)

    try:
        if canvas_id:
            edit_canvas(client, canvas_id, content, dry_run=dry_run)
            action = "edited"
        else:
            new_id = create_canvas(client, channel_id, content, dry_run=dry_run)
            if new_id:
                state.set(channel_id, new_id)
                canvas_id = new_id
            action = "created"
        log.info(
            "%s canvas for portco=%s channel=%s canvas_id=%s",
            action,
            portco_key,
            channel_id,
            canvas_id or "<dry-run>",
        )
        return {
            "portco": portco_key,
            "channel": channel_id,
            "canvas_id": canvas_id,
            "action": action,
            "ok": True,
        }
    except SlackApiError as exc:
        err = exc.response.get("error") if exc.response else str(exc)
        log.error(
            "Slack API failure for portco=%s channel=%s: %s",
            portco_key,
            channel_id,
            err,
        )
        return {
            "portco": portco_key,
            "channel": channel_id,
            "canvas_id": canvas_id,
            "action": "error",
            "ok": False,
            "error": err,
        }


def run(
    *,
    only_channel: str | None = None,
    dry_run: bool = False,
) -> list[dict[str, Any]]:
    content = load_content()
    channels = load_portco_channels()
    if only_channel:
        channels = [(k, c) for k, c in channels if c == only_channel]
        if not channels:
            log.warning(
                "No active portco found with channel=%s. Nothing to do.",
                only_channel,
            )
            return []

    state = CanvasState.load()

    token = os.environ.get("SLACK_BOT_TOKEN")
    if not token and not dry_run:
        raise RuntimeError(
            "SLACK_BOT_TOKEN must be set. Use --dry-run to preview without "
            "calling Slack."
        )
    # In dry-run we still construct a client (sans token) so payload
    # generation flows through the same code path. The client is never
    # called, so a dummy token is fine.
    client = WebClient(token=token or "xoxb-dry-run")

    results: list[dict[str, Any]] = []
    for portco_key, channel_id in channels:
        results.append(
            upsert_channel(
                client,
                state,
                portco_key,
                channel_id,
                content,
                dry_run=dry_run,
            )
        )

    if not dry_run:
        state.save()

    successes = sum(1 for r in results if r["ok"])
    failures = len(results) - successes
    log.info(
        "Upsert complete: %d ok, %d failed, %d total (dry_run=%s)",
        successes,
        failures,
        len(results),
        dry_run,
    )
    return results


# ---------- CLI ----------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Upsert the GTM Health Bot 'How to Ask' canvas in every "
        "portco Slack channel."
    )
    parser.add_argument(
        "--channel",
        help="Upsert only this channel ID (must match an active portco's "
        "slack_channel).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the payloads that would be sent, but don't call Slack.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    results = run(only_channel=args.channel, dry_run=args.dry_run)
    failures = [r for r in results if not r["ok"]]
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
