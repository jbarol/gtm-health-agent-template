# Memory-store format for Plan #33 surface state

`surface_compute.py` reads per-portco markdown files from the Anthropic
memory store at `/mnt/memory/gtm-health-memory/{portco}/` and turns them
into rows for the persistent-state surface. To make that parseable
without an LLM, agents (Coordinator + Specialists) write each entry as a
YAML front-matter block followed by a free-form prose body.

## File set

```
/mnt/memory/gtm-health-memory/{portco}/
  findings.md
  metrics.md
  open_questions.md
  resolved.md
  decisions.md         # new in Plan #33
```

`decisions.md` is new: the Coordinator appends a row whenever the
agent or the portco resolves a finding so the surface can show
"recent decisions" without re-reading the full session log.

## Entry shape

Every entry in every file follows the same shape: a `---`-delimited
YAML block plus a prose body. The YAML keys vary by file; the
delimiter rule does not. Blocks are separated by a single blank line.

```
---
priority: P1
urgency: this_week
status: open
first_seen: 2026-05-08
decision_required: true
decision_options:
  - Coach AE-East
  - Pause partner channel
evidence: Partner-sourced opps closed 24.1% vs 32.5% prior quarter (n=47).
confidence: HIGH
---
Win rate fell 8pp in partner channel.

The drop is concentrated in deals owned by AE-East (n=12, win rate 16.7%).
Other regions held steady at 28-31%. Recommend coaching cycle plus
partner-handoff QA before Q2 quota lock.
```

The first prose line is the title. Subsequent lines are evidence
narrative. The parser concatenates the body lines into a single
string for the `body` field; only the first line is used as `title`
when the schema row requires one. The `evidence` YAML key takes
precedence over the body for the `evidence` schema field — the body
is supplementary narrative.

## YAML keys by file

### findings.md (→ FindingRow)

| Key | Type | Required | Notes |
|---|---|---|---|
| `priority` | P0 / P1 / P2 / P3 | yes | from `response_schemas.Priority` |
| `urgency` | immediate / this_week / this_quarter / monitor | yes | from `response_schemas.Urgency` |
| `status` | open / investigating / blocked / resolved / monitor | yes | from `surface_schemas.Status` |
| `first_seen` | ISO date | yes | YYYY-MM-DD |
| `decision_required` | bool | yes | |
| `decision_options` | list[str] | no | empty list ok |
| `evidence` | str | no | one-line summary; body is supplementary |
| `confidence` | HIGH / MEDIUM / LOW / DATA_GAP | yes | from `response_schemas.Confidence` |

Example with two entries:

```
---
priority: P1
urgency: this_week
status: open
first_seen: 2026-05-08
decision_required: true
decision_options:
  - Coach AE-East
  - Pause partner channel
evidence: Partner-sourced opps closed 24.1% vs 32.5% prior quarter (n=47).
confidence: HIGH
---
Win rate fell 8pp in partner channel.

Concentrated in AE-East deals; other regions flat.

---
priority: P2
urgency: this_quarter
status: investigating
first_seen: 2026-05-05
decision_required: false
decision_options: []
evidence: Median stage-3 duration rose from 18d to 30d over 6 weeks.
confidence: MEDIUM
---
Stage 3 cycle time +12 days.

Cause unclear; queued for Sales specialist deep-dive.
```

### metrics.md (→ KeyMetricRow)

| Key | Type | Required | Notes |
|---|---|---|---|
| `name` | str | yes | metric label, e.g. "Win rate (Q1)" |
| `value` | str | yes | current value as a display string |
| `delta_vs_prior` | str | yes | signed delta, e.g. "-8.4pp" or "flat" |
| `status` | open / investigating / blocked / resolved / monitor | yes | |
| `as_of` | ISO date | yes | YYYY-MM-DD |

Example with two entries:

