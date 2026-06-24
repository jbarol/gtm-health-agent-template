"""Redact absolute container filesystem paths from Slack-bound text.

Issues #293, #318: error messages and tool output sometimes carry absolute
paths (``/mnt/session/outputs/...``, ``/app/orchestrator/...``, ``/tmp/...``,
home dirs). These leak internal layout to the channel and look unprofessional.
``redact_paths`` collapses any such path to ``…/<basename>`` while leaving a
trailing ``:line`` suffix intact (useful for the operator) and plain text
untouched.
"""

from __future__ import annotations

import re

# A path rooted at one of the container/host prefixes, captured down to its
# final ``basename`` (optionally with a ``:123`` line suffix). The basename is
# group 1; everything before it (the directory chain) is dropped.
_PATH_RE = re.compile(
    r"/(?:app|mnt|tmp|Users|home|var|opt|root)/[^\s\"'`)]*/([^/\s\"'`):]+(?::\d+)?)"
)


def redact_paths(text: str | None) -> str:
    """Replace absolute container paths with ``…/<basename>``. Returns ""
    for falsy input."""
    if not text:
        return ""
    return _PATH_RE.sub(r"…/\1", text)
