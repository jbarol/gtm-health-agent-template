---
kind: transient_infra
verified_kapa_rest_tool_blob: b8d2485afbdc4eb17d215f54143f6cc38625f268
last_verified_at: 2026-05-15T03:43:28Z
status: operational
re_verify_if: orchestrator/kapa_rest_tool.py changes
superseded_at_commit: e269740507f1dd4dc58e1e13e20038af3a01c67c
---

# Kapa REST tool status — operational

Kapa endpoint `/chat/stream/` with `Accept: application/json` verified operational on 2026-05-14.

Live probe (`bin/probe_kapa.py`, query "What is FATI?") returned:
- HTTP 200 in 5.59s
- 2,206 chars of content
- 6 distinct sources cited
- No mid-stream errors

This entry supersedes any prior outage note describing the 2026-05-14 HTTP 406. The 406 was caused by `Accept: text/event-stream` and was fixed by PR #167 (header change to `Accept: application/json`).

This note remains authoritative until `orchestrator/kapa_rest_tool.py` is modified — the verified SHA above pins the Kapa-touching code at the time of verification, not the wider repo. Any commit that leaves `kapa_rest_tool.py` untouched inherits this operational status. If `kapa_rest_tool.py` changes, re-run `python bin/probe_kapa.py` and bump the SHA + timestamp.