```
---
name: Win rate (new biz, Q1)
value: 24.1%
delta_vs_prior: -8.4pp
status: investigating
as_of: 2026-05-11
---
Partner-channel drag; AE-East cohort underperforming.

---
name: GRR
value: 87.2%
delta_vs_prior: -1.9pp
status: monitor
as_of: 2026-05-11
---
Within tolerance band; watching Western region.
```

### open_questions.md (→ OpenQuestionRow)

| Key | Type | Required | Notes |
|---|---|---|---|
| `question` | str | yes | the question itself |
| `asked_at` | ISO date | yes | YYYY-MM-DD |
| `context` | str | no | who or what triggered the question |

Example with two entries:

```
---
question: Why is stage 3 elongating?
asked_at: 2026-05-09
context: Needs Sales specialist deep-dive.
---
Median stage-3 days rose from 18 to 30 in six weeks. No
single owner is dragging the cohort; possibly a buyer-side
procurement change.

---
question: Is Western GRR a single-team problem?
asked_at: 2026-05-08
context: Cross-Domain Synthesizer pattern-match pending.
---
Western churn rate is 4pp above other regions, but n is
small. Need to check if it concentrates in one CSM book.
```

### resolved.md (→ FindingRow, status="resolved")

Same keys as `findings.md`. Used by `compute_trajectory` to detect
which findings have been resolved in the trailing window. New
entries are appended; old entries are never removed (the file is
the audit trail).

```
---
priority: P2
urgency: this_quarter
status: resolved
first_seen: 2026-04-15
decision_required: false
decision_options: []
evidence: Coverage dropped to 1.8x in Q1; recovered to 2.4x in Q2.
confidence: HIGH
---
Q1 pipeline coverage shortfall.

Resolved 2026-05-02 — Q2 plan generation refilled the funnel.
```

### decisions.md (→ DecisionRow) — new

| Key | Type | Required | Notes |
|---|---|---|---|
| `title` | str | yes | the finding or topic the decision is about |
| `decided_at` | ISO date | yes | YYYY-MM-DD |
| `decision` | str | yes | freeform; conventional values: acted / ignored / corrected / deferred |
| `portco_response` | str | no | what the portco said or did |

Example with two entries:

```
---
title: MQL to SQL drop in partner-sourced
decided_at: 2026-05-04
decision: acted
portco_response: Coaching cycle launched for partner-channel AEs.
---
Q1 partner-sourced MQL→SQL conversion fell from 22% to 14%.
RevOps + Sales VP agreed to a 4-week coaching sprint.

---
title: ARR computation off by 4%
decided_at: 2026-05-07
decision: corrected
portco_response: Schema cache fixed; downstream metrics recomputed.
---
Discovered stale Opportunity.Amount cache. Forced refresh,
rebuilt cohort metrics.
```

## Parser contract

`surface_compute.py` reads each file with this contract:

1. Split the file on lines that contain only `---`. Pairs of `---`
   delimit a YAML block.
2. For each block: `yaml.safe_load` the YAML. On parse error, log
   at debug and skip the entry — never raise.
3. The text between the closing `---` of one entry and the opening
   `---` of the next is the body for the previous entry. The first
   non-empty line of the body is the title; the remainder is the
   evidence narrative.
4. Construct the schema row from the YAML keys + body. Missing
   required keys → log at debug, skip.
5. Unknown YAML keys → ignored (Pydantic `extra="forbid"` is
   handled at construction time by filtering to known keys).

Empty file → empty list. Missing file → empty list. File present
but no entries → empty list.

## Migration note

Pre-Plan-#33 memory-store files are loose markdown bullets without
front matter. Those files parse to **empty lists** under the new
format — they will not crash the surface. There is no hard cutover:
the next time an agent writes to a file, it uses the new format,
and the surface picks the entries up. Old bullets remain in place
until they are superseded or the file is rewritten in full. The
Coordinator's nightly compaction job (if/when added) is the
expected vehicle for migrating bulk historical entries.

A `pre-front-matter` line (anything before the first `---`) is
treated as preamble and ignored by the parser. Agents may keep a
top-level `# Heading` for human readability.
